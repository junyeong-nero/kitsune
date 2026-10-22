// Kitsune - Background Script
// Connects to local bridge server via WebSocket, relays commands to content scripts

'use strict';

const EXPECTED_CONTENT_VERSION = 40;
const WS_URL = 'ws://127.0.0.1:47652';
// scripts/build_extension.py sets 'signed' for the AMO build (see content.js).
const KITSUNE_BUILD = 'dev';
let ws = null;
let reconnectTimer = null;
let heartbeatTimer = null;
let reconnectDelay = 1000; // Start at 1s, exponential backoff
const MAX_RECONNECT_DELAY = 15000;

// Policies survive document navigation, but are cleared when a tab closes.
const dialogPolicies = new Map();
// The last main-frame request per tab, { requestId, url, at, statusCode }, so an unscriptable
// error page can name its status. A new main-frame request replaces it and a discard or reload
// clears it, so it never describes a later document. at: when the request started; statusCode:
// once its headers arrive. These events can reach the background page tens of ms before or after
// the tabs events of the same load, so they cannot tell which document a tab shows.
const mainFrameStatus = new Map();
// Callbacks waiting for a tab's next status record (see waitForHttpStatus); called with true on close.
const mainFrameStatusWaiters = new Map();
// When a tab was last woken or reloaded (see markReload), for injectionFailure.
const tabReloads = new Map();
// The tab's last load as tabs events report it, { at, complete }: replaced when the tab reports
// loading, complete once it reports complete after that (see wokenPageState).
const tabLoads = new Map();
const notifyStatusWaiters = (tabId, closed = false) =>
  [...(mainFrameStatusWaiters.get(tabId) || [])].forEach(notify => notify(closed));
browser.tabs.onRemoved.addListener(tabId => {
  dialogPolicies.delete(tabId);
  pruneShortRefs(tabId);
  mainFrameStatus.delete(tabId);
  tabReloads.delete(tabId);
  tabLoads.delete(tabId);
  notifyStatusWaiters(tabId, true);
});
browser.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.discarded === true) mainFrameStatus.delete(tabId);
  // The browser also wakes discarded tabs, e.g. when one is selected.
  if (changeInfo.discarded === false) tabReloads.set(tabId, Date.now());
  // agent: the agent was acting on the tab when it started loading (see userActivity).
  if (changeInfo.status === 'loading') {
    pruneShortRefs(tabId);
    tabLoads.set(tabId, { at: Date.now(), complete: false, agent: agentTouched(tabId) });
  }
  const load = tabLoads.get(tabId);
  if (load && changeInfo.status === 'complete') load.complete = true;
  // A synced copy that loads was not the agent's (see Agent windows).
  if (changeInfo.status === 'loading' || changeInfo.url) returnSyncedCopy(tabId);
});
const MAIN_FRAME = { urls: ['<all_urls>'], types: ['main_frame'] };
// A redirect keeps its requestId, and so its record.
const mainFrameRequest = (tabId, requestId) => {
  const seen = mainFrameStatus.get(tabId);
  if (seen?.requestId === requestId) return seen;
  const started = { requestId, at: Date.now() };
  mainFrameStatus.set(tabId, started);
  return started;
};
browser.webRequest?.onBeforeRequest.addListener(({ tabId, requestId, url }) => {
  if (tabId >= 0) mainFrameRequest(tabId, requestId).url = url;
}, MAIN_FRAME);
browser.webRequest?.onHeadersReceived.addListener(
  ({ tabId, requestId, url, statusCode }) => {
    if (tabId < 0) return;
    Object.assign(mainFrameRequest(tabId, requestId), { url, statusCode });
    notifyStatusWaiters(tabId);
  },
  MAIN_FRAME);

// Tabs a page opens (target=_blank links, window.open) name it as their opener.
// Page actions report the tabs their tab opened since the action started.
let tabsCreated = 0;
const openedTabs = [];          // { tabId, openerTabId, seq } for open tabs with an opener
const actionStarts = new Map(); // tabId -> tabsCreated when its last page action started
const linkFallbackTabs = new Set(); // tabs opened by openBlockedLink
browser.tabs.onCreated.addListener(tab => {
  tabsCreated++;
  if (tab.openerTabId != null) {
    openedTabs.push({ tabId: tab.id, openerTabId: tab.openerTabId, seq: tabsCreated });
    if (agentTouched(tab.openerTabId)) keepOpenedTabInBackground(tab);
  }
});
browser.tabs.onRemoved.addListener(tabId => {
  actionStarts.delete(tabId);
  linkFallbackTabs.delete(tabId);
  agentTabs.delete(tabId);
  agentTouches.delete(tabId);
  revertActivation.delete(tabId);
  for (const record of agentWindows) {
    record.windowCopies?.delete(tabId);
    record.syncedCopies.delete(tabId);
  }
  for (let i = openedTabs.length - 1; i >= 0; i--) {
    if (openedTabs[i].tabId === tabId || openedTabs[i].openerTabId === tabId) openedTabs.splice(i, 1);
  }
});

// == Shared browser (docs/api-reference.md#shared-browser) ==
// The user and the agent use this browser at the same time: agent work never takes the user's
// focus, and it backs off from a tab the user is using.

// Tabs the agent created or its page actions opened (getTabs `owned`); only these close without
// the user's approval. They live in this background page, so a bridge restart keeps them; an
// extension reload or browser restart forgets them, and those tabs then count as the user's.
const agentTabs = new Set();
// tabId -> { running, until }: agent commands on the tab now, and until when the tabs it opens
// and the loads it starts are still the agent's (a click's tab or navigation can lag its reply).
const agentTouches = new Map();
const AGENT_EFFECT_GRACE_MS = 2000;
const agentTouched = tabId => {
  const touch = agentTouches.get(tabId);
  return !!touch && (touch.running > 0 || Date.now() < touch.until);
};
function touchStart(tabId) {
  const touch = agentTouches.get(tabId) || { running: 0, until: 0 };
  touch.running++;
  agentTouches.set(tabId, touch);
}
function touchEnd(tabId) {
  const touch = agentTouches.get(tabId);
  if (!touch) return;
  touch.running = Math.max(0, touch.running - 1);
  touch.until = Date.now() + AGENT_EFFECT_GRACE_MS;
}
function markAgentTab(tabId) {
  agentTabs.add(tabId);
  const load = tabLoads.get(tabId);
  if (load) load.agent = true;
  if (!agentTouches.has(tabId)) agentTouches.set(tabId, { running: 0, until: Date.now() + AGENT_EFFECT_GRACE_MS });
}

// Firefox shows a tab a page opens (target=_blank, window.open) at once. When an agent action
// opened it, the tab the user saw is shown again right away, and a popup window gives the
// window focus back; both flicker for a moment.
const OPENED_TAB_REVERT_MS = 2000;
const revertActivation = new Map(); // agent-opened tabId -> deadline for undoing its activation
const lastActivation = new Map();   // windowId -> { tabId, previousTabId }
const revertFocus = new Map();      // agent popup windowId -> deadline for undoing its focus
let focusedWindowId = null, previousFocusedWindowId = null;
browser.windows?.getLastFocused?.().then(w => {
  focusedWindowId ??= w.focused ? w.id : browser.windows.WINDOW_ID_NONE;
}, () => {});
function keepOpenedTabInBackground(tab) {
  markAgentTab(tab.id);
  const deadline = Date.now() + OPENED_TAB_REVERT_MS;
  revertActivation.set(tab.id, deadline);
  const last = lastActivation.get(tab.windowId);
  if (tab.active && last?.tabId === tab.id) undoActivation(tab.id, last.previousTabId);
  browser.tabs.get(tab.openerTabId).then(opener => {
    if (opener.windowId === tab.windowId) return;
    revertFocus.set(tab.windowId, deadline);
    if (focusedWindowId === tab.windowId) undoFocus(tab.windowId, previousFocusedWindowId);
  }, () => {});
}
function undoActivation(tabId, previousTabId) {
  const deadline = revertActivation.get(tabId);
  revertActivation.delete(tabId);
  if (deadline >= Date.now() && previousTabId != null && previousTabId !== tabId) {
    browser.tabs.update(previousTabId, { active: true }).catch(() => {});
  }
}
function undoFocus(windowId, previousWindowId) {
  const deadline = revertFocus.get(windowId);
  revertFocus.delete(windowId);
  // Without a previously focused browser window the user was in another app; nothing to give back.
  if (deadline >= Date.now() && previousWindowId != null && previousWindowId !== browser.windows.WINDOW_ID_NONE) {
    browser.windows.update(previousWindowId, { focused: true }).catch(() => {});
  }
}
browser.tabs.onActivated?.addListener(({ tabId, previousTabId, windowId }) => {
  lastActivation.set(windowId, { tabId, previousTabId });
  if (revertActivation.has(tabId)) undoActivation(tabId, previousTabId);
});
browser.windows?.onFocusChanged?.addListener(windowId => {
  [previousFocusedWindowId, focusedWindowId] = [focusedWindowId, windowId];
  if (revertFocus.has(windowId)) undoFocus(windowId, previousFocusedWindowId);
  const agentWindow = agentWindowRecord(windowId);
  if (agentWindow?.focusWindowId != null && Date.now() < agentWindow.until) {
    browser.windows.update(agentWindow.focusWindowId, { focused: true }).catch(() => {});
  }
});
browser.windows?.onRemoved?.addListener(windowId => {
  revertFocus.delete(windowId);
  lastActivation.delete(windowId);
  const agentWindow = agentWindowRecord(windowId);
  if (agentWindow) agentWindows.delete(agentWindow);
});

// Agent windows (docs/api-reference.md#agent-windows): only an explicit createWindow or detachTab
// creates a window; all other agent work stays in background tabs of the user's window.
// Firefox may focus a new window despite focused: false (Zen does), so the window the user had
// focused gets the focus back right away, and again if the browser focuses the new window within
// AGENT_WINDOW_SYNC_MS; the new window can flicker for a moment.
// Zen's window sync mirrors tabs between windows: the new window gets a lazy copy of each user
// tab, and the user's windows get a lazy copy of the agent window's tab, which shows the agent's
// page once the agent window closes. Closing a copy closes its original, so:
// - windowCopies: the browser's copies in a createWindow window (discarded when it opens), decided
//   once at creation. Never owned, so closeTab refuses them; closeWindow may close the window with
//   them, since removing a window closes no originals.
// - syncedCopies: tabs adopted as copies of the agent window's tab (owned, closed by closeWindow).
//   See isSyncedCopy; one goes back to the user when the user shows it, it starts a load the agent
//   did not start, or it reports user activity when closeWindow would close it.
const AGENT_WINDOW_SYNC_MS = 1500;
// { windowId (null until windows.create resolves), userWindowIds, urls, until, focusWindowId,
//   windowCopies (null for detachTab), syncedCopies, closing }
const agentWindows = new Set();
const agentWindowRecord = windowId => [...agentWindows].find(w => w.windowId === windowId);
const syncedCopyRecord = tabId => [...agentWindows].find(w => w.syncedCopies.has(tabId));
// Within AGENT_WINDOW_SYNC_MS of windows.create, in a window that existed before it, in the
// background, without an opener, discarded (a lazy tab that loads nothing), at about:blank (where
// Zen's copies start) or one of the agent window's URLs. A tab the user opens is shown, has an
// opener, or loads a page.
const isSyncedCopy = (tab, record) => Date.now() <= record.until && record.userWindowIds.has(tab.windowId)
  && !tab.active && tab.openerTabId == null && tab.discarded === true && record.urls.has(tab.url);
browser.tabs.onCreated.addListener(tab => {
  for (const record of agentWindows) {
    if (tab.windowId === record.windowId) {
      if (record.windowCopies && tab.discarded === true && Date.now() <= record.until) record.windowCopies.add(tab.id);
      return;
    }
    if (isSyncedCopy(tab, record)) {
      record.syncedCopies.add(tab.id);
      agentTabs.add(tab.id);
      return;
    }
  }
});
function returnSyncedCopy(tabId) {
  const record = syncedCopyRecord(tabId);
  if (!record || record.closing || agentTouched(tabId)) return;
  record.syncedCopies.delete(tabId);
  agentTabs.delete(tabId);
}
browser.tabs.onActivated?.addListener(({ tabId }) => returnSyncedCopy(tabId));

// windows.create for an explicit request; windowCopies: whether to record the browser's copies in it.
async function createAgentWindow(createData, urls, { windowCopies }) {
  const [focused, windows] = await Promise.all([
    browser.windows.getLastFocused().catch(() => null), browser.windows.getAll().catch(() => [])]);
  const record = {
    windowId: null, userWindowIds: new Set(windows.map(w => w.id)), urls: new Set(['about:blank', ...urls]),
    until: Date.now() + AGENT_WINDOW_SYNC_MS, focusWindowId: focused?.focused ? focused.id : null,
    windowCopies: windowCopies ? new Set() : null, syncedCopies: new Set(), closing: false,
  };
  agentWindows.add(record);
  let created;
  try {
    created = await browser.windows.create({ ...createData, focused: false });
  } catch (e) {
    agentWindows.delete(record);
    throw e;
  }
  record.windowId = created.id;
  // Without a focused browser window the user was in another app; nothing to give back.
  if (record.focusWindowId != null) await browser.windows.update(record.focusWindowId, { focused: true }).catch(() => {});
  return { window: created, record };
}

// After closeWindow removed the agent window: its synced copies now show the agent's page.
async function closeSyncedCopies(record) {
  for (const tabId of [...record.syncedCopies]) {
    if (await userActivity(tabId)) {
      record.syncedCopies.delete(tabId);
      agentTabs.delete(tabId);
      continue;
    }
    await browser.tabs.remove(tabId).catch(() => {});
  }
}

// Recent user activity on a tab (docs/api-reference.md#user-activity): agent writes and
// navigations on it, and view switches away from it, return user_active instead of acting.
// A navigation counts while the shown tab loads a document the agent did not start.
const USER_NAVIGATION_WINDOW_MS = 10000;

async function userActivity(tabId) {
  const tab = await browser.tabs.get(tabId).catch(() => null);
  if (!tab) return null;
  const load = tabLoads.get(tabId);
  const sinceLoad = load ? Date.now() - load.at : Infinity;
  if (tab.active && tab.status === 'loading' && load && !load.complete && !load.agent
      && sinceLoad < USER_NAVIGATION_WINDOW_MS) {
    return { tabId, url: tab.url, reason: 'navigating', sinceMs: sinceLoad };
  }
  if (tab.discarded || isRestrictedUrl(tab.url)) return null;
  const page = await browser.tabs.sendMessage(tabId, { action: 'userActivity' }).catch(() => null);
  return page?.active ? { tabId, url: tab.url, ...page.state } : null;
}

// The tab whose user activity an action must respect: switching the view or window focus leaves
// the tab the user sees; everything else acts on its own tab.
async function guardedTabId(action, params) {
  let windowId;
  if (action === 'switchTab') {
    const target = await browser.tabs.get(params.tabId).catch(() => null);
    if (!target || target.active) return null;
    windowId = target.windowId;
  } else if (action === 'focusWindow') {
    const focused = await browser.windows.getLastFocused().catch(() => null);
    if (!focused || focused.id === params.windowId) return null;
    windowId = focused.id;
  } else {
    return params.tabId;
  }
  return (await browser.tabs.query({ windowId, active: true }).catch(() => []))[0]?.id ?? null;
}

async function userActiveError(tabId) {
  const state = tabId != null && await userActivity(tabId);
  return state ? tabError(`The user is using tab ${tabId}; nothing was done`, 'user_active', state) : null;
}
browser.runtime.onMessage.addListener((msg, sender) => {
  if (msg.action === 'getDialogPolicy') {
    return Promise.resolve(dialogPolicies.get(sender.tab?.id) || null);
  }
});

// == WebSocket Connection ==

// The first 16 hex digits of the SHA-256 of data, as src/kitsune/version.py writes them.
const sha256Prefix = async data => [...new Uint8Array(await crypto.subtle.digest('SHA-256', data))]
  .map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);

// Fingerprint of the scripts this copy loaded, sent in hello: the bridge compares it with the
// unpacked extension on disk to report a developer build edited but not reloaded. Mirrors
// extension_source_hash in src/kitsune/version.py; null when it cannot be computed.
const SOURCE_HASH = (async () => {
  try {
    const manifest = browser.runtime.getManifest();
    // Firefox lists manifest script paths as absolute moz-extension://<uuid>/ URLs; hash the
    // path relative to the extension root, as written in manifest.json and read from disk.
    const root = browser.runtime.getURL('');
    const relative = name => (name.startsWith(root) ? name.slice(root.length) : name).replace(/^\/+/, '');
    const scripts = [...new Set([...(manifest.background?.scripts || []),
      ...(manifest.content_scripts || []).flatMap(entry => entry.js || [])].map(relative))];
    if (!scripts.length) return null;
    const encoder = new TextEncoder();
    const separator = new Uint8Array([0]);
    const parts = [];
    for (const name of scripts) {
      const bytes = await (await fetch(browser.runtime.getURL(name))).arrayBuffer();
      parts.push(encoder.encode(name), separator, new Uint8Array(bytes), separator);
    }
    return await sha256Prefix(await new Blob(parts).arrayBuffer());
  } catch {
    return null;
  }
})();

// Fingerprint of the manifest.json this copy loaded, for manifest-only edits. A field of its own,
// so a bridge still compares the scripts of an extension that predates it. Mirrors
// extension_manifest_hash in src/kitsune/version.py; null when it cannot be computed.
const MANIFEST_HASH = (async () => {
  try {
    return await sha256Prefix(await (await fetch(browser.runtime.getURL('manifest.json'))).arrayBuffer());
  } catch {
    return null;
  }
})();

function connect() {
  // Kill any existing connection first
  cleanup();
  
  try {
    const socket = ws = new WebSocket(WS_URL);
    
    ws.onopen = async () => {
      console.log('[Kitsune] Connected to bridge server');
      reconnectDelay = 1000; // Reset backoff
      stopReconnect();
      const [sourceHash, manifestHash] = await Promise.all([SOURCE_HASH, MANIFEST_HASH]);
      if (ws !== socket) return; // replaced while the fingerprints were computed
      ws.send(JSON.stringify({ type: 'hello', version: browser.runtime.getManifest().version, build: KITSUNE_BUILD,
        sourceHash, manifestHash }));
      sendBrowserState();
      startHeartbeat();
      injectAllTabs(); // Inject content scripts into all existing tabs
    };
    
    ws.onmessage = async (event) => {
      let cmdId = null;
      try {
        const command = JSON.parse(event.data);
        cmdId = command.id;
        console.log('[Kitsune] <<', command.action, cmdId);
        
        const result = await handleCommand(command);
        
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ id: cmdId, result }));
        }
      } catch (e) {
        console.error('[Kitsune] Command error:', e);
        if (cmdId && ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ id: cmdId, result: { error: e.message } }));
        }
      }
    };
    
    ws.onclose = () => {
      console.log('[Kitsune] Disconnected');
      cleanup();
      scheduleReconnect();
    };
    
    ws.onerror = () => {
      console.error('[Kitsune] WebSocket error');
      // onclose will fire after this, which handles reconnect
    };
  } catch (e) {
    console.error('[Kitsune] Connection failed:', e);
    cleanup();
    scheduleReconnect();
  }
}

// For the bridge's browser_status diagnosis (docs/api-reference.md#connection-diagnosis): the normal
// windows Kitsune can use (private ones only when allowed) and whether it may script all sites.
async function sendBrowserState() {
  const [windows, allSites] = await Promise.all([
    browser.windows.getAll({ windowTypes: ['normal'] }).then(all => all.length, () => null),
    browser.permissions.contains({ origins: ['<all_urls>'] }).catch(() => null),
  ]);
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'state', windows, allSites }));
}
for (const event of [browser.windows?.onCreated, browser.windows?.onRemoved,
  browser.permissions?.onAdded, browser.permissions?.onRemoved]) {
  event?.addListener(() => sendBrowserState());
}

function cleanup() {
  stopHeartbeat();
  if (ws) {
    try { ws.close(); } catch {}
    ws = null;
  }
}

function scheduleReconnect() {
  stopReconnect();
  console.log(`[Kitsune] Reconnecting in ${reconnectDelay}ms...`);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, reconnectDelay);
  // Exponential backoff capped at MAX
  reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
}

function stopReconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
}

// Heartbeat: detect dead connections the browser hasn't noticed
function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.log('[Kitsune] Heartbeat: connection dead');
      cleanup();
      scheduleReconnect();
      return;
    }
    try {
      ws.send(JSON.stringify({ type: 'ping' }));
    } catch {
      cleanup();
      scheduleReconnect();
    }
  }, 10000); // Every 10s
}

function stopHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

// == Inject Content Scripts Into All Tabs ==

async function injectAllTabs() {
  try {
    const tabs = await browser.tabs.query({});
    let injected = 0, skipped = 0;
    for (const tab of tabs) {
      if (isRestrictedUrl(tab.url)) {
        skipped++;
        continue;
      }
      try {
        await browser.tabs.executeScript(tab.id, { file: '/content/content.js' });
        injected++;
      } catch {
        skipped++; // CSP or other restriction
      }
    }
    console.log(`[Kitsune] Injected content script: ${injected} tabs, ${skipped} skipped`);
  } catch (e) {
    console.error('[Kitsune] Tab injection error:', e);
  }
}

function isRestrictedUrl(url) {
  if (!url) return true;
  if (url === 'about:blank') return false;  // blank tabs may have loaded content - let sendMessage decide
  return url.startsWith('about:') ||
         url.startsWith('moz-extension:') ||
         url.startsWith('chrome:') ||
         url.startsWith('resource:') ||
         url.startsWith('view-source:') ||
         url.startsWith('data:');
}

// == Command Handler ==

// Actions whose success returns a snapshot, and the tool named in a later stale_ref.
const POST_SNAPSHOT_ACTIONS = new Map([
  ['click', 'browser_click'], ['type', 'browser_type'], ['fill', 'browser_type'],
  ['selectOption', 'browser_select_option'], ['checkBox', 'browser_check'], ['keypress', 'browser_keypress'],
  ['submitForm', 'browser_submit_form'], ['navigate', 'browser_navigate'],
]);
// These usually load another document, so their snapshot is full. The others return a delta
// (see getSnapshot) unless the command passes snapshot: 'full'.
const FULL_SNAPSHOT_ACTIONS = new Set(['submitForm', 'navigate']);
// Page actions on one existing tab (the bridge's outcome actions) report opened_tabs.
const OPENER_ACTIONS = new Set([
  'click', 'doubleClick', 'type', 'fill', 'setEditableContent', 'selectOption', 'checkBox',
  'keypress', 'submitForm', 'formFill', 'hover', 'scroll', 'drag', 'focusElement', 'blurElement',
  'navigate', 'goBack', 'goForward', 'reloadTabBrowser', 'clickAndWaitNavigation',
]);
// Writes and navigations that back off from a tab the user is using (see userActivity): the page
// actions above and these. Element and full-page captures scroll the page.
const USER_GUARDED_ACTIONS = new Set([
  ...OPENER_ACTIONS, 'media', 'executeJS', 'uploadCommit',
  'closeTab', 'switchTab', 'focusWindow', 'elementScreenshot', 'fullPageScreenshot',
]);
// A tab opened by a link starts on about:blank; wait this long for its real URL.
const OPENED_TAB_URL_WAIT_MS = 1000;
// How long a click on a target=_blank link waits for its tab before openBlockedLink opens it.
const BLOCKED_LINK_WAIT_MS = 500;

async function handleCommand(command) {
  const params = { ...command };
  // Descriptions are audit metadata only; never use them to locate elements.
  if (params.ref) params.selector = params.ref;
  if (params.action === 'drag') {
    if (params.ref) params.from = params.ref;
    if (params.toRef) params.to = params.toRef;
  }
  const withSnapshot = POST_SNAPSHOT_ACTIONS.has(params.action) && params.includeSnapshot !== false;
  const deltaSnapshot = withSnapshot && params.snapshot !== 'full' && !FULL_SNAPSHOT_ACTIONS.has(params.action);
  // The content script records the delta's baseline right before it acts.
  if (deltaSnapshot) params.deltaBaseline = true;
  // Text snapshots report which tab they read.
  const textSnapshot = params.action === 'getSnapshot' && !!params.text;
  const tracksOpenedTabs = OPENER_ACTIONS.has(params.action);
  // A trusted click's screenPoint (see screenPointForClick) acts on the page like a click.
  const guarded = USER_GUARDED_ACTIONS.has(params.action) || (params.action === 'screenPoint' && !!params.click);
  const touches = guarded && params.action !== 'focusWindow';
  if ((withSnapshot || textSnapshot || touches) && params.tabId == null) params.tabId = await getActiveTabId();
  if (guarded) {
    const busy = await userActiveError(await guardedTabId(params.action, params));
    if (busy) return busy;
  }
  if (tracksOpenedTabs && params.tabId != null) actionStarts.set(params.tabId, tabsCreated);
  const touched = touches ? params.tabId : null;
  if (touched != null) touchStart(touched);
  try {
    return await runCommand(params, { withSnapshot, deltaSnapshot, textSnapshot, tracksOpenedTabs });
  } finally {
    if (touched != null) touchEnd(touched);
  }
}

async function runCommand(params, { withSnapshot, deltaSnapshot, textSnapshot, tracksOpenedTabs }) {
  const result = await dispatchCommand(params);
  const blankLink = result?.targetBlankLink;
  if (blankLink) delete result.targetBlankLink;
  if (textSnapshot && result && !result.error) result.tabId = params.tabId;
  if (withSnapshot && result && !result.error && result.ok !== false) {
    try {
      const snapshot = await forwardToContent('getSnapshot', {
        tabId: params.tabId, maxChars: 4000, focusLastAction: params.action !== 'navigate',
        observation: POST_SNAPSHOT_ACTIONS.get(params.action),
        ...(params.snapshotText ? { text: true } : {}),
        ...(deltaSnapshot ? { delta: true } : {}),
      });
      if (snapshot.error) result.snapshot_error = snapshot.error;
      else {
        result.snapshot = snapshot.snapshot;
        if (snapshot.delta) {
          result.snapshot_delta = true;
          // A same-document url or title change (history.pushState) is part of the delta.
          if (snapshot.url !== undefined) result.url = snapshot.url;
          if (snapshot.title !== undefined) result.title = snapshot.title;
        }
        if (snapshot.truncated) result.snapshot_truncated = true;
        if (snapshot.target_ref) result.target_ref = snapshot.target_ref;
      }
    } catch (e) { result.snapshot_error = e.message; }
  }
  if (blankLink && params.tabId != null) await openBlockedLink(params.tabId, blankLink, result);
  // Checked after the snapshot, which gives a link's new tab time to be created.
  if (tracksOpenedTabs && params.tabId != null && result && typeof result === 'object') {
    const opened = await getOpenedTabs(params.tabId, OPENED_TAB_URL_WAIT_MS);
    if (opened.opened_tabs.length) result.opened_tabs = opened.opened_tabs;
  }
  return result;
}

// Tabs opened by tabId since its last page action started, with their current URLs.
async function getOpenedTabs(tabId, urlWait = 0) {
  const since = actionStarts.get(tabId);
  if (since == null) return { opened_tabs: [] };
  const entries = openedTabs.filter(t => t.openerTabId === tabId && t.seq > since);
  const tabs = await Promise.all(entries.map(async ({ tabId: id }) => {
    let url;
    try { url = (await browser.tabs.get(id)).url; } catch { return null; }
    if (url === 'about:blank' && urlWait > 0) {
      const r = await waitForTabProperty('url', { tabId: id, pattern: '^(?!about:blank$)', timeout: urlWait });
      if (r.ok) url = r.url;
      else if (r.ok !== false) return null; // closed while waiting
    }
    return { tabId: id, url, ...(linkFallbackTabs.has(id) ? { via: 'link_fallback' } : {}) };
  }));
  return { opened_tabs: tabs.filter(Boolean) };
}

// A user's click on a target=_blank link opens its tab even under the popup blocker, which
// drops the tab of a synthetic click. When no tab comes, open the link's own URL from the
// clicked tab, as the click would have; a rel=opener link needs the page's window.opener,
// so it only gets a popup_blocked warning.
async function openBlockedLink(tabId, link, result) {
  const since = actionStarts.get(tabId);
  const opened = () => openedTabs.some(t => t.openerTabId === tabId && t.seq > since);
  for (let waited = 0; !opened() && waited < BLOCKED_LINK_WAIT_MS; waited += 50) {
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  if (opened()) return;
  if (!link.opener) {
    try {
      const clicked = await browser.tabs.get(tabId);
      const tab = await browser.tabs.create({
        url: link.url, openerTabId: tabId, windowId: clicked.windowId, cookieStoreId: clicked.cookieStoreId,
        active: false,
      });
      linkFallbackTabs.add(tab.id);
      markAgentTab(tab.id);
      return;
    } catch {}
  }
  result.warning ??= { code: 'popup_blocked', state: { url: link.url },
    hint: 'The link opens in a new tab, but no tab opened; the popup blocker likely dropped this ' +
      'synthetic click. Open state.url with browser_tabs action=new, or click with browser_trusted_click.' };
}

async function dispatchCommand(command) {
  const { action, id, ...params } = command;
  
  switch (action) {
    // Browser-level commands (handled here)
    case 'screenshot':   return await captureScreenshot(params);
    case 'getTabs':      return await getOpenTabs();
    case 'waitForUrl':   return await waitForTabProperty('url', params);
    case 'waitForTitle': return await waitForTabProperty('title', params);
    case 'getOpenedTabs': return await getOpenedTabs(params.tabId);
    case 'navigate':     return await navigateTab(params.url, params.tabId, params.expectTitle);
    case 'switchTab':    return await switchTab(params.tabId);
    case 'newTab':       return await createTab(params.url, params);
    case 'closeTab':     return await closeTab(params.tabId, params);
    case 'restoreActiveTab': return await restoreActiveTab(params.tabId, params.from);
    case 'screenPoint':  return params.click ? await screenPointForClick(params) : await forwardToContent(action, params);
    case 'wakeTab':      return await wakeTab(params.tabId);
    case 'reloadExtension': return reloadExtensionAction();
    case 'getPageTextFromTab': return await getPageText(params);

    // 1.4.0 browser-level additions
    case 'pinTab':           return await updateTab(params.tabId, { pinned: !!params.pinned });
    case 'muteTab':          return await updateTab(params.tabId, { muted: !!params.muted });
    case 'duplicateTab':     return await duplicateTab(params.tabId);
    case 'reloadTabBrowser': return await reloadTabBrowser(params.tabId, params.bypassCache);
    case 'goBack':
    case 'goForward':        return await goInHistory(params.tabId, action);
    case 'getZoom':          return await getZoom(params.tabId);
    case 'setZoom':          return await setZoom(params.tabId, params.factor);
    case 'getWindows':       return await getWindowsList();
    case 'createWindow':     return await createWindow(params.url, params.incognito);
    case 'closeWindow':      return await closeWindow(params.windowId, params.userTabApproved);
    case 'focusWindow':      return await focusWindow(params.windowId);
    case 'moveTab':          return await moveTabAction(params.tabId, params.windowId, params.index);
    case 'detachTab':        return await detachTab(params.tabId);
    case 'elementScreenshot':return await elementScreenshot(params.tabId, params.selector, params.scale, params.imagePreview);
    case 'fullPageScreenshot': return await fullPageScreenshot(params.tabId, params.imagePreview);
    case 'cookies':          return await cookiesOp(params.op, params.url, params.name, params.value, params.domain, params.path, params.secure);
    case 'clipboard':        return await clipboardOp(params.op, params.text);
    case 'downloads':        return await downloadsOp(params.op, params.url, params.filename, params.query);
    case 'clearBrowsingData':return await clearBrowsingData(params.types, params.since);
    case 'intercept':        return await interceptOp(params.op, params.patterns, params.effect);
    case 'clickAndWaitNavigation': return await clickAndWaitNavigation(params.tabId, params.selector, params.timeout, params.first);
    case 'describeTarget':   return await describeTarget(params);
    // history.js (#140)
    case 'historySearch':    return await historySearch(params);
    case 'historyVisits':    return await historyVisits(params);
    case 'bookmarksSearch':  return await bookmarksSearch(params);

    // Content script commands (forwarded to active tab)
    case 'handleDialog': {
      const tabId = params.tabId || await getActiveTabId();
      const result = await forwardToContent('handleDialog', { ...params, tabId });
      if (!result.error) {
        if (params.accept == null) dialogPolicies.delete(tabId);
        else dialogPolicies.set(tabId, { accept: params.accept, prompt_text: params.prompt_text });
      }
      return result;
    }
    case 'getPageText':  return await getPageText(params);
    case 'waitForText':
    case 'getPageInfo':
    case 'getSnapshot':
    case 'getAccessibilityTree':
    case 'getFormFields':
    case 'click':
    case 'type':
    case 'setEditableContent':
    case 'scroll':
    case 'hover':
    case 'fill':
    case 'find':
    case 'executeJS':
    case 'highlight':
    case 'clearHighlight':
    case 'waitForElement':
    case 'waitForResult':
    // 1.4.0 forwarded actions
    case 'query':
    case 'getHTML':
    case 'getLinks':
    case 'getImages':
    case 'getMeta':
    case 'getStructuredData':
    case 'markScreenshotTarget':
    case 'unmarkScreenshotTarget':
    case 'getBounds':
    case 'getComputedStyle':
    case 'getReadability':
    case 'getMarkdown':
    case 'selectOption':
    case 'checkBox':
    case 'focusElement':
    case 'blurElement':
    case 'keypress':
    case 'doubleClick':
    case 'submitForm':
    case 'formFill':
    case 'media':
    case 'drag':
    case 'waitForNetworkIdle':
    case 'captureNetwork':
    case 'watchConsole':
    case 'consoleLogs':
    case 'storageOp':
    case 'getIframes':
    case 'explainSelector':
    case 'fullPageMetrics':
    case 'uploadChunk':
    case 'uploadCommit':
    case 'uploadAbort':
    case 'keepAlive':
    case 'neuterUnload':
      return await forwardToContent(action, params);

    default:
      return { error: `Unknown action: ${action}` };
  }
}

// == Browser Commands ==

// Tab metadata survives document replacement; never send these waits to content.
async function waitForTabProperty(property, { tabId, pattern, timeout = 10000 }) {
  try {
    const re = new RegExp(pattern);
    if (!Number.isFinite(timeout) || timeout < 0) {
      return { error: 'timeout must be non-negative milliseconds' };
    }
    const targetTabId = tabId ?? await getActiveTabId();
    if (targetTabId == null) return noActiveTab('No active tab found');
    const start = Date.now();
    return await new Promise(resolve => {
      let settled = false;
      let value = '';
      let revision = 0;
      const finish = result => {
        if (settled) return;
        settled = true;
        clearTimeout(deadline);
        clearInterval(poll);
        browser.tabs.onUpdated.removeListener(onUpdated);
        browser.tabs.onRemoved.removeListener(onRemoved);
        resolve(result);
      };
      const checkTab = tab => {
        value = tab[property] || '';
        if (re.test(value)) {
          finish({ ok: true, [property]: value, elapsed: Date.now() - start });
        }
      };
      const check = async () => {
        const currentRevision = ++revision;
        try {
          const tab = await browser.tabs.get(targetTabId);
          if (!settled && currentRevision === revision) checkTab(tab);
        } catch (e) {
          if (!settled && currentRevision === revision) finish({ error: e.message });
        }
      };
      function onUpdated(updatedTabId, changeInfo, tab) {
        if (updatedTabId !== targetTabId || settled) return;
        // Ignore older in-flight reads once a newer tab update is available.
        revision++;
        if (tab && tab[property] != null) checkTab(tab);
        else check();
      }
      function onRemoved(removedTabId) {
        if (removedTabId === targetTabId) finish({ error: 'Tab closed while waiting for ' + property });
      }
      const deadline = setTimeout(() => {
        finish({ ok: false, [property]: value, error: 'Timed out' });
      }, timeout);
      const poll = setInterval(check, 200);
      // Subscribe before the initial read so navigation in that gap is not lost.
      browser.tabs.onUpdated.addListener(onUpdated);
      browser.tabs.onRemoved.addListener(onRemoved);
      check();
    });
  } catch (e) {
    return { error: e.message };
  }
}

// tabs.captureTab renders a tab whether or not it is shown, so no capture activates a tab.
async function captureScreenshot(params = {}) {
  try {
    const tab = params.tabId != null ? await browser.tabs.get(params.tabId)
      : (await browser.tabs.query({ active: true, currentWindow: true }))[0];
    if (!tab) return noActiveTab('No active tab found');
    const dataUrl = await browser.tabs.captureTab(tab.id, { format: 'png', quality: 90 });
    return {
      dataUrl: params.imagePreview ? await imagePreview(dataUrl) : dataUrl,
      url: tab.url || '',
      title: tab.title || '',
      width: tab.width || 0,
      height: tab.height || 0
    };
  } catch (e) {
    return { error: `Screenshot failed: ${e.message}` };
  }
}

async function getOpenTabs() {
  try {
    const tabs = await browser.tabs.query({});
    return {
      tabs: tabs.map(t => ({
        id: t.id, url: t.url, title: t.title,
        active: t.active, windowId: t.windowId, index: t.index, owned: agentTabs.has(t.id)
      }))
    };
  } catch (e) {
    return { error: e.message };
  }
}

function urlWithoutFragment(url) {
  try { return new URL(url).href.split('#')[0]; } catch { return null; }
}

// Runs startNavigation and resolves true once the tab finishes loading, false on timeout.
// Subscribes first so a fast 'complete' is not missed. Gecko may never emit a fresh
// 'complete' (e.g. reloading a page with iframes in headed Zen), so tab.status is also
// polled: a reload accepts any 'complete', other loads only one seen after 'loading'.
function waitForTabLoad(tabId, startNavigation, { reload = false, timeout = 15000 } = {}) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let sawLoading = false;
    let poll = null;
    const finish = (loaded, error) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      clearInterval(poll);
      browser.tabs.onUpdated.removeListener(listener);
      if (error) reject(error);
      else resolve(loaded);
    };
    const isLoaded = tab => tab.status === 'complete' && tab.url && !tab.url.startsWith('about:');
    function listener(updatedTabId, changeInfo, tab) {
      if (updatedTabId !== tabId || settled) return;
      if (changeInfo.status === 'loading') sawLoading = true;
      if (changeInfo.status === 'complete' && isLoaded(tab)) finish(true);
    }
    const check = async () => {
      try {
        const tab = await browser.tabs.get(tabId);
        if (settled) return;
        if (tab.status === 'loading') sawLoading = true;
        else if ((sawLoading || reload) && isLoaded(tab)) finish(true);
      } catch (e) {
        finish(false, e);
      }
    };
    const deadline = setTimeout(() => finish(false), timeout);
    browser.tabs.onUpdated.addListener(listener);
    Promise.resolve().then(startNavigation).then(() => {
      if (!settled) poll = setInterval(check, 200);
    }, e => finish(false, e));
  });
}

async function navigateTab(url, tabId, expectTitle) {
  let targetTabId = null;
  try {
    targetTabId = tabId || (await getActiveTabId());
    if (!targetTabId) return noActiveTab('No active tab');
    const target = urlWithoutFragment(url);
    const sameUrl = target !== null && target === urlWithoutFragment((await browser.tabs.get(targetTabId)).url);

    let loaded = true;
    if (sameUrl && url.includes('#')) {
      // Same-document (fragment) navigation: no new load, so no status 'complete'.
      await browser.tabs.update(targetTabId, { url });
    } else {
      loaded = await waitForTabLoad(targetTabId, () => browser.tabs.update(targetTabId, { url }), { reload: sameUrl });
    }

    // Check if page title matches expectation (catches silent redirects)
    const result = loaded ? { ok: true, url } : { ok: false, timedOut: true, url };
    if (expectTitle) {
      const tab = await browser.tabs.get(targetTabId);
      const title = tab.title || '';
      const match = title.toLowerCase().includes(expectTitle.toLowerCase());
      result.title = title;
      if (!match) {
        result.warning = `Title "${title}" does not contain expected "${expectTitle}" — possible redirect`;
        result.redirected = true;
      }
    }
    return result;
  } catch (e) {
    return await tabApiError(targetTabId, e);
  }
}

// Switches the user's view: only for an explicit request (browser_tabs select).
async function switchTab(tabId) {
  try {
    await browser.tabs.update(tabId, { active: true });
    return { ok: true };
  } catch (e) {
    return await tabApiError(tabId, e);
  }
}

// Opens an agent-owned tab in the background. Immediate by default. With wait, resolves once the
// tab loads (or the timeout passes, leaving the tab open) and reports the actual URL, so
// redirects are visible.
async function createTab(url, { wait = false, timeout = 15000 } = {}) {
  try {
    const target = url || 'about:blank';
    const tab = await browser.tabs.create({ url: target, active: false });
    markAgentTab(tab.id);
    if (!wait) return { ok: true, tabId: tab.id };
    // A new tab has no previous document, so any non-about: 'complete' is the destination.
    const loaded = target.startsWith('about:')
      || await waitForTabLoad(tab.id, () => {}, { reload: true, timeout });
    const current = await browser.tabs.get(tab.id);
    return {
      ok: loaded, ...(loaded ? {} : { timedOut: true }),
      tabId: tab.id, url: current.url, status: current.status,
    };
  } catch (e) {
    return { error: e.message };
  }
}

// How long a forced close waits for the page to disarm beforeunload; a hung page still closes.
const NEUTER_UNLOAD_WAIT_MS = 8000;

// Closes a tab the agent owns. A user tab needs userTabApproved, which only the bridge sets, after
// the user approved that close with the confirm token of its user_tab refusal.
async function closeTab(tabId, { force = false, userTabApproved = false } = {}) {
  try {
    if (!agentTabs.has(tabId) && !userTabApproved) {
      const tab = await browser.tabs.get(tabId);
      return tabError(`Tab ${tabId} was not opened by the agent, so it was not closed`, 'user_tab',
        { tabId, url: tab.url, title: tab.title }, false);
    }
    if (force) {
      let timer;
      await Promise.race([forwardToContent('neuterUnload', { tabId }).catch(() => {}),
        new Promise(resolve => { timer = setTimeout(resolve, NEUTER_UNLOAD_WAIT_MS); })]);
      clearTimeout(timer);
    }
    await browser.tabs.remove(tabId);
    return { ok: true };
  } catch (e) {
    return await tabApiError(tabId, e);
  }
}

// A trusted OS click lands on whatever the screen shows, so a target tab that is not shown is
// activated for the click; the bridge restores previousTabId right after it (restoreActiveTab).
const TRUSTED_CLICK_PAINT_MS = 150;
async function screenPointForClick(params) {
  let previousTabId = null;
  try {
    const tab = await browser.tabs.get(params.tabId);
    if (!tab.active) {
      const [shown] = await browser.tabs.query({ windowId: tab.windowId, active: true });
      const busy = shown && await userActiveError(shown.id);
      if (busy) return busy;
      previousTabId = shown?.id ?? null;
      await browser.tabs.update(tab.id, { active: true });
      await new Promise(resolve => setTimeout(resolve, TRUSTED_CLICK_PAINT_MS));
    }
  } catch (e) {
    return await tabApiError(params.tabId, e);
  }
  let point;
  try {
    point = await forwardToContent('screenPoint', params);
    if (previousTabId == null || point.error) return point;
    return { ...point, tabId: params.tabId, previousTabId };
  } finally {
    if (previousTabId != null && (!point || point.error)) {
      await restoreActiveTab(previousTabId, params.tabId);
    }
  }
}

// Shows the user's tab again after a trusted click, unless the user already moved elsewhere.
async function restoreActiveTab(tabId, from) {
  try {
    const shown = await browser.tabs.get(from);
    if (!shown.active) return { ok: true, restored: false };
    await browser.tabs.update(tabId, { active: true });
    return { ok: true, restored: true };
  } catch (e) {
    return await tabApiError(tabId, e);
  }
}

function reloadExtensionAction() {
  // Defer the reload so we can ack first — the WS dies the moment we reload,
  // and the bridge's pending future would otherwise hang. The new background
  // script comes up cold and reconnects via the existing onclose backoff.
  setTimeout(() => {
    try { browser.runtime.reload(); } catch (e) { console.error('[Kitsune] Reload failed:', e); }
  }, 100);
  return { ok: true, reloading: true };
}

// How long wakeTab waits for a woken tab's page to answer the content script, polling this often.
const WAKE_TAB_TIMEOUT_MS = 15000;
const WAKE_TAB_POLL_MS = 100;

async function wakeTab(tabId) {
  // Reload a discarded tab and wait until its page can be scripted. Idempotent — returns
  // immediately if the tab is already alive. Use before sending commands to a
  // tab that may have been idle long enough for Zen's Tab Unloader to discard.
  if (!tabId) return { error: 'wakeTab requires tabId' };
  try {
    const tab = await browser.tabs.get(tabId);
    if (!tab.discarded) {
      return { ok: true, woken: false, discarded: false };
    }
    markReload(tabId);
    const since = Date.now();
    const deadline = since + WAKE_TAB_TIMEOUT_MS;
    await browser.tabs.reload(tabId);
    while (true) {
      const page = await wokenPageState(tabId, since);
      if (page.ready) return { ok: true, woken: true, ready: true };
      if (page.unscriptable) return { ok: true, woken: true, ready: false, unscriptable: true, state: page.state };
      if (Date.now() >= deadline) {
        return { ok: true, woken: true, ready: false, timedOut: true, state: await tabState(tabId) };
      }
      await new Promise(r => setTimeout(r, WAKE_TAB_POLL_MS));
    }
  } catch (e) {
    return { error: e.message };
  }
}

// Gecko first restores a woken tab's discarded document as a placeholder, reporting it complete
// at about:blank or with the tab's URL and title before the reload replaces it; a command that
// reached it would be lost with it. So the woken page counts only once the tab reported loading
// after the wake (`since`), then complete. It is ready when it answers the content script and
// is still that complete load afterwards; it is final (unscriptable) when it refuses injection.
async function wokenPageState(tabId, since) {
  const tab = await browser.tabs.get(tabId);
  const load = tabLoads.get(tabId);
  if (tab.discarded || tab.status !== 'complete' || !(load?.at >= since && load.complete)) return {};
  try {
    await ensureContentScript(tabId);
  } catch {
    const httpStatus = await waitForHttpStatus(tabId, tab.url, since);
    return { unscriptable: true,
      state: { ...await tabState(tabId), title: tab.title, ...(httpStatus ? { httpStatus } : {}) } };
  }
  const after = await browser.tabs.get(tabId);
  return { ready: after.status === 'complete' && tabLoads.get(tabId) === load };
}

// == Forward to Content Script ==

// Error contract (see docs/api-reference.md#action-errors): `error` keeps its text; code,
// retryable, hint and state are machine-readable. retryable is false when the message may
// have reached the page, so its outcome is unknown; the bridge relaxes that for reads.
// error_page is never retryable: nothing reached the page, but repeating cannot succeed.
const TAB_FAILURE_HINTS = {
  tab_closed: 'The tab no longer exists; list tabs with browser_tabs and target an open tab.',
  tab_unloaded: 'The tab was unloaded; wake it with browser_wake_tab, then take a new browser_snapshot.',
  navigating: 'The tab is loading a new document; wait for it with browser_wait_for, then take a new browser_snapshot.',
  content_unavailable: 'The page did not answer; inspect it with browser_snapshot before repeating the action.',
  restricted_page: 'Browser-internal pages cannot be scripted; navigate the tab to a web page or target another tab.',
  error_page: 'The page has no scriptable document (often an error response with an empty body), so reading it again fails the same way; check state.httpStatus and state.title, then navigate the tab elsewhere or target another tab.',
  host_permission_denied: 'Firefox does not let Kitsune script this site. For state.reason not_granted, allow its site access in about:addons → Kitsune → Permissions; restricted_domain sites (addons.mozilla.org, …) are closed to all extensions, so use another tab.',
  user_tab: 'The agent did not open this tab (owned: false in browser_tabs list), so it stays open. Leave it, ' +
    'or ask the user and repeat a direct browser_tabs close with the confirm_token that call returns.',
  user_active: 'The user is using this tab (state.reason); nothing was done. Retry in a few seconds, ' +
    'work in a tab you opened, or ask the user.',
  // Matches the bridge's browser_window_unavailable diagnosis hint.
  browser_window_unavailable: 'Open a normal browser window in the profile where Kitsune is installed (private windows need Run in Private Windows in about:addons), or pass tab_id.',
};

// Without an active tab Kitsune sees no usable window: all are closed, or only private windows
// it may not use are open.
const noActiveTab = message => tabError(message, 'browser_window_unavailable');

// Mozilla's default extensions.webextensions.restrictedDomains: no extension may script them.
const RESTRICTED_DOMAINS = new Set([
  'accounts-static.cdn.mozilla.net', 'accounts.firefox.com', 'addons.cdn.mozilla.net', 'addons.mozilla.org',
  'api.accounts.firefox.com', 'content.cdn.mozilla.net', 'discovery.addons.mozilla.org', 'install.mozilla.org',
  'oauth.accounts.firefox.com', 'profile.accounts.firefox.com', 'support.mozilla.org', 'sync.services.mozilla.com',
]);

// Gecko refuses injection into a page's own error documents with the same message as into a
// site Kitsune may not script, so tell the latter apart by its host.
async function hostPermissionDenial(url) {
  const { protocol, hostname } = new URL(url);
  if (RESTRICTED_DOMAINS.has(hostname)) return 'restricted_domain';
  const granted = await browser.permissions?.contains({ origins: [`${protocol}//${hostname}/*`] })
    .catch(() => true);
  return granted === false ? 'not_granted' : null;
}

async function tabState(tabId) {
  try {
    const tab = await browser.tabs.get(tabId);
    return { tabId, url: tab.url, status: tab.status, ...(tab.discarded ? { discarded: true } : {}) };
  } catch {
    return null;
  }
}

function tabError(message, code, state, retryable = true) {
  return { error: message, code, retryable, hint: TAB_FAILURE_HINTS[code], state };
}

async function tabFailure(tabId, message, dispatched) {
  const state = await tabState(tabId);
  if (!state) return tabError(message, 'tab_closed', { tabId });
  const code = state.discarded ? 'tab_unloaded' : state.status === 'loading' ? 'navigating' : 'content_unavailable';
  return tabError(message, code, state, !dispatched);
}

// The status of the tab's url, from a request started at or after `since`.
function httpStatusFor(tabId, url, since = -Infinity) {
  const seen = mainFrameStatus.get(tabId);
  return seen && seen.at >= since && seen.url.split('#')[0] === url.split('#')[0] ? seen.statusCode : undefined;
}

// The tab's document is about to be replaced: forget its status, so only the new response counts.
function markReload(tabId) {
  mainFrameStatus.delete(tabId);
  tabReloads.set(tabId, Date.now());
}

// How long after a wake or reload a refused injection may still be the old document.
const RELOAD_SETTLE_MS = 5000;

// Firefox can deliver a response's webRequest events after tabs reports its page complete
// (3-20 ms late, mostly on the first loads after the browser starts).
const MAIN_FRAME_STATUS_WAIT_MS = 1000;

// Resolves the status recorded for the tab's url (see httpStatusFor), waiting up to
// MAIN_FRAME_STATUS_WAIT_MS for it to be recorded; undefined on timeout or when the tab closes.
function waitForHttpStatus(tabId, url, since = -Infinity) {
  const known = httpStatusFor(tabId, url, since);
  if (known !== undefined) return Promise.resolve(known);
  return new Promise(resolve => {
    if (!mainFrameStatusWaiters.has(tabId)) mainFrameStatusWaiters.set(tabId, new Set());
    const waiters = mainFrameStatusWaiters.get(tabId);
    const finish = status => {
      clearTimeout(deadline);
      waiters.delete(notify);
      if (!waiters.size) mainFrameStatusWaiters.delete(tabId);
      resolve(status);
    };
    const notify = closed => {
      const status = closed ? undefined : httpStatusFor(tabId, url, since);
      if (closed || status !== undefined) finish(status);
    };
    const deadline = setTimeout(() => finish(undefined), MAIN_FRAME_STATUS_WAIT_MS);
    waiters.add(notify);
  });
}

const isLoadedWebPage = state =>
  state && !state.discarded && state.status === 'complete' && /^https?:/i.test(state.url);

// An http(s) page that loaded but refuses injection is a document the browser rendered
// itself, such as an error response with an empty body or a PDF/JSON viewer; retrying cannot
// reach it. It may instead be an old document about to be replaced (navigating): within
// RELOAD_SETTLE_MS of a wake or reload, unless a response since then is an error, and when a
// 2xx response only arrives while the status is awaited.
async function injectionFailure(tabId, e) {
  const message = `Cannot inject content script: ${e.message}`;
  let state = await tabState(tabId);
  if (!isLoadedWebPage(state)) return await tabFailure(tabId, message, false);
  const denied = await hostPermissionDenial(state.url);
  if (denied) return tabError(message, 'host_permission_denied', { ...state, reason: denied });
  const reloadedAt = tabReloads.get(tabId);
  const settling = Date.now() - reloadedAt < RELOAD_SETTLE_MS;
  const since = settling ? reloadedAt : -Infinity;
  const known = httpStatusFor(tabId, state.url, since);
  if (known === undefined) {
    await waitForHttpStatus(tabId, state.url, since);
    state = await tabState(tabId);
    if (!isLoadedWebPage(state)) return await tabFailure(tabId, message, false);
  }
  const httpStatus = httpStatusFor(tabId, state.url, since);
  const withStatus = httpStatus ? { httpStatus } : {};
  if (!(httpStatus >= 400) && (settling || (known === undefined && httpStatus !== undefined))) {
    return tabError(message, 'navigating', { ...state, ...withStatus });
  }
  const { title } = await browser.tabs.get(tabId).catch(() => ({}));
  const page = httpStatus >= 400 ? `an HTTP ${httpStatus} error response` : 'a page';
  return tabError(`The tab shows ${page} with no scriptable document. ${message}`, 'error_page',
    { ...state, title, ...withStatus }, false);
}

// An error response has no text of its own; report it as an empty page.
async function getPageText(params) {
  const result = await forwardToContent('getPageText', params);
  if (result.code !== 'error_page' || params.selector || !(result.state.httpStatus >= 400)) return result;
  const { tabId, url, title, httpStatus } = result.state;
  return { text: '', empty: true, httpStatus, tabId, url, title };
}

// Tab APIs fail opaquely; name the common case of a tab that no longer exists.
async function tabApiError(tabId, e) {
  if (tabId != null && !(await tabState(tabId))) return tabError(e.message, 'tab_closed', { tabId });
  return { error: e.message };
}

// A target that vanished while the tab loads is a navigation race, not a missing element.
async function withTabState(tabId, response) {
  const state = { ...response.state, ...(await tabState(tabId) || { tabId }) };
  if (state.status === 'loading' && (response.code === 'not_found' || response.code === 'stale_ref')) {
    // The loading document replaces any current ref the stale_ref suggested.
    const { currentRef, ...rest } = state;
    return { ...response, code: 'navigating', hint: TAB_FAILURE_HINTS.navigating,
      state: { ...rest, cause: response.code } };
  }
  return { ...response, state };
}

// Injects the content script unless the page already runs the current one; throws if it cannot.
async function ensureContentScript(tabId) {
  try {
    const ping = await browser.tabs.sendMessage(tabId, { action: 'ping' });
    if (ping && ping.version >= EXPECTED_CONTENT_VERSION) return;
  } catch {}
  await browser.tabs.executeScript(tabId, { file: '/content/content.js' });
  await new Promise(r => setTimeout(r, 150));
}

const MISSING_CONTENT_RECEIVER = 'Could not establish connection. Receiving end does not exist.';

// Public refs never reuse an index. Keep the generation-qualified ref private so the
// content script can still distinguish replaced snapshots and other documents.
const shortRefs = new Map();
const publicRefs = new Map();
// Retired aliases still answer other_document, not not_in_snapshot; bounded, oldest first.
const retiredShortRefs = new Set();
const RETIRED_SHORT_REFS_MAX = 4096;
// Retire aliases with their document/tab, without resetting the persisted monotonic counter.
function pruneShortRefs(tabId) {
  for (const [short, entry] of shortRefs) {
    if (entry.tabId !== tabId) continue;
    shortRefs.delete(short);
    publicRefs.delete(`${tabId}:${entry.ref}`);
    retiredShortRefs.add(short);
  }
  for (const short of retiredShortRefs) {
    if (retiredShortRefs.size <= RETIRED_SHORT_REFS_MAX) break;
    retiredShortRefs.delete(short);
  }
}
// The params that may carry a ref, and a public ref's spelling.
const REF_PARAMS = ['ref', 'selector', 'from', 'to', 'toRef'];
const isShortRef = value => typeof value === 'string' && /^e\d+$/.test(value);
const REF_RESULT_ACTIONS = new Set([
  'getSnapshot', 'getAccessibilityTree', 'getFormFields', 'find', 'waitForElement',
  'getIframes', 'submitForm',
]);
let nextShortRef = null;
let shortRefEnd = 0n;
let shortRefQueue = Promise.resolve();

async function reserveShortRefs(count) {
  if (nextShortRef === null) {
    const stored = await browser.storage.local.get('shortRefEnd');
    const end = stored.shortRefEnd ?? '0';
    if (!/^\d+$/.test(end)) throw new Error('Invalid short-ref counter');
    nextShortRef = shortRefEnd = BigInt(end);
  }
  if (nextShortRef + BigInt(count) > shortRefEnd) {
    const end = nextShortRef + BigInt(Math.max(1024, count));
    // Reserve before issuing: a background reload loses the maps but skips every
    // index it might have issued, including a partially used reservation.
    await browser.storage.local.set({ shortRefEnd: end.toString() });
    shortRefEnd = end;
  }
}

function shortRefError(ref, reason) {
  const hint = reason === 'other_document'
    ? 'The ref belongs to another document: the tab navigated or a different tab is targeted. ' +
      'Take a new browser_snapshot of the intended tab (pass tab_id).'
    : 'This ref is unknown or expired; take a new browser_snapshot and use its refs.';
  return { error: 'stale ref, call browser_snapshot again', code: 'stale_ref', retryable: true,
    hint, state: { reason, ref } };
}

function expandShortRefs(params, tabId) {
  const expanded = { ...params };
  for (const key of REF_PARAMS) {
    const ref = params[key];
    if (!isShortRef(ref)) continue;
    const entry = shortRefs.get(ref);
    if (!entry) return { error: shortRefError(ref, retiredShortRefs.has(ref) ? 'other_document' : 'not_in_snapshot') };
    if (entry.tabId !== tabId) return { error: shortRefError(ref, 'other_document') };
    expanded[key] = entry.ref;
  }
  return { params: expanded };
}

function compactContentRefs(action, response, tabId, params) {
  // Only protocol-owned fields are rewritten, never page text, JS results or values.
  const visit = (value, convert) => {
    if (Array.isArray(value)) return value.map(item => visit(item, convert));
    if (!value || typeof value !== 'object') return value;
    return Object.fromEntries(Object.entries(value).map(([key, item]) => {
      if (key === 'snapshot' && typeof item === 'string') {
        return [key, item.replace(/\[ref=(s\d+e\d+)\]/g, (_, ref) => `[ref=${convert(ref)}]`)];
      }
      if (['ref', 'target_ref', 'currentRef'].includes(key) && typeof item === 'string' && /^s\d+e\d+$/.test(item)) {
        // An error echoes the caller's ref, including legacy refs, verbatim.
        if (response.code && value === response.state && key === 'ref') {
          const original = REF_PARAMS.map(k => params[k])
            .find(ref => shortRefs.get(ref)?.ref === item);
          return [key, original || item];
        }
        return [key, convert(item)];
      }
      return [key, visit(item, convert)];
    }));
  };
  const rewrite = async () => {
    if (!REF_RESULT_ACTIONS.has(action) && !response.code) return response;
    const missing = new Set();
    visit(response, ref => {
      if (!publicRefs.has(`${tabId}:${ref}`)) missing.add(ref);
      return ref;
    });
    if (missing.size) {
      await reserveShortRefs(missing.size);
      for (const ref of missing) {
        const short = `e${nextShortRef++}`;
        shortRefs.set(short, { tabId, ref });
        publicRefs.set(`${tabId}:${ref}`, short);
      }
    }
    return visit(response, ref => publicRefs.get(`${tabId}:${ref}`));
  };
  const pending = shortRefQueue.then(rewrite);
  shortRefQueue = pending.catch(() => {});
  return pending;
}

async function forwardToContent(action, params) {
  const tabId = params.tabId || (await getActiveTabId());
  if (!tabId) return noActiveTab('No active tab found');

  let retried = false;
  while (true) {
    // Verify the target tab before anything can reach a page, including a retry.
    const state = await tabState(tabId);
    if (!state) return tabError(`Tab ${tabId} no longer exists`, 'tab_closed', { tabId });
    if (isRestrictedUrl(state.url)) {
      return tabError(`Cannot access browser internal page: ${state.url.split('?')[0]}`, 'restricted_page', state);
    }

    try {
      await ensureContentScript(tabId);
    } catch (e) {
      return await injectionFailure(tabId, e);
    }

    const expanded = expandShortRefs(params, tabId);
    if (expanded.error) return await withTabState(tabId, expanded.error);
    let response;
    try {
      response = await browser.tabs.sendMessage(tabId, { action, ...expanded.params });
    } catch (e) {
      const missingReceiver = e.message === MISSING_CONTENT_RECEIVER;
      // A wake's receiver can disappear after the ping. Only this error proves nothing
      // reached the page: recheck/inject once, never replay a possibly dispatched action.
      if (missingReceiver && !retried) {
        retried = true;
        continue;
      }
      return await tabFailure(tabId, `Content script error: ${e.message}`, !missingReceiver);
    }
    if (!response) return await tabFailure(tabId, 'No response from content script', true);
    if (response.code) response = await withTabState(tabId, response);
    const result = await compactContentRefs(action, response, tabId, params);
    // Waits and drags echo their targets; keep their public spelling too.
    for (const key of REF_PARAMS) {
      if (!isShortRef(params[key])) continue;
      if (result[key] === expanded.params[key]) result[key] = params[key];
      if (typeof result.error === 'string') result.error = result.error.replaceAll(expanded.params[key], params[key]);
    }
    return result;
  }
}

async function getActiveTabId() {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  return tabs[0]?.id || null;
}

// ══════════════════════════════════════════════════════════════
// 1.4.0 — browser-level handler implementations
// ══════════════════════════════════════════════════════════════

// Runs act on tabId or the active tab; any failure is a bare error.
async function onTab(tabId, act) {
  try {
    const id = tabId || (await getActiveTabId());
    if (!id) return { error: 'No tab' };
    return await act(id);
  } catch (e) { return { error: e.message }; }
}

const updateTab = (tabId, props) => onTab(tabId, async id => {
  const tab = await browser.tabs.update(id, props);
  return { ok: true, tabId: id, pinned: tab.pinned, muted: tab.mutedInfo?.muted };
});

const duplicateTab = tabId => onTab(tabId, async id => {
  const dup = await browser.tabs.duplicate(id, { active: false });
  markAgentTab(dup.id);
  return { ok: true, tabId: dup.id, sourceTabId: id };
});

const reloadTabBrowser = (tabId, bypassCache) => onTab(tabId, async id => {
  markReload(id);
  await browser.tabs.reload(id, { bypassCache: !!bypassCache });
  return { ok: true, tabId: id, bypassCache: !!bypassCache };
});

// direction: 'goBack' or 'goForward'.
const goInHistory = (tabId, direction) => onTab(tabId, async id => {
  await browser.tabs[direction](id);
  return { ok: true, tabId: id };
});

const getZoom = tabId => onTab(tabId, async id => ({ ok: true, tabId: id, factor: await browser.tabs.getZoom(id) }));

const setZoom = (tabId, factor) => onTab(tabId, async id => {
  await browser.tabs.setZoom(id, Number(factor) || 1);
  return { ok: true, tabId: id, factor: Number(factor) || 1 };
});

async function getWindowsList() {
  try {
    const wins = await browser.windows.getAll({ populate: true });
    return {
      count: wins.length,
      windows: wins.map(w => ({
        id: w.id, focused: w.focused, state: w.state, type: w.type,
        width: w.width, height: w.height, left: w.left, top: w.top,
        incognito: w.incognito,
        tabs: (w.tabs || []).map(t => ({ id: t.id, url: t.url, title: t.title, active: t.active, pinned: t.pinned })),
      })),
    };
  } catch (e) { return { error: e.message }; }
}

async function createWindow(url, incognito) {
  try {
    let urls;
    if (url) urls = Array.isArray(url) ? url : [url];
    const { window: w, record } = await createAgentWindow({ url: urls, incognito: !!incognito }, urls || [],
      { windowCopies: true });
    // The tabs windows.create made are the agent's; the browser's copies are discarded (see Agent windows).
    const tabIds = [];
    for (const tab of w.tabs || []) {
      if (tab.discarded) {
        record.windowCopies.add(tab.id);
      } else {
        markAgentTab(tab.id);
        tabIds.push(tab.id);
      }
    }
    return { ok: true, windowId: w.id, tabIds };
  } catch (e) { return { error: e.message }; }
}

// Closing a window closes its tabs, so one with user tabs needs approval like closeTab.
async function closeWindow(windowId, userTabApproved = false) {
  try {
    const record = agentWindowRecord(windowId);
    const userTabs = (await browser.windows.get(windowId, { populate: true })).tabs
      .filter(tab => !agentTabs.has(tab.id) && !record?.windowCopies?.has(tab.id)).map(tab => tab.id);
    if (userTabs.length && !userTabApproved) {
      return tabError(`Window ${windowId} has tabs the agent did not open, so it was not closed`, 'user_tab',
        { windowId, tabIds: userTabs }, false);
    }
    if (!record) {
      await browser.windows.remove(windowId);
      return { ok: true, windowId };
    }
    // Closing: the copies Zen fills with the agent's page as the window closes stay the agent's.
    record.closing = true;
    await browser.windows.remove(windowId).catch(e => { record.closing = false; throw e; });
    agentWindows.delete(record);
    await closeSyncedCopies(record);
    return { ok: true, windowId };
  } catch (e) { return { error: e.message }; }
}

// Moves the user's focus: only for an explicit request (browser_focus_window).
async function focusWindow(windowId) {
  try {
    const agentWindow = agentWindowRecord(windowId);
    if (agentWindow) agentWindow.focusWindowId = null;
    await browser.windows.update(windowId, { focused: true });
    return { ok: true, windowId };
  } catch (e) { return { error: e.message }; }
}

async function moveTabAction(tabId, windowId, index) {
  try {
    const moved = await browser.tabs.move(tabId, { windowId, index: index ?? -1 });
    return { ok: true, tabId, windowId, index: Array.isArray(moved) ? moved[0]?.index : moved.index };
  } catch (e) { return { error: e.message }; }
}

async function detachTab(tabId) {
  try {
    const tab = await browser.tabs.get(tabId);
    const { window: w } = await createAgentWindow({ tabId }, [tab.url], { windowCopies: false });
    return { ok: true, windowId: w.id, tabId };
  } catch (e) { return { error: e.message }; }
}

// Bound inline image payloads without adding a Python imaging dependency.
async function imagePreview(dataUrl) {
  const limit = 950000; // base64 characters, leaving room for MCP metadata
  if (dataUrl.length <= limit) return dataUrl;
  const img = await loadDataUrl(dataUrl);
  let width = img.width, height = img.height;
  for (let i = 0; i < 16; i++) {
    const canvas = new OffscreenCanvas(Math.max(1, Math.round(width)), Math.max(1, Math.round(height)));
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await canvas.convertToBlob({ type: 'image/jpeg', quality: 0.8 });
    const result = arrayBufferToDataUrl(await blob.arrayBuffer(), 'image/jpeg');
    if (result.length <= limit) return result;
    width *= 0.75;
    height *= 0.75;
  }
  throw new Error('Unable to fit screenshot preview under 1 MB');
}

// ── Element / full-page screenshot ──
// 2.1.0: shared helpers for stitched captures
const CANVAS_MAX = 32000;   // Firefox canvas dimension limit is 32767
const zlSleep = (ms) => new Promise(r => setTimeout(r, ms));

// Capture helpers call named content.js operations; no code strings leave the package.
async function zlContent(tabId, action, params = {}) {
  const r = await forwardToContent(action, { tabId, ...params });
  return r && !r.error ? r : null;
}

async function zlScroll(tabId, scroll) {
  const r = await zlContent(tabId, 'captureScroll', scroll);
  return r ? r.sy : null;
}

// 2.1.1: scale (1-4) = output pixels per CSS pixel. captureTab renders the viewport at that scale, so
// scale 2 gives a genuinely sharper image (text in article screenshots), not an upscale. Default 1 = old size.
async function elementScreenshot(tabId, selector, scale, preview) {
  let hidFixed = false, id = null, captureAttribute = null;
  const want = Math.min(Math.max(Number(scale) || 1, 1), 4);
  try {
    id = tabId || (await getActiveTabId());
    if (!id || !selector) return { error: 'tabId and selector required' };
    if (/^(?:s\d+e\d+|r\d+)$/.test(selector)) {
      const target = await forwardToContent('markScreenshotTarget', { tabId: id, selector });
      if (target.error) return target;
      selector = target.selector;
      captureAttribute = target.attribute;
    }
    const tab = await browser.tabs.get(id);
    if (isRestrictedUrl(tab.url)) return { error: 'Cannot access browser internal page' };
    const measure = async () => {
      const r = await zlContent(id, 'captureMeasure', { selector });
      return r && !r.missing ? r : null;
    };
    let m = await measure();
    if (!m) return { error: 'No element matches: ' + selector };
    if (m.w < 1 || m.h < 1) return { error: 'Element has no size: ' + selector };
    const tall = m.h > m.vh;
    await zlContent(id, 'captureScrollIntoView', { selector, block: tall ? 'start' : 'center' });
    await zlSleep(150);
    m = await measure();
    if (!m) return { error: 'No element matches: ' + selector };
    const cssW = Math.min(m.w, CANVAS_MAX / want), cssH = Math.min(m.h, CANVAS_MAX / want);
    const W = Math.max(1, Math.round(cssW * want)), H = Math.max(1, Math.round(cssH * want));
    const canvas = new OffscreenCanvas(W, H);
    const ctx = canvas.getContext('2d');
    const shot = want > 1 ? { format: 'png', scale: want } : { format: 'png' };
    let covered = 0, segments = 0;
    // 2.1.0: elements taller than the viewport are scrolled and stitched (they used to come back blank below the fold)
    for (let guard = 0; guard < 60 && covered < cssH; guard++) {
      const img = await loadDataUrl(await browser.tabs.captureTab(id, shot));
      const d = img.width / m.vw;          // captured pixels per CSS pixel (dpr, or the requested scale)
      const o = W / cssW;                  // output pixels per CSS pixel
      const visTop = Math.max(0, m.y), visBot = Math.min(m.vh, m.y + m.h);
      const srcTop = Math.max(visTop, m.y + covered);
      const sx = Math.max(0, m.x), sw = Math.min(m.vw, m.x + m.w) - sx;
      if (visBot > srcTop && sw > 0) {
        ctx.drawImage(img, sx * d, srcTop * d, sw * d, (visBot - srcTop) * d,
                      (sx - m.x) * o, (srcTop - m.y) * o, sw * o, (visBot - srcTop) * o);
        covered = visBot - m.y;
        segments++;
      }
      if (covered >= cssH) break;
      if (!hidFixed) { await zlContent(id, 'hideFixed', { selector }); hidFixed = true; }
      const before = m.sy;
      await zlScroll(id, { by: Math.max(50, Math.floor(m.vh * 0.85)) });
      await zlSleep(160);
      m = await measure();
      if (!m) return { error: 'No element matches: ' + selector };
      if (m.sy === before) break;   // can't scroll any further
    }
    const blob = await canvas.convertToBlob({ type: 'image/png' });
    const cropped = arrayBufferToDataUrl(await blob.arrayBuffer(), 'image/png');
    return { ok: true, selector, bounds: { x: m.x, y: m.y, width: W, height: H }, scale: want, segments,
             complete: covered >= cssH - 1, dataUrl: preview ? await imagePreview(cropped) : cropped };
  } catch (e) { return { error: 'elementScreenshot failed: ' + e.message }; }
  finally {
    if (captureAttribute) {
      try { await forwardToContent('unmarkScreenshotTarget', { tabId: id, attribute: captureAttribute }); } catch (e) {}
    }
    if (hidFixed) { try { await zlContent(id, 'restoreFixed'); } catch (e) {} }
  }
}

async function fullPageScreenshot(tabId, preview) {
  let hidFixed = false, id = null;
  try {
    id = tabId || (await getActiveTabId());
    if (!id) return { error: 'No tab' };
    const tab = await browser.tabs.get(id);
    if (isRestrictedUrl(tab.url)) return { error: 'Cannot access browser internal page' };
    const m = await forwardToContent('fullPageMetrics', { tabId: id });
    if (m.error) return m;
    const vh = m.viewportHeight, vw = m.viewportWidth;
    const docH = Math.min(m.docHeight, CANVAS_MAX), docW = Math.min(Math.max(m.docWidth, 1), CANVAS_MAX);
    const segments = [];
    let y = 0;
    for (let i = 0; i < 40 && y < docH; i++) {
      await zlScroll(id, { to: y });
      await zlSleep(150);   // let lazy content settle
      const sy = Number(await zlScroll(id, {})) || 0;   // the page may clamp the last scroll
      segments.push({ y: sy, data: await browser.tabs.captureTab(id, { format: 'png' }) });
      if (!hidFixed) { await zlContent(id, 'hideFixed', { selector: null }); hidFixed = true; }
      if (sy + vh >= docH) break;
      y = sy + vh;
    }
    const canvas = new OffscreenCanvas(Math.round(Math.min(Math.max(docW, vw), CANVAS_MAX)), Math.round(docH));
    const ctx = canvas.getContext('2d');
    for (const seg of segments) {
      const img = await loadDataUrl(seg.data);
      const scale = img.width / vw;   // device pixels per CSS pixel (2.1.0: was ignored -> cropped on HiDPI)
      const drawH = Math.min(vh, docH - seg.y);
      if (drawH <= 0) continue;
      ctx.drawImage(img, 0, 0, img.width, drawH * scale, 0, seg.y, vw, drawH);
    }
    const blob = await canvas.convertToBlob({ type: 'image/png' });
    const dataUrl = arrayBufferToDataUrl(await blob.arrayBuffer(), 'image/png');
    return { ok: true, width: canvas.width, height: canvas.height, segments: segments.length,
             truncated: m.docHeight > CANVAS_MAX, dataUrl: preview ? await imagePreview(dataUrl) : dataUrl };
  } catch (e) { return { error: 'fullPageScreenshot failed: ' + e.message }; }
  finally {
    if (hidFixed) { try { await zlContent(id, 'restoreFixed'); } catch (e) {} }
  }
}

async function loadDataUrl(dataUrl) {
  const resp = await fetch(dataUrl);
  const blob = await resp.blob();
  return await createImageBitmap(blob);
}

function arrayBufferToDataUrl(buf, mime) {
  const bytes = new Uint8Array(buf);
  let bin = '';
  for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
  return 'data:' + mime + ';base64,' + btoa(bin);
}

// ── Cookies ──
async function cookiesOp(op, url, name, value, domain, path, secure) {
  try {
    if (op === 'get') {
      const all = await browser.cookies.getAll({ url, domain, name });
      return { ok: true, count: all.length, cookies: all };
    }
    if (op === 'set') {
      const c = await browser.cookies.set({ url, name, value: String(value), domain, path, secure: !!secure });
      return { ok: true, cookie: c };
    }
    if (op === 'remove') {
      const r = await browser.cookies.remove({ url, name });
      return { ok: true, removed: !!r };
    }
    if (op === 'clear') {
      const all = await browser.cookies.getAll({ url, domain });
      for (const c of all) {
        const u = (c.secure ? 'https://' : 'http://') + c.domain.replace(/^\./, '') + c.path;
        try { await browser.cookies.remove({ url: u, name: c.name }); } catch {}
      }
      return { ok: true, removed: all.length };
    }
    return { error: 'Unknown cookies op: ' + op };
  } catch (e) { return { error: e.message }; }
}

// ── Clipboard ──
async function clipboardOp(op, text) {
  try {
    if (op === 'read') {
      const t = await navigator.clipboard.readText();
      return { ok: true, text: t };
    }
    if (op === 'write') {
      await navigator.clipboard.writeText(String(text ?? ''));
      return { ok: true, length: String(text ?? '').length };
    }
    return { error: 'Unknown clipboard op: ' + op };
  } catch (e) { return { error: 'clipboard ' + op + ' failed: ' + e.message }; }
}

// ── Downloads ──
async function downloadsOp(op, url, filename, query) {
  try {
    if (op === 'download') {
      const id = await browser.downloads.download({ url, filename, conflictAction: 'uniquify' });
      return { ok: true, downloadId: id };
    }
    if (op === 'list') {
      const items = await browser.downloads.search(query || { limit: 20, orderBy: ['-startTime'] });
      return { ok: true, count: items.length, items };
    }
    if (op === 'cancel') {
      await browser.downloads.cancel(query?.id);
      return { ok: true };
    }
    return { error: 'Unknown downloads op: ' + op };
  } catch (e) { return { error: e.message }; }
}

// ── Browsing data ──
async function clearBrowsingData(types, since) {
  try {
    const opts = since ? { since } : {};
    const dataTypes = {};
    for (const t of (types || ['cache','cookies','history','localStorage','passwords','downloads'])) dataTypes[t] = true;
    await browser.browsingData.remove(opts, dataTypes);
    return { ok: true, cleared: Object.keys(dataTypes), since };
  } catch (e) { return { error: e.message }; }
}

// ── Request interception ──
const __intercept = { rules: [], listening: false, requestLog: [], maxLog: 500 };

function interceptListener(details) {
  __intercept.requestLog.push({
    time: Date.now(),
    url: details.url,
    method: details.method,
    tabId: details.tabId,
    type: details.type,
  });
  if (__intercept.requestLog.length > __intercept.maxLog) __intercept.requestLog.shift();
  for (const rule of __intercept.rules) {
    try {
      if (new RegExp(rule.pattern).test(details.url)) {
        if (rule.action === 'block') return { cancel: true };
        if (rule.action === 'redirect' && rule.target) return { redirectUrl: rule.target };
      }
    } catch {}
  }
  return {};
}

async function interceptOp(op, patterns, effect) {
  try {
    if (op === 'add') {
      for (const p of (patterns || [])) __intercept.rules.push({ pattern: p, action: effect || 'block' });
      ensureInterceptListener();
      return { ok: true, rules: __intercept.rules.length };
    }
    if (op === 'clear') {
      __intercept.rules.length = 0;
      removeInterceptListener();
      return { ok: true, rules: 0 };
    }
    if (op === 'list') {
      return { ok: true, rules: __intercept.rules.slice(), logged: __intercept.requestLog.length };
    }
    if (op === 'log') {
      return { ok: true, count: __intercept.requestLog.length, requests: __intercept.requestLog.slice(-200) };
    }
    if (op === 'clearLog') {
      __intercept.requestLog.length = 0;
      return { ok: true };
    }
    return { error: 'Unknown intercept op: ' + op };
  } catch (e) { return { error: e.message }; }
}

function ensureInterceptListener() {
  if (__intercept.listening) return;
  try {
    browser.webRequest.onBeforeRequest.addListener(interceptListener, { urls: ['<all_urls>'] }, ['blocking']);
    __intercept.listening = true;
  } catch (e) {
    console.error('[Kitsune] webRequest unavailable:', e);
  }
}

function removeInterceptListener() {
  if (!__intercept.listening) return;
  try {
    browser.webRequest.onBeforeRequest.removeListener(interceptListener);
    __intercept.listening = false;
  } catch {}
}

// ── Ask-before-act (bridge policy) ──
// Read-only: the tab a command would target and, with inspect, whether acting on its
// target element can submit a form. The bridge holds the command when it cannot tell.
async function describeTarget(params) {
  const tabId = params.tabId ?? await getActiveTabId();
  if (tabId == null) return noActiveTab('No active tab found');
  const state = await tabState(tabId);
  if (!state) return tabError(`Tab ${tabId} no longer exists`, 'tab_closed', { tabId });
  if (!params.inspect) return { tabId, url: state.url };
  const target = await forwardToContent('describeTarget', { ...params, tabId });
  return target.error ? target : { tabId, url: state.url, target };
}

// ── Click and wait for navigation ──
async function clickAndWaitNavigation(tabId, selector, timeout, first) {
  let cleanup;
  try {
    const id = tabId || (await getActiveTabId());
    if (!id || !selector) return { error: 'tabId and selector required' };
    const t0 = Date.now();
    const navPromise = new Promise((resolve) => {
      const max = timeout || 15000;
      const to = setTimeout(() => { cleanup(); resolve({ ok: false, timedOut: true }); }, max);
      function listener(updatedTabId, changeInfo, tab) {
        if (updatedTabId === id && changeInfo.status === 'complete') {
          cleanup();
          resolve({ ok: true, url: tab.url, title: tab.title, elapsed: Date.now() - t0 });
        }
      }
      cleanup = () => {
        clearTimeout(to);
        browser.tabs.onUpdated.removeListener(listener);
      };
      browser.tabs.onUpdated.addListener(listener);
    });
    const click = await forwardToContent('click', { tabId: id, selector, first });
    if (click.error) return { ...click, clicked: false };
    const nav = await navPromise;
    return { ok: true, clicked: true, navigation: nav };
  } catch (e) { return { error: e.message }; }
  finally { cleanup?.(); }
}

// == Init ==
connect();
browser.runtime.onStartup.addListener(connect);
browser.runtime.onInstalled.addListener(() => {
  console.log('[Kitsune] Extension installed');
  connect();
});
