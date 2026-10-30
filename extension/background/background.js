// Kitsune - Background Script
// Connects to local bridge server via WebSocket, relays commands to content scripts

'use strict';

const EXPECTED_CONTENT_VERSION = 47;
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
// Requests that keep an action's delta waiting (settleAfterAction, #348).
const SETTLE_REQUEST_FILTER = { urls: ['<all_urls>'], types: ['xmlhttprequest', 'script', 'sub_frame'] };
browser.webRequest?.onBeforeRequest.addListener(({ tabId, requestId }) => {
  settleRequests.get(tabId)?.add(requestId);
}, SETTLE_REQUEST_FILTER);
for (const event of ['onCompleted', 'onErrorOccurred']) {
  browser.webRequest?.[event]?.addListener(({ tabId, requestId }) => {
    settleRequests.get(tabId)?.delete(requestId);
  }, SETTLE_REQUEST_FILTER);
}
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
  trustedShownTabs.delete(tabId);
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
  const page = await browser.tabs.sendMessage(tabId, { action: 'userActivity' }, TOP_FRAME).catch(() => null);
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

// Viewing counts as using (#326, docs/api-reference.md#viewed-tab): a guarded write or navigation
// on the tab shown in the browser window the user last focused returns user_viewing even without
// recent input. That window keeps the hold while another app (the agent's terminal) has the
// focus. Closing a tab, view switches and screenshots are not held, nor are tabs the agent opened.
// Only the bridge sets viewedTabApproved (a confirmed direct call; the userTabApproved of a
// protected navigation counts too) and holdViewedTab: false (the client's KITSUNE_HOLD_VIEWED_TAB=0).
// Showing another tab (switchTab, focusWindow) moves the hold with the view. A tab a trusted
// command showed for its OS input (trustedShownTabs) is the agent's view, not the user's, until
// the user's tab is shown again: a trusted drag measures its destination there after its source.
const VIEWED_TAB_EXEMPT_ACTIONS = new Set(['closeTab', 'switchTab', 'focusWindow', 'elementScreenshot', 'fullPageScreenshot']);
async function viewedTabError(params) {
  const tabId = params.tabId;
  // The bridge strips a caller's userTabApproved only on the routes that approve it.
  const navigationApproved = params.userTabApproved && PROTECTED_NAVIGATIONS.has(params.action);
  if (params.holdViewedTab === false || params.viewedTabApproved || navigationApproved ||
      VIEWED_TAB_EXEMPT_ACTIONS.has(params.action) || tabId == null || agentTabs.has(tabId) ||
      trustedShownTabs.get(tabId)?.until > Date.now()) return null;
  const tab = await browser.tabs.get(tabId).catch(() => null);
  if (!tab?.active) return null;
  const focused = await browser.windows.getLastFocused().catch(() => null);
  if (focused?.id !== tab.windowId) return null;
  return tabError(`The user is viewing tab ${tabId}; nothing was done`, 'user_viewing',
    { tabId, url: tab.url, title: tab.title, windowId: tab.windowId, reason: 'viewing',
      browserFocused: !!focused.focused }, false);
}

// The longest pause a held click or a sustained hover asks for (content.js POINTER_MAX_MS).
const POINTER_PAUSE_MAX_MS = 10000;

browser.runtime.onMessage.addListener((msg, sender) => {
  if (msg.action === 'getDialogPolicy') {
    return Promise.resolve(dialogPolicies.get(sender.tab?.id) || null);
  }
  // A hidden tab's timers are throttled to 1 s, the background's are not (#351).
  if (msg.action === 'pointerPause') {
    const ms = Math.min(Math.max(Number(msg.ms) || 0, 0), POINTER_PAUSE_MAX_MS);
    return new Promise(resolve => setTimeout(resolve, ms, { ok: true }));
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
      // A replaced socket closes after connect() opened the next one; cleaning up then would
      // close its replacement and drop the bridge connection a second later (#299).
      if (ws !== socket) return;
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
  ['selectOption', 'browser_select_option'], ['checkBox', 'browser_check'], ['keypress', 'browser_press_key'],
  ['submitForm', 'browser_submit_form'], ['navigate', 'browser_navigate'], ['hover', 'browser_hover'],
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
  // browser_wait_for(time)'s delta (#350) is a post-action snapshot: KITSUNE_POST_ACTION_SNAPSHOT=0 turns it off.
  if (params.action === 'getSnapshot' && params.sinceObservation && params.includeSnapshot === false) return { ok: true };
  // Descriptions are audit metadata only; never use them to locate elements.
  if (params.ref) params.selector = params.ref;
  if (params.action === 'drag') {
    if (params.ref) params.from = params.ref;
    if (params.toRef) params.to = params.toRef;
  }
  const withSnapshot = POST_SNAPSHOT_ACTIONS.has(params.action) && params.includeSnapshot !== false;
  const deltaSnapshot = withSnapshot && params.snapshot !== 'full' && !FULL_SNAPSHOT_ACTIONS.has(params.action);
  // The content script records the delta's baseline right before it acts. keepBaseline: diff
  // against the baseline an earlier action recorded (deltaBaseline without a snapshot), as
  // browser_fill_form does for its fields (#348); this action only starts the settle watch.
  if (deltaSnapshot) {
    if (params.keepBaseline) params.settleWatch = true;
    else params.deltaBaseline = true;
  }
  // Text snapshots report which tab they read.
  const textSnapshot = params.action === 'getSnapshot' && !!params.text;
  const tracksOpenedTabs = OPENER_ACTIONS.has(params.action);
  // A trusted click's screenPoint (see screenPointForClick) acts on the page like a click.
  const guarded = USER_GUARDED_ACTIONS.has(params.action) || (params.action === 'screenPoint' && !!params.click);
  const touches = guarded && params.action !== 'focusWindow';
  if ((withSnapshot || textSnapshot || touches) && params.tabId == null) params.tabId = await getActiveTabId();
  const unprotected = await protectedTabError(params);
  if (unprotected) return unprotected;
  if (guarded) {
    const busy = await userActiveError(await guardedTabId(params.action, params)) || await viewedTabError(params);
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

// Under a session's protectUserTabs (#199), only the bridge's userTabApproved navigates a tab the
// agent did not open. Clicks, forms, reloads and page-initiated loads are not covered.
const PROTECTED_NAVIGATIONS = new Set(['navigate', 'goBack', 'goForward']);
async function protectedTabError(params) {
  const tabId = params.tabId;
  if (!params.protectUserTabs || params.userTabApproved || !PROTECTED_NAVIGATIONS.has(params.action) ||
      tabId == null || agentTabs.has(tabId)) return null;
  const tab = await browser.tabs.get(tabId).catch(() => null);
  if (!tab) return null;
  return { ...tabError(`Tab ${tabId} was not opened by the agent and this session protects user tabs, so it was ` +
    'not navigated', 'user_tab', { tabId, url: tab.url, title: tab.title }, false),
    hint: 'Nothing navigated. Open extra material with browser_tabs action=new and pass its tabId as tab_id, ' +
      'or ask the user and repeat a direct call with tab_id and the confirm_token it returns.' };
}

// ── Settling after an action (#348) ──
// Before its delta snapshot, an action waits for the page's reaction:
// - at least SETTLE_MIN_MS after it started;
// - until the page has made no change for SETTLE_QUIET_MS (see settleState in the content script);
// - until the XHR/fetch, script and frame requests the tab started since then are done.
// It waits at most SETTLE_CAP_MS, or SETTLE_REQUEST_CAP_MS while such requests are pending.
// For comparison, Playwright MCP waits a fixed 500 ms, then for the requests the action started.
// The minimum also covers debounced reactions, which change nothing until they fire (jQuery UI
// autocomplete waits 300 ms). In a hidden tab Firefox delays page timers to at least 1 s
// (dom.min_background_timeout_value), so a 250 ms timer fires after about 1000 ms there; the
// minimum is then SETTLE_HIDDEN_MIN_MS.
const SETTLE_MIN_MS = 400;
const SETTLE_HIDDEN_MIN_MS = 1100;
const SETTLE_QUIET_MS = 150;
const SETTLE_CAP_MS = 1500;
const SETTLE_REQUEST_CAP_MS = 4000;
const SETTLE_POLL_MS = 40;
// tabId -> ids of the requests the tab started while an action on it settles (filled by the
// webRequest listeners at the top of this file).
const settleRequests = new Map();

// Never fails: when the page cannot say, the snapshot is taken right away.
// The document an action ran in: a cross-origin frame's own when its ref is there (#343). A
// message without a frameId would reach every frame.
function actionFrame(tabId, params) {
  const expanded = expandShortRefs(params, tabId);
  return { frameId: expanded.params ? targetFrame(tabId, expanded.params) : 0 };
}

async function settleAfterAction(tabId, pending, params) {
  const start = Date.now();
  try {
    const frame = actionFrame(tabId, params);
    while (true) {
      let timer;
      const state = await Promise.race([
        browser.tabs.sendMessage(tabId, { action: 'settleState' }, frame).catch(() => null),
        new Promise(resolve => { timer = setTimeout(() => resolve(null), SETTLE_CAP_MS); }),
      ]);
      clearTimeout(timer);
      // A page that navigated away or does not answer has nothing to wait for here.
      if (!state?.watching) return;
      const busy = pending.size > 0;
      const min = state.hidden ? SETTLE_HIDDEN_MIN_MS : SETTLE_MIN_MS;
      if (!busy && state.elapsedMs >= min && state.quietMs >= SETTLE_QUIET_MS) return;
      if (Date.now() - start >= (busy ? SETTLE_REQUEST_CAP_MS : SETTLE_CAP_MS)) return;
      await new Promise(resolve => setTimeout(resolve, SETTLE_POLL_MS));
    }
  } catch {}
}

// ── Actions that navigate (#349) ──
// An action with a delta snapshot that started loading another document in its tab (a link, a
// form, a script setting location) waits for that load like browser_navigate, at most this long,
// and returns navigated {url, title} with a full snapshot of the new page instead of a delta of
// the old one; navigation_pending when it has not finished loading by then.
const ACTION_NAVIGATION_WAIT_MS = 10000;
// A main-frame request that loads nothing (a download, a 204) is given up after this long.
const ACTION_NAVIGATION_START_MS = 1000;

// Whether the tab started another load or main-frame request since the action began.
function navigationStarted(tabId, watch) {
  const request = mainFrameStatus.get(tabId);
  return tabLoads.get(tabId) !== watch.loadBefore ||
    (request != null && request.requestId !== watch.requestBefore && request.at >= watch.start);
}

// Resolves to 'loaded', 'pending' (still loading at the deadline) or null (nothing loaded). A
// load that kept the document (a 204, a download) is not a navigation: the document that
// acted still answers with its settle watch.
async function waitForActionNavigation(tabId, watch, params) {
  const start = Date.now();
  try {
    while (true) {
      const load = tabLoads.get(tabId);
      const tab = await browser.tabs.get(tabId);
      if (load !== watch.loadBefore && load?.complete && tab.status === 'complete') {
        const kept = await browser.tabs.sendMessage(tabId, { action: 'settleState' }, actionFrame(tabId, params))
          .catch(() => null);
        return kept?.watching ? null : 'loaded';
      }
      if (load === watch.loadBefore && Date.now() - start >= ACTION_NAVIGATION_START_MS) return null;
      if (Date.now() - start >= ACTION_NAVIGATION_WAIT_MS) return load !== watch.loadBefore ? 'pending' : null;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
  } catch {
    return null;
  }
}

async function runCommand(params, options) {
  const settles = options.deltaSnapshot && params.tabId != null && params.settle !== false;
  if (!settles) return await runCommandSettled(params, options, null);
  const watch = { pending: new Set(), start: Date.now(), loadBefore: tabLoads.get(params.tabId),
    requestBefore: mainFrameStatus.get(params.tabId)?.requestId };
  settleRequests.set(params.tabId, watch.pending);
  try {
    return await runCommandSettled(params, options, watch);
  } finally {
    if (settleRequests.get(params.tabId) === watch.pending) settleRequests.delete(params.tabId);
  }
}

async function runCommandSettled(params, { withSnapshot, deltaSnapshot, textSnapshot, tracksOpenedTabs }, watch) {
  const result = await dispatchCommand(params);
  let navigation = null;
  if (watch && result && !result.error && result.ok !== false) {
    await settleAfterAction(params.tabId, watch.pending, params);
    if (navigationStarted(params.tabId, watch)) {
      navigation = await waitForActionNavigation(params.tabId, watch, params);
      if (navigation) {
        const tab = await browser.tabs.get(params.tabId).catch(() => null);
        result.navigated = { url: tab?.url, title: tab?.title };
        if (navigation === 'pending') result.navigation_pending = true;
      }
    }
  }
  const blankLink = result?.targetBlankLink;
  if (blankLink) delete result.targetBlankLink;
  const newTabLink = result?.newTabLink;
  if (newTabLink) delete result.newTabLink;
  // A click or hover the content script watched for a popup it opens (#329).
  const popupWatch = result?.popupWatch;
  if (popupWatch) delete result.popupWatch;
  // A click during which a page script's window.open was refused (#304).
  const scriptPopup = result?.scriptPopup;
  if (scriptPopup) delete result.scriptPopup;
  if (textSnapshot && result && !result.error) result.tabId = params.tabId;
  // Taken before the snapshot, which then lists the popup even when it is not rendered.
  if (popupWatch && params.tabId != null) {
    const popup = await takeOpenedPopup(params.tabId);
    if (popup) result.opened_popup = popup;
  }
  if (withSnapshot && result && !result.error && result.ok !== false) {
    try {
      const snapshot = await forwardToContent('getSnapshot', {
        tabId: params.tabId, maxChars: 4000, focusLastAction: params.action !== 'navigate',
        observation: POST_SNAPSHOT_ACTIONS.get(params.action),
        ...(params.snapshotText ? { text: true } : {}),
        // The document the action ran in, whose delta has its baseline (#354).
        ...(deltaSnapshot && !navigation ? { delta: true, actionFrame: actionFrame(params.tabId, params).frameId } : {}),
      });
      if (snapshot.error) result.snapshot_error = snapshot.error;
      else {
        result.snapshot = snapshot.snapshot;
        if (snapshot.delta) {
          result.snapshot_delta = true;
          if (snapshot.unchanged) result.no_visible_change = true;
          // A same-document url or title change (history.pushState) is part of the delta.
          if (snapshot.url !== undefined) result.url = snapshot.url;
          if (snapshot.title !== undefined) result.title = snapshot.title;
        }
        if (snapshot.truncated) result.snapshot_truncated = true;
        if (snapshot.hint) result.snapshot_hint = snapshot.hint;
        // The field describes the snapshot it comes with; a navigation's own result does not come from
        // the page, and an action's may predate the user showing the tab (#328).
        if (snapshot.visibilityState) result.visibilityState = snapshot.visibilityState;
        else delete result.visibilityState;
        if (snapshot.target_ref) result.target_ref = snapshot.target_ref;
      }
    } catch (e) { result.snapshot_error = e.message; }
  }
  if (blankLink && params.tabId != null) await openBlockedLink(params.tabId, blankLink, result);
  // A middle or Ctrl/Cmd click's link opens like a popup-blocked one; a cancelled click is the page's.
  if (newTabLink && !newTabLink.prevented && params.tabId != null) {
    await openBlockedLink(params.tabId, { url: newTabLink.url, opener: false }, result);
  }
  // Checked after the snapshot, which gives a link's new tab time to be created.
  if (tracksOpenedTabs && params.tabId != null && result && typeof result === 'object') {
    const opened = await getOpenedTabs(params.tabId, OPENED_TAB_URL_WAIT_MS);
    if (opened.opened_tabs.length) result.opened_tabs = opened.opened_tabs;
    else {
      if (newTabLink) noNewTab(result, newTabLink);
      if (scriptPopup) scriptPopupBlocked(result, scriptPopup);
    }
  }
  return result;
}

// The popup a click or hover opened, with public refs (#329). A page that navigated away has
// none, so this skips ensureContentScript and any wait for a new document.
async function takeOpenedPopup(tabId) {
  try {
    const response = await browser.tabs.sendMessage(tabId, { action: 'openedPopup' }, TOP_FRAME);
    if (!response?.opened_popup) return null;
    return (await compactContentRefs('openedPopup', response, tabId, {})).opened_popup;
  } catch {
    return null;
  }
}

// A click during which a page script's window.open returned null (#304): the popup blocker
// refused the popup, because a synthetic click has no user activation. Kitsune neither retries
// it nor clicks again; an earlier warning stays. The URL comes from a page script, so it is
// redacted like snapshot URLs.
function scriptPopupBlocked(result, popup) {
  const url = typeof popup.url === 'string' && popup.url ? redactHistoryUrl(popup.url) : null;
  const urls = (Array.isArray(popup.urls) ? popup.urls : (popup.url ? [popup.url] : []))
    .filter(value => typeof value === 'string' && value).slice(0, 20).map(redactHistoryUrl);
  const state = { ...(url ? { url } : {}), urls, reason: 'script_popup' };
  result.warning ??= { code: 'popup_blocked', state,
    hint: 'A page script tried to open a popup (window.open), which Firefox blocks without user activation, ' +
      'so none opened. Check state.urls for all refused URLs. If this click should open that popup ' +
      '(a sign-in at state.url, for example), ask the ' +
      'user to click it, then continue in the new tab by tab_id (find it with browser_tabs), or use ' +
      'browser_trusted_click; if not (an ad, for example), ignore this warning.' };
}

// A click meant to open its link in a new tab says so when none opened (#223).
function noNewTab(result, link) {
  result.opened_tabs = [];
  const state = { url: link.url, ...(link.prevented ? { reason: 'page_cancelled' } : {}) };
  result.warning ??= { code: 'no_tab_opened', state,
    hint: link.prevented
      ? 'The page cancelled this click, so its link did not open in a new tab, and the page opened none ' +
        '(the popup blocker drops tabs that synthetic clicks open). It may have acted in this tab instead; ' +
        'check the snapshot, or open state.url with browser_tabs action=new.'
      : 'No tab opened for this link; open state.url with browser_tabs action=new.' };
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
  result.warning ??= { code: 'popup_blocked', state: { url: redactHistoryUrl(link.url) },
    hint: 'The link opens in a new tab, but no tab opened; the popup blocker likely dropped this ' +
      'synthetic click. Use browser_trusted_click on the original page control, or ask the user to click it; ' +
      'state.url may contain redacted values and must not be used as a navigation URL.' };
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
    case 'screenPoint':  return params.click ? await screenPointForClick(params) : await zoomedScreenPoint(params);
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
    case 'resizeWindow':     return await resizeWindow(params.tabId, params.width, params.height);
    case 'moveTab':          return await moveTabAction(params.tabId, params.windowId, params.index);
    case 'detachTab':        return await detachTab(params.tabId);
    case 'elementScreenshot':return await elementScreenshot(params.tabId, params.selector, params.scale, params.imagePreview, params.format);
    case 'fullPageScreenshot': return await fullPageScreenshot(params.tabId, params.imagePreview, params.format);
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
    case 'focusState':
    case 'agentInput':
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

function screenshotFormat(format = 'png') {
  if (format !== 'png' && format !== 'jpeg') throw new Error('Screenshot format must be png or jpeg');
  return format;
}

function screenshotFailure(error, stage, tabId) {
  const underlying = error?.screenshotResult || {};
  const cause = error?.message || String(error);
  return {
    ...underlying,
    error: `Screenshot failed during ${stage}: ${cause}`,
    code: underlying.code || 'screenshot_failed',
    retryable: underlying.retryable ?? false,
    state: { ...underlying.state, tabId, stage, cause, name: error?.name || 'Error' },
    hint: underlying.hint || 'If the tab is loading or was just resized, wait for it to settle before retrying. ' +
      'Internal/protected pages cannot be captured. Otherwise report this cause and stage; do not retry blindly.'
  };
}

// tabs.captureTab renders a tab whether or not it is shown, so no capture activates a tab.
async function captureScreenshot(params = {}) {
  let stage = 'resolve tab', tabId = params.tabId;
  try {
    const format = screenshotFormat(params.format);
    const tab = params.tabId != null ? await browser.tabs.get(params.tabId)
      : (await browser.tabs.query({ active: true, currentWindow: true }))[0];
    if (!tab) return noActiveTab('No active tab found');
    tabId = tab.id;
    stage = 'tabs.captureTab';
    const dataUrl = await browser.tabs.captureTab(tab.id, { format, quality: 90 });
    stage = 'image preview';
    return {
      dataUrl: params.imagePreview ? await imagePreview(dataUrl) : dataUrl,
      url: tab.url || '',
      title: tab.title || '',
      width: tab.width || 0,
      height: tab.height || 0
    };
  } catch (e) {
    return screenshotFailure(e, stage, tabId);
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

// The URL of the tab's top document once it has loaded, from its content script; null while it
// loads, for an about: page, or when nothing answers within PAGE_URL_TIMEOUT_MS. While Zen's
// first-run welcome screen hides the tab area, Gecko keeps a loaded tab's url at about:blank and
// executeScript never returns there (#360), but the content script answers.
const PAGE_URL_TIMEOUT_MS = 500;
async function loadedPageUrl(tabId) {
  let timer;
  const info = await Promise.race([
    browser.tabs.sendMessage(tabId, { action: 'getPageInfo' }, TOP_FRAME).catch(() => null),
    new Promise(resolve => { timer = setTimeout(() => resolve(null), PAGE_URL_TIMEOUT_MS); }),
  ]);
  clearTimeout(timer);
  const href = typeof info?.url === 'string' ? info.url : '';
  return info?.readyState === 'complete' && href && !href.startsWith('about:') ? href : null;
}

// Runs startNavigation and resolves true once the tab finishes loading, false on timeout.
// Subscribes first so a fast 'complete' is not missed. Gecko may never emit a fresh
// 'complete' (e.g. reloading a page with iframes in headed Zen), so tab.status is also
// polled: a reload accepts any 'complete', other loads only one seen after 'loading'. A
// complete tab still at about: counts when its page reports a loaded web URL (loadedPageUrl).
function waitForTabLoad(tabId, startNavigation, { reload = false, timeout = 15000 } = {}) {
  return new Promise((resolve, reject) => {
    let settled = false;
    let sawLoading = false;
    let poll = null;
    // A check that asks the page (loadedPageUrl) can outlast the poll interval; one at a time.
    let checking = false;
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
      if (checking) return;
      checking = true;
      try {
        const tab = await browser.tabs.get(tabId);
        if (settled) return;
        if (tab.status === 'loading') sawLoading = true;
        else if ((sawLoading || reload) && isLoaded(tab)) finish(true);
        else if ((sawLoading || reload) && tab.status === 'complete' && tab.url?.startsWith('about:')
          && await loadedPageUrl(tabId)) finish(true);
      } catch (e) {
        finish(false, e);
      } finally {
        checking = false;
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
    const shownUrl = current.url.startsWith('about:') && !target.startsWith('about:')
      && await loadedPageUrl(tab.id) || current.url;
    return {
      ok: loaded, ...(loaded ? {} : { timedOut: true }),
      tabId: tab.id, url: shownUrl, status: current.status,
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
// The tab it hides for that moment is checked for the user's input, not held for being viewed
// (viewedTabError holds only the click's own tab, before it is shown).
const TRUSTED_CLICK_PAINT_MS = 150;
// tabId -> { windowId, until }: tabs screenPointForClick showed, until restoreActiveTab, another
// tab of their window is shown, or TRUSTED_SHOWN_MAX_MS (a bridge that never restored them).
const trustedShownTabs = new Map();
const TRUSTED_SHOWN_MAX_MS = 60000;
browser.tabs.onActivated?.addListener(({ tabId, windowId }) => {
  for (const [shown, entry] of trustedShownTabs) {
    if (entry.windowId === windowId && shown !== tabId) trustedShownTabs.delete(shown);
  }
});
// How long a shown tab may take to get a size on screen, polling this often.
const TRUSTED_CLICK_LAYOUT_WAIT_MS = 1000;
const TRUSTED_CLICK_LAYOUT_POLL_MS = 50;
async function screenPointForClick(params) {
  let previousTabId = null;
  let tab;
  try {
    tab = await browser.tabs.get(params.tabId);
    if (!tab.active) {
      const [shown] = await browser.tabs.query({ windowId: tab.windowId, active: true });
      const busy = shown && await userActiveError(shown.id);
      if (busy) return busy;
      previousTabId = shown?.id ?? null;
      await browser.tabs.update(tab.id, { active: true });
      trustedShownTabs.set(tab.id, { windowId: tab.windowId, until: Date.now() + TRUSTED_SHOWN_MAX_MS });
      await new Promise(resolve => setTimeout(resolve, TRUSTED_CLICK_PAINT_MS));
    }
  } catch (e) {
    return await tabApiError(params.tabId, e);
  }
  let point;
  try {
    point = await tabNotShownError(tab.id) || await zoomedScreenPoint(params);
    if (point.error) return point;
    // The bridge checks that this window is the one at the click point (#336).
    const win = await browser.windows.get(tab.windowId).catch(() => null);
    if (win) point = { ...point, window: { left: win.left, top: win.top, width: win.width, height: win.height } };
    if (previousTabId == null) return point;
    return { ...point, tabId: params.tabId, previousTabId };
  } finally {
    if (previousTabId != null && (!point || point.error)) {
      await restoreActiveTab(previousTabId, params.tabId);
    }
  }
}

// tab_not_shown while the tab reports no size on screen: its window shows no page, as during
// Zen's first-run welcome screen (#360), so the page has no box to click. null once it has one.
async function tabNotShownError(tabId) {
  const deadline = Date.now() + TRUSTED_CLICK_LAYOUT_WAIT_MS;
  try {
    while (true) {
      const { width, height } = await browser.tabs.get(tabId);
      if (width !== 0 && height !== 0) return null;
      if (Date.now() >= deadline) {
        return tabError(`Tab ${tabId} has no size on screen; no OS input was sent`, 'tab_not_shown',
          { ...await tabState(tabId), width, height });
      }
      await new Promise(resolve => setTimeout(resolve, TRUSTED_CLICK_LAYOUT_POLL_MS));
    }
  } catch (e) {
    return await tabApiError(tabId, e);
  }
}

// Content measures in the page's zoomed CSS pixels; screenPoint converts with the tab's zoom (#307).
async function zoomedScreenPoint(params) {
  const tabId = params.tabId || await getActiveTabId();
  if (!tabId) return noActiveTab('No active tab found');
  let zoom;
  try {
    zoom = await browser.tabs.getZoom(tabId);
  } catch (e) {
    return await tabApiError(tabId, e);
  }
  return await forwardToContent('screenPoint', { ...params, tabId, zoom });
}

// Shows the user's tab again after a trusted click, unless the user already moved elsewhere.
async function restoreActiveTab(tabId, from) {
  trustedShownTabs.delete(from);
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
    // The last completed check; one the deadline cut short observed nothing and keeps it.
    let page = {};
    while (Date.now() < deadline) {
      page = await wokenPageState(tabId, since, deadline) ?? page;
      if (page.ready) return { ok: true, woken: true, ready: true };
      if (page.unscriptable) return { ok: true, woken: true, ready: false, unscriptable: true, state: page.state };
      const remaining = deadline - Date.now();
      if (remaining > 0) await new Promise(r => setTimeout(r, Math.min(WAKE_TAB_POLL_MS, remaining)));
    }
    const state = await tabState(tabId);
    // The last check found the woken page loaded, but nothing answered after injection.
    return { ok: true, woken: true, ready: false, timedOut: true,
      state: page.noReceiver && state ? { ...state, reason: 'no_receiver' } : state };
  } catch (e) {
    return { error: e.message };
  }
}

// Gecko first restores a woken tab's discarded document as a placeholder, reporting it complete
// at about:blank or with the tab's URL and title before the reload replaces it; a command that
// reached it would be lost with it. So the woken page counts only once the tab reported loading
// after the wake (`since`), then complete. It is ready when it answers the content script and
// is still that complete load afterwards; it is final (unscriptable) when it refuses injection.
// A page that accepts injection but does not answer is not ready (noReceiver); the next check
// tries again, until the wake's deadline. null: the deadline left no time for a confirming ping.
async function wokenPageState(tabId, since, deadline) {
  const tab = await browser.tabs.get(tabId);
  const load = tabLoads.get(tabId);
  if (tab.discarded || tab.status !== 'complete' || !(load?.at >= since && load.complete)) return {};
  try {
    await ensureContentScript(tabId, deadline);
  } catch (e) {
    if (e instanceof NoContentReceiver) return e.pings ? { noReceiver: true } : null;
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
  tab_not_shown: 'The browser shows no page in this tab\'s window. Ask the user to finish or close whatever ' +
    'hides the page (for example, Zen\'s first-run welcome screen), then retry.',
  user_viewing: 'The user is viewing this tab (shown in the browser window they last focused); nothing was done. ' +
    'Work in a tab you opened (browser_tabs action=new), or ask the user and repeat a direct call with tab_id and ' +
    'the confirm_token that call returns.',
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

// A successful injection does not prove a receiver (#291). For example, Gecko refuses every message
// to a tab it marks pending while injection still succeeds, finding the content script initialized.
// pings: how many confirming pings went unanswered (none when the deadline left no time).
class NoContentReceiver extends Error {
  constructor(message, pings) {
    super(message);
    this.pings = pings;
  }
}

// With a deadline (wakeTab), how long one ping may wait for its answer.
const CONTENT_PING_TIMEOUT_MS = 1000;
// After an injection, how long the content script has to answer a ping, polling this often.
const CONTENT_CONFIRM_MS = 500;
const CONTENT_CONFIRM_POLL_MS = 50;

// Whether the tab's current document answers a ping from the current content script, within
// budgetMs (Infinity waits for the answer). Inactive documents never answer.
async function pingContent(tabId, budgetMs, frameId = 0) {
  if (!(budgetMs > 0)) return false;
  const ping = browser.tabs.sendMessage(tabId, { action: 'ping' }, { frameId }).catch(() => null);
  let timer;
  const reply = budgetMs === Infinity ? await ping
    : await Promise.race([ping, new Promise(resolve => { timer = setTimeout(() => resolve(null), budgetMs); })]);
  clearTimeout(timer);
  return reply?.ok === true && reply.version >= EXPECTED_CONTENT_VERSION;
}

// Injects the content script unless the page already answers, then confirms that it answers.
// Throws the injection's error if the page refuses it, or NoContentReceiver if nothing answers.
// With a deadline, every ping ends by it; the injection is one browser call it cannot cut short.
// Without one, the first ping waits for its answer, as a busy page answers late.
async function ensureContentScript(tabId, deadline = Infinity, frameId = 0) {
  const firstBudget = deadline === Infinity ? Infinity : Math.min(CONTENT_PING_TIMEOUT_MS, deadline - Date.now());
  if (await pingContent(tabId, firstBudget, frameId)) return;
  await browser.tabs.executeScript(tabId, { file: '/content/content.js', frameId });
  const confirmBy = Math.min(Date.now() + CONTENT_CONFIRM_MS, deadline);
  let pings = 0;
  while (Date.now() < confirmBy) {
    pings++;
    if (await pingContent(tabId, confirmBy - Date.now(), frameId)) return;
    if (confirmBy - Date.now() <= CONTENT_CONFIRM_POLL_MS) break;
    await new Promise(r => setTimeout(r, CONTENT_CONFIRM_POLL_MS));
  }
  throw new NoContentReceiver('The content script was injected but the page did not answer', pings);
}

const MISSING_CONTENT_RECEIVER = 'Could not establish connection. Receiving end does not exist.';

// With the content script in every frame (all_frames), a message without a frameId reaches
// all of them and the first answer wins; page-level messages go to the top frame.
const TOP_FRAME = { frameId: 0 };

// ── Cross-origin frames (#343) ──
// A cross-origin frame's content is read by the content script running in that frame. Every
// document seeds its own ref generation (s<generation>e<n>), so the frame a ref belongs to is
// known from its generation: tabId -> Map(generation -> frameId). Unknown ones are top-frame.
const frameGenerations = new Map();
// tabId -> Map(frameId -> { parent: frameId, ref }): the frame showing a cross-origin frame
// and the ref of its frame element there, for screen points (screenPoint).
const frameParents = new Map();
// Frames read inside frames, at most this many cross-origin frames deep.
const CROSS_ORIGIN_FRAME_DEPTH = 3;
// How long a snapshot waits for the frames it reads, together; a frame that has not answered
// by then stays opaque.
const FRAME_READ_TIMEOUT_MS = 1500;
// Cross-origin frames one snapshot reads, in document order (ads and widgets can be many).
const CROSS_ORIGIN_FRAME_READS = 6;

function noteFrameRefs(tabId, snapshot, frameId) {
  const generations = new Set([...snapshot.matchAll(/\bs(\d+)e\d+\b/g)].map(m => m[1]));
  if (!generations.size) return;
  if (!frameGenerations.has(tabId)) frameGenerations.set(tabId, new Map());
  for (const generation of generations) frameGenerations.get(tabId).set(generation, frameId);
}

// The frame the (expanded) refs of params point into; 0 (top) without one.
function targetFrame(tabId, params) {
  for (const key of REF_PARAMS) {
    const generation = typeof params[key] === 'string' && /^\s*s(\d+)e\d+/.exec(params[key])?.[1];
    const frameId = generation && frameGenerations.get(tabId)?.get(generation);
    if (frameId) return frameId;
  }
  return 0;
}

// A screen point in a cross-origin frame: its point in the frame's viewport, converted by the
// frame showing it, up to the top frame. Errors (a covered frame) come from that frame.
async function frameScreenPoint(tabId, frameId, point, params) {
  for (let depth = 0; frameId && depth < CROSS_ORIGIN_FRAME_DEPTH; depth++) {
    const link = frameParents.get(tabId)?.get(frameId);
    if (!link) {
      return { error: 'The frame holding the target is no longer known; take a new browser_snapshot',
        code: 'stale_ref', retryable: true, hint: 'Take a new browser_snapshot and use its refs.' };
    }
    point = await browser.tabs.sendMessage(tabId, { action: 'screenPoint', frame: link.ref,
      x: point.viewportX, y: point.viewportY, click: params.click, zoom: params.zoom,
      modifiers: params.modifiers }, { frameId: link.parent });
    if (!point || point.error) return point;
    frameId = link.parent;
  }
  return point;
}

// A read's budget for the cross-origin frames in it, shared with the frames inside them.
// answered: the frames that answered.
function frameReadBudget() {
  return { reads: CROSS_ORIGIN_FRAME_READS, deadline: Date.now() + FRAME_READ_TIMEOUT_MS, answered: new Set() };
}

// Where a read is: the frame of the document it reads, how many cross-origin frames deep that
// document is, and the read's budget. innerScope is the scope of a frame listed in it.
const innerScope = (scope, frame) => ({ ...scope, parent: frame.frameId, depth: scope.depth + 1 });

// readFrame's answer for a frame the browser refused to script or message: one removed from
// the page, for example, unlike a frame that is still loading or busy.
const FRAME_GONE = Symbol('frame gone');

// One frame's answer to message, injecting the content script into it first: frames get it
// only when a read reaches them. null when it does not answer by deadline, FRAME_GONE above.
async function readFrame(tabId, frameId, message, deadline) {
  let timer;
  const read = (async () => {
    await ensureContentScript(tabId, deadline, frameId);
    return await browser.tabs.sendMessage(tabId, message, { frameId });
  })().catch(e => e instanceof NoContentReceiver ? null : FRAME_GONE);
  const child = await Promise.race([read, new Promise(resolve => {
    timer = setTimeout(() => resolve(null), Math.max(0, deadline - Date.now()));
  })]);
  clearTimeout(timer);
  if (child === FRAME_GONE) return child;
  return child && typeof child === 'object' && !child.error ? child : null;
}

// Reads the cross-origin frames a document lists ({ ref, frameId } each, in document order),
// each with await message(frame), in parallel: at most the budget's reads, each by its
// deadline. refsOf(child, frame) is the text holding the refs of a frame's answer, or null when
// the answer is not this read's. Returns the frames read with their answers (read) and how many
// were left out (unread); a document CROSS_ORIGIN_FRAME_DEPTH frames deep reads none (tooDeep).
// A frame the browser no longer finds is forgotten, so deltas stop reading it (knownFrameDeltas).
async function readFrames(tabId, frames, scope, message, refsOf) {
  if (scope.depth >= CROSS_ORIGIN_FRAME_DEPTH) return { read: [], unread: 0, tooDeep: true };
  const { budget } = scope;
  const reading = frames.slice(0, Math.max(0, budget.reads));
  budget.reads -= reading.length;
  const children = await Promise.all(reading.map(async frame => {
    const child = await readFrame(tabId, frame.frameId, await message(frame), budget.deadline);
    if (child === FRAME_GONE) frameParents.get(tabId)?.delete(frame.frameId);
    const refs = child && child !== FRAME_GONE && refsOf(child, frame);
    if (typeof refs !== 'string') return null;
    budget.answered.add(frame.frameId);
    noteFrameRefs(tabId, refs, frame.frameId);
    if (!frameParents.has(tabId)) frameParents.set(tabId, new Map());
    frameParents.get(tabId).set(frame.frameId, { parent: scope.parent, ref: frame.ref });
    return child;
  }));
  return { read: reading.map((frame, i) => ({ frame, child: children[i] })).filter(({ child }) => child),
    unread: frames.length - reading.length, tooDeep: false };
}

function unreadFramesHint(unread, how) {
  return `${unread} more cross-origin frame${unread > 1 ? 's were' : ' was'} not read; ${how}`;
}

// The refs in a read's answer (every ref field), for noteFrameRefs.
function resultRefs(value) {
  if (Array.isArray(value)) return value.map(resultRefs).join(' ');
  if (!value || typeof value !== 'object') return '';
  return Object.entries(value).map(([key, item]) => key === 'ref' && typeof item === 'string' ? item : resultRefs(item))
    .join(' ');
}

// A cross-origin frame's results as the document showing it sees them (#354): bounds move into
// its top viewport by where the frame's viewport starts there (frame.offset), and CSS
// selectors, which would run in that document, are dropped; refs reach the frame. Link URLs
// (browser_dom's href) are redacted like snapshot URLs: another site's query can hold tokens.
function fromFrame(value, frame) {
  if (Array.isArray(value)) return value.map(item => fromFrame(item, frame));
  if (!value || typeof value !== 'object') return value;
  const [x, y] = frame.offset || [0, 0];
  return Object.fromEntries(Object.entries(value).flatMap(([key, item]) => {
    if (key === 'selector') return [];
    if (key === 'href' && typeof item === 'string') return [[key, redactHistoryUrl(item)]];
    if (key === 'bounds' && Array.isArray(item) && item.length === 4) {
      return [[key, [Math.round(item[0] + x), Math.round(item[1] + y), item[2], item[3]]]];
    }
    return [[key, fromFrame(item, frame)]];
  }));
}

// The frames a snapshot lists, with the index of each one's line (at).
function listedFrames(frames, lines) {
  return frames.map(frame => ({ ...frame, at: lines.findIndex(line => line.includes(`[ref=${frame.ref}]`)) }))
    .filter(frame => frame.at >= 0);
}

// Lists each frame's own snapshot (child) under the frame's line, indented one level deeper. A
// frame read reads [frame=cross_origin src=…] instead of [content=cross_origin …]; frames past
// the depth limit read content=too_deep.
function placeFrameSnapshots(result, lines, listed, { read, unread, tooDeep }) {
  if (tooDeep) for (const { at } of listed) lines[at] = lines[at].replace('[content=cross_origin', '[content=too_deep');
  let cuts = result.cuts;
  // Bottom up, so the line numbers of frames above stay valid.
  for (const { frame: { at }, child } of [...read].sort((a, b) => b.frame.at - a.frame.at)) {
    const indent = ' '.repeat(lines[at].search(/\S/) + 2);
    const inserted = child.snapshot.split('\n')
      .filter(line => line && line !== '- … truncated; scope with selector, ref or max_depth')
      .map(line => indent + line);
    lines[at] = lines[at].replace('[content=cross_origin', '[frame=cross_origin');
    lines.splice(at + 1, 0, ...inserted);
    if (cuts) {
      const childCuts = (child.cuts || []).filter(cut => cut > 0 && cut < inserted.length);
      cuts = [...cuts.map(cut => cut > at ? cut + inserted.length : cut),
        ...childCuts.map(cut => cut + at + 1)].sort((a, b) => a - b);
    }
    if (child.truncated) result.truncated = true;
    // A delta is unchanged when its frames' deltas are; an action inside a frame names its target there.
    if (child.unchanged !== true) delete result.unchanged;
    if (child.target_ref && !result.target_ref) result.target_ref = child.target_ref;
  }
  if (unread && !result.hint) result.hint = unreadFramesHint(unread, 'snapshot a frame\'s ref to read it.');
  return { ...result, snapshot: lines.join('\n'), ...(cuts ? { cuts } : {}) };
}

const snapshotRefs = child => typeof child.snapshot === 'string' ? child.snapshot : null;

// Lists each cross-origin frame's own snapshot under the frame's line (#343). Frames are read
// in parallel, at most CROSS_ORIGIN_FRAME_READS per snapshot in document order and each by one
// deadline; the others, and frames that do not answer, stay as they were.
async function withFrameSnapshots(tabId, response, params, scope) {
  const { frames, ...result } = response;
  if (!frames?.length || typeof result.snapshot !== 'string') return result;
  const lines = result.snapshot.split('\n');
  const listed = listedFrames(frames, lines);
  const reads = await readFrames(tabId, listed, scope, () => ({
    action: 'getSnapshot', maxChars: params.maxChars, text: params.text, observation: params.observation,
    boxes: params.boxes, collapse: params.collapse, absoluteUrls: true,
  }), snapshotRefs);
  reads.read = await Promise.all(reads.read.map(async ({ frame, child }) =>
    ({ frame, child: await withFrameSnapshots(tabId, child, params, innerScope(scope, frame)) })));
  return placeFrameSnapshots(result, lines, listed, reads);
}

// ── Deltas with cross-origin frames (#354) ──
// A delta (an action's, or browser_wait_for(time)'s sinceObservation) covers the page and its
// cross-origin frames. The document the action ran in (actionFrame) diffs against its baseline
// from right before the action; every other one against what the agent last saw of it (its last
// whole-page snapshot, lastObservation in the content script). Frames read before
// (frameParents) are diffed first: a frame whose delta lists changes keeps its line in its
// parent's delta (keepRefs), with that delta below it. A frame whose line a delta lists for
// another reason (a new frame) is read too, in full when nothing of it was seen before.
function deltaMode(params, frameId) {
  return params.delta && (params.actionFrame ?? 0) === frameId ? { delta: true }
    : { delta: false, sinceObservation: true, focusLastAction: false };
}

// Whether a frame's delta lists changes. A frame with nothing to diff against answers in full
// (with text, textWhenFull). That is a change when the action ran in the frame or the frame
// holds another document than when it was read before (a form it submitted, a sign-in step),
// but not when no snapshot showed it yet (browser_find, browser_forms or browser_dom read it).
function frameChanged(child, frame, params, replaced) {
  return !!child.snapshot && (child.delta ? true : !!child.target_ref || frame.frameId === params.actionFrame || replaced);
}

// Whether a frame's refs are of a document no read of the frame has seen.
function replacedDocument(tabId, refs, frameId) {
  const generations = frameGenerations.get(tabId);
  return ![...refs.matchAll(/\bs(\d+)e\d+\b/g)].some(m => generations?.get(m[1]) === frameId);
}

// The frames, by ref, whose delta lists changes.
function changedFrameRefs(deltas) {
  return [...deltas].filter(([, child]) => child.changed).map(([ref]) => ref);
}

// The deltas of the frames read before under the scope's document, by the ref of their frame
// there. The frames holding the action's document come first, so the budget never leaves it out.
async function knownFrameDeltas(tabId, params, scope) {
  const links = frameParents.get(tabId) || new Map();
  const path = new Set();
  for (let id = params.actionFrame, n = 0; id && n < CROSS_ORIGIN_FRAME_DEPTH; id = links.get(id)?.parent, n++) path.add(id);
  const known = [...links].filter(([, link]) => link.parent === scope.parent)
    .map(([frameId, { ref }]) => ({ ref, frameId }))
    .sort((a, b) => path.has(b.frameId) - path.has(a.frameId));
  const { read } = await readFrameDeltas(tabId, known, params, scope);
  return new Map(read.map(({ frame, child }) => [frame.ref, child]));
}

// Each frame's delta, after the deltas of the frames read before inside it; changed says
// whether it lists changes (frameChanged).
async function readFrameDeltas(tabId, frames, params, scope) {
  const inner = new Map();
  const replaced = new Map();
  const reads = await readFrames(tabId, frames, scope, async frame => {
    inner.set(frame.frameId, await knownFrameDeltas(tabId, params, innerScope(scope, frame)));
    return { action: 'getSnapshot', maxChars: params.maxChars, text: params.text, observation: params.observation,
      focusLastAction: params.focusLastAction, absoluteUrls: true, textWhenFull: true, ...deltaMode(params, frame.frameId),
      keepRefs: changedFrameRefs(inner.get(frame.frameId)) };
  }, (child, frame) => {
    const refs = snapshotRefs(child);
    // Before readFrames notes this answer's refs.
    if (refs != null) replaced.set(frame.frameId, replacedDocument(tabId, refs, frame.frameId));
    return refs;
  });
  reads.read = await Promise.all(reads.read.map(async ({ frame, child }) => {
    const changed = frameChanged(child, frame, params, replaced.get(frame.frameId));
    const merged = await withFrameDeltas(tabId, child, params, innerScope(scope, frame), inner.get(frame.frameId));
    return { frame, child: { ...merged, changed: changed || changedFrameRefs(inner.get(frame.frameId)).length > 0 } };
  }));
  return reads;
}

// Lists the deltas of the frames a delta lists under their lines: known, the frames' deltas
// read before this one (knownFrameDeltas).
async function withFrameDeltas(tabId, response, params, scope, known) {
  const { frames, ...result } = response;
  if (!frames?.length || typeof result.snapshot !== 'string') return result;
  const lines = result.snapshot.split('\n');
  const listed = listedFrames(frames, lines);
  const reads = await readFrameDeltas(tabId, listed.filter(frame => !known.has(frame.ref)), params, scope);
  reads.read.push(...listed.filter(frame => known.has(frame.ref)).map(frame => ({ frame, child: known.get(frame.ref) })));
  return placeFrameSnapshots(result, lines, listed, reads);
}

// ── browser_find, browser_forms and browser_dom in cross-origin frames (#354) ──
// The content script's browser_find cap.
const FIND_LIMIT = 20;

// Each frame's matches join the page's. A query that matched as a CSS selector in a document
// returns its matches unscored, and those win, as they do within one document; otherwise the
// best scores across the documents, then the shorter text (the more specific element), the
// page's first among equals.
async function withFrameFinds(tabId, response, query, scope) {
  const { frames, ...result } = response;
  if (!frames?.length || !Array.isArray(result.results)) return result;
  const { read, unread } = await readFrames(tabId, frames, scope, () => ({ action: 'find', query }),
    child => Array.isArray(child.results) ? resultRefs(child) : null);
  const children = await Promise.all(read.map(({ frame, child }) =>
    withFrameFinds(tabId, child, query, innerScope(scope, frame))));
  const all = [result.results, ...children.map((child, i) => fromFrame(child.results, read[i].frame))].flat();
  const bySelector = all.filter(match => match.score == null);
  const results = (bySelector.length ? bySelector
    : all.sort((a, b) => b.score - a.score || (a.text?.length ?? 0) - (b.text?.length ?? 0))).slice(0, FIND_LIMIT);
  const { hint, ...rest } = result;
  const hints = [results.length ? null : hint, unread ? unreadFramesHint(unread, 'browser_snapshot reads a frame by its ref.') : null];
  return { ...rest, results, ...(hints.some(Boolean) ? { hint: hints.filter(Boolean).join(' ') } : {}) };
}

// Each frame's fields follow the page's, with frame set to the frame's title or src (as
// unavailableFrames names it) unless a frame inside it is named. A frame read leaves
// unavailableFrames; frames inside it that were not read join it.
async function withFrameForms(tabId, response, scope) {
  const { frames, ...result } = response;
  if (!frames?.length || !Array.isArray(result.fields)) return result;
  const { read, unread, tooDeep } = await readFrames(tabId, frames, scope, () => ({ action: 'getFormFields' }),
    child => Array.isArray(child.fields) ? resultRefs(child) : null);
  const children = await Promise.all(read.map(({ frame, child }) => withFrameForms(tabId, child, innerScope(scope, frame))));
  const { unavailableFrames = [], ...rest } = result;
  const entries = new Map(unavailableFrames.map(entry => [entry.ref, entry]));
  if (tooDeep) for (const { ref } of frames) if (entries.has(ref)) entries.get(ref).content = 'too_deep';
  const readRefs = new Set(read.map(({ frame }) => frame.ref));
  const fields = [...result.fields];
  const unavailable = unavailableFrames.filter(entry => !readRefs.has(entry.ref));
  read.forEach(({ frame }, i) => {
    const entry = entries.get(frame.ref);
    const name = entry?.title || entry?.src || '';
    fields.push(...fromFrame(children[i].fields, frame).map(field => ({ ...field, frame: field.frame || name })));
    unavailable.push(...(children[i].unavailableFrames || []));
  });
  return { ...rest, fields, ...(unavailable.length ? { unavailableFrames: unavailable } : {}),
    ...(unread ? { hint: unreadFramesHint(unread, 'browser_snapshot reads a frame by its ref.') } : {}) };
}

// The content script's browser_dom cap, in characters of the tree's JSON, and its hint.
const DOM_TREE_LIMIT = 20000;
const DOM_TRUNCATED_HINT = 'Tree cut at 20,000 chars; scope it with selector, ref or max_depth, or use browser_snapshot.';

// Each frame's tree becomes the child of its frame's node, which then has frame: 'cross_origin'
// instead of content. Frames are read to the depth left below their node and within what is
// left of the cap (limit); in document order, a frame's tree that no longer fits, or that has
// no room for even its root, is left out and the tree is truncated.
async function withFrameTrees(tabId, response, scope, maxDepth, limit = DOM_TREE_LIMIT) {
  const { frames, ...result } = response;
  if (!frames?.length || !result.tree) return result;
  let left = limit - JSON.stringify(result.tree).length;
  const depthBelow = frame => maxDepth - frame.depth - 1;
  const { read, unread, tooDeep } = await readFrames(tabId, frames.filter(frame => depthBelow(frame) >= 0), scope,
    frame => ({ action: 'getAccessibilityTree', depth: depthBelow(frame), limit: left }),
    child => child.tree ? resultRefs(child.tree) : child.truncated ? '' : null);
  const children = await Promise.all(read.map(({ frame, child }) =>
    withFrameTrees(tabId, child, innerScope(scope, frame), depthBelow(frame), left)));
  const nodes = new Map();
  (function index(node) {
    nodes.set(node.ref, node);
    (node.children || []).forEach(index);
  })(result.tree);
  if (tooDeep) for (const { ref } of frames) if (nodes.get(ref)?.content) nodes.get(ref).content = 'too_deep';
  read.forEach(({ frame }, i) => {
    const node = nodes.get(frame.ref);
    if (!node) return;
    const tree = fromFrame(children[i].tree, frame);
    const cost = tree ? JSON.stringify(tree).length + ',"children":[],'.length : Infinity;
    if (cost > left) {
      Object.assign(result, { truncated: true, hint: DOM_TRUNCATED_HINT });
      return;
    }
    left -= cost;
    delete node.content;
    Object.assign(node, { frame: 'cross_origin', children: [tree] });
    result.refs += children[i].refs || 0;
    if (children[i].truncated) Object.assign(result, { truncated: true, hint: DOM_TRUNCATED_HINT });
  });
  if (unread && !result.hint) result.hint = unreadFramesHint(unread, 'browser_dom with a frame\'s ref reads it.');
  return result;
}

// Public refs never reuse an index. Keep the generation-qualified ref private so the
// content script can still reject refs from another document or an earlier injection.
const shortRefs = new Map();
const publicRefs = new Map();
// Retired aliases still answer other_document, not not_in_snapshot; bounded, oldest first.
const retiredShortRefs = new Set();
const RETIRED_SHORT_REFS_MAX = 4096;
// Retire aliases with their document/tab, without resetting the persisted monotonic counter.
function pruneShortRefs(tabId) {
  frameGenerations.delete(tabId);
  frameParents.delete(tabId);
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
// A short ref given alone or starting a >> chain (e12 >> xpath=..): the ref and the rest.
// Surrounding whitespace is dropped, as the content script trims selectors.
function leadingShortRef(value) {
  const match = typeof value === 'string' && /^\s*(e\d+)(\s*>>[\s\S]*?)?\s*$/.exec(value);
  return match ? { ref: match[1], rest: match[2] || '' } : null;
}
const REF_RESULT_ACTIONS = new Set([
  'getSnapshot', 'getAccessibilityTree', 'getFormFields', 'find', 'waitForElement',
  'waitForText', 'getIframes', 'submitForm', 'openedPopup',
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
      'Take a new browser_snapshot of the intended tab (select it with browser_tabs, or pass tab_id).'
    : 'This ref is unknown or expired; take a new browser_snapshot and use its refs.';
  return { error: 'stale ref, call browser_snapshot again', code: 'stale_ref', retryable: true,
    hint, state: { reason, ref } };
}

function expandShortRefs(params, tabId) {
  const expanded = { ...params };
  for (const key of REF_PARAMS) {
    const { ref, rest } = leadingShortRef(params[key]) || {};
    if (!ref) continue;
    const entry = shortRefs.get(ref);
    if (!entry) return { error: shortRefError(ref, retiredShortRefs.has(ref) ? 'other_document' : 'not_in_snapshot') };
    if (entry.tabId !== tabId) return { error: shortRefError(ref, 'other_document') };
    expanded[key] = entry.ref + rest;
  }
  return { params: expanded };
}

// The owned ref follows the JSON-quoted name. Ref-like text inside that name or a
// quoted value is page data, never a target field; JSON escapes must stay inside it.
function snapshotLineRef(line) {
  return /^ *- [^"\n]+ "(?:[^"\\]|\\.)*" \[ref=(e\d+)\](?= |$)/.exec(line)?.[1];
}

// Enforce explicit snapshot budgets on the final public protocol, after aliases and URL
// redaction. Keep complete lines and their ancestors, prioritizing an action's target;
// without one, keep or skip whole spans between the content script's cuts, so a block it
// keeps whole (a product card) is never split.
function boundPublicSnapshot({ cuts, ...response }, params) {
  let budget = Number.isFinite(params.maxChars) ? Math.max(0, params.maxChars) : Infinity;
  if (params.text === true) budget = Math.min(budget, 12000);
  if (!Number.isFinite(budget) || typeof response.snapshot !== 'string') return response;
  if (!response.truncated && response.snapshot.length <= budget) return response;
  const marker = '- … truncated; scope with selector, ref or max_depth';
  const lines = response.snapshot.split('\n').filter(line => line && line !== marker);
  const isUrl = i => /^ *- \/url: /.test(lines[i]);
  const isLink = i => /^ *- link "/.test(lines[i]);
  // A link line without a /url line repeats the URL of the link line before it at its level
  // (#310); urlFrom is the /url line it then needs.
  const parents = [], urlFrom = [], stack = [], lastChild = new Map();
  for (let i = 0; i < lines.length; i++) {
    const indent = lines[i].length - lines[i].trimStart().length;
    while (stack.length && stack.at(-1).indent >= indent) stack.pop();
    parents[i] = stack.at(-1)?.index;
    stack.push({ indent, index: i });
  }
  const hasUrl = i => i + 1 < lines.length && isUrl(i + 1) && parents[i + 1] === i;
  for (let i = 0; i < lines.length; i++) {
    const previous = lastChild.get(parents[i]);
    lastChild.set(parents[i], i);
    if (isLink(i) && !hasUrl(i) && previous != null && isLink(previous)) {
      urlFrom[i] = hasUrl(previous) ? previous + 1 : urlFrom[previous];
    }
  }
  const kept = new Set();
  const showMarker = marker.length <= budget;
  const available = showMarker ? budget - marker.length : budget + 1;
  let used = 0;
  // The lines, each with its ancestors and a link's /url line (its own or the one it repeats):
  // all of them or none. With shortUrls, /url lines not kept yet become `- /url: "…"` (#310).
  const add = (indices, shortUrls = false) => {
    const block = new Set();
    const gather = index => {
      for (let i = index; i != null && !kept.has(i) && !block.has(i); i = parents[i]) {
        block.add(i);
        if (urlFrom[i] != null) gather(urlFrom[i]);
        if (isLink(i) && hasUrl(i)) gather(i + 1);
      }
    };
    indices.forEach(gather);
    const lineOf = i => shortUrls && isUrl(i) ? lines[i].replace(/- \/url: .*$/, '- /url: "…"') : lines[i];
    const cost = [...block].reduce((sum, i) => sum + lineOf(i).length + 1, 0);
    if (used + cost > available) return false;
    for (const i of block) {
      lines[i] = lineOf(i);
      kept.add(i);
    }
    used += cost;
    return true;
  };
  const target = response.target_ref ? lines.findIndex(line => snapshotLineRef(line) === response.target_ref) : -1;
  if (target >= 0) add([target]) || add([target], true);
  const ends = target < 0 && cuts ? cuts : lines.map((_, i) => i + 1);
  let from = 0;
  for (const to of ends) {
    // A span that does not fit whole is kept with shortened /url lines if it fits then (#310).
    const span = Array.from({ length: Math.max(0, to - from) }, (_, k) => from + k);
    if (!add(span)) add(span, true);
    from = to;
  }
  const selected = lines.filter((_, i) => kept.has(i));
  if (showMarker) selected.push(marker);
  const result = { ...response, snapshot: selected.join('\n'), truncated: true,
    hint: 'Snapshot truncated; scope with selector, ref or max_depth.' };
  if (response.target_ref && !selected.some(line => snapshotLineRef(line) === response.target_ref)) {
    delete result.target_ref;
    result.hint += ' The target and its ancestor context did not fit; take a scoped snapshot.';
  }
  return result;
}

// A same-origin link URL is a path (#310); it is redacted as a URL on a placeholder origin.
function redactSnapshotUrl(value) {
  if (!value.startsWith('/')) return redactHistoryUrl(value);
  const origin = 'https://kitsune.invalid';
  return redactHistoryUrl(origin + value).slice(origin.length);
}

function compactContentRefs(action, response, tabId, params) {
  // Only protocol-owned fields are rewritten, never page text, JS results or values.
  const visit = (value, convert) => {
    if (Array.isArray(value)) return value.map(item => visit(item, convert));
    if (!value || typeof value !== 'object') return value;
    return Object.fromEntries(Object.entries(value).map(([key, item]) => {
      if (action === 'getSnapshot' && value === response && key === 'url' && typeof item === 'string') {
        let url;
        try { url = new URL(item); } catch {}
        return [key, url && ['http:', 'https:'].includes(url.protocol) ? redactHistoryUrl(item) : undefined];
      }
      if (key === 'snapshot' && typeof item === 'string') {
        const snapshot = item.replace(/^( *- \/url: )("(?:[^"\\\r\n]|\\.)*")$/gm,
          (_, prefix, encoded) => prefix + JSON.stringify(redactSnapshotUrl(JSON.parse(encoded))));
        return [key, snapshot.replace(/\[ref=(s\d+e\d+)\]/g, (_, ref) => `[ref=${convert(ref)}]`)
          // A collapse marker names its container's ref, a landmark marker its landmark's.
          .replace(/^( *- … (?:\d+ more similar items|\d+ lines \(\d+ links\) not shown); snapshot )(s\d+e\d+)( to list all)$/gm,
            (_, head, ref, tail) => head + convert(ref) + tail)];
      }
      if (['ref', 'target_ref', 'currentRef', 'opened_by'].includes(key) && typeof item === 'string' && /^s\d+e\d+$/.test(item)) {
        // An error echoes the caller's ref, including legacy refs, verbatim.
        if (response.code && value === response.state && key === 'ref') {
          const original = REF_PARAMS.map(k => leadingShortRef(params[k])?.ref)
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
    const result = visit(response, ref => publicRefs.get(`${tabId}:${ref}`));
    return action === 'getSnapshot' ? boundPublicSnapshot(result, params) : result;
  };
  const pending = shortRefQueue.then(rewrite);
  shortRefQueue = pending.catch(() => {});
  return pending;
}

async function forwardToContent(action, params) {
  const tabId = params.tabId || (await getActiveTabId());
  if (!tabId) return noActiveTab('No active tab found');

  let retried = false;
  // A delta's frames read before (#354), read once even when the message is sent again.
  const delta = action === 'getSnapshot' && !!(params.delta || params.sinceObservation);
  let deltaScope = null, frameDeltas = null;
  while (true) {
    // Verify the target tab before anything can reach a page, including a retry.
    const state = await tabState(tabId);
    if (!state) return tabError(`Tab ${tabId} no longer exists`, 'tab_closed', { tabId });
    if (isRestrictedUrl(state.url)) {
      return tabError(`Cannot access browser internal page: ${state.url.split('?')[0]}`, 'restricted_page', state);
    }

    const expanded = expandShortRefs(params, tabId);
    if (expanded.error) return await withTabState(tabId, expanded.error);
    const frameId = targetFrame(tabId, expanded.params);
    try {
      await ensureContentScript(tabId, Infinity, frameId);
    } catch (e) {
      if (!(e instanceof NoContentReceiver)) return await injectionFailure(tabId, e);
      // Nothing was sent; the tab's state explains the silence where it can.
      const failure = await tabFailure(tabId, e.message, false);
      if (failure.code === 'content_unavailable') failure.state.reason = 'no_receiver';
      return failure;
    }

    let message = { action, ...expanded.params };
    if (delta) {
      deltaScope ??= { parent: frameId, depth: 0, budget: frameReadBudget() };
      frameDeltas ??= await knownFrameDeltas(tabId, params, deltaScope);
      message = { ...message, ...deltaMode(params, frameId), keepRefs: changedFrameRefs(frameDeltas) };
    }
    let response;
    try {
      response = await browser.tabs.sendMessage(tabId, message, { frameId });
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
    // Reads of cross-origin frames (#343, #354), with a budget of their own.
    const frameScope = () => ({ parent: frameId, depth: 0, budget: frameReadBudget() });
    if (typeof response.snapshot === 'string') {
      if (frameId) noteFrameRefs(tabId, response.snapshot, frameId);
      if (delta) {
        response = await withFrameDeltas(tabId, response, params, deltaScope, frameDeltas);
        // An action whose frame did not answer may have changed it.
        if (params.actionFrame && !deltaScope.budget.answered.has(params.actionFrame)) delete response.unchanged;
      } else if (action === 'getSnapshot') response = await withFrameSnapshots(tabId, response, params, frameScope());
    }
    if (action === 'find') response = await withFrameFinds(tabId, response, expanded.params.query, frameScope());
    if (action === 'getFormFields') response = await withFrameForms(tabId, response, frameScope());
    if (action === 'getAccessibilityTree') {
      response = await withFrameTrees(tabId, response, frameScope(), expanded.params.depth ?? 6);
    }
    if (action === 'screenPoint' && frameId && !response.error) {
      response = await frameScreenPoint(tabId, frameId, response, expanded.params);
      if (!response) return await tabFailure(tabId, 'No response from content script', true);
      if (response.code) response = await withTabState(tabId, response);
    }
    if (response.frames) delete response.frames;
    const result = await compactContentRefs(action, response, tabId, params);
    // Waits and drags echo their targets; keep their public spelling too.
    for (const key of REF_PARAMS) {
      if (!leadingShortRef(params[key])) continue;
      if (result[key] === expanded.params[key]) result[key] = params[key];
      if (result.state?.selector === expanded.params[key]) result.state.selector = params[key];
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

// Resizes the window holding the tab (default: the active tab); does not focus it.
async function resizeWindow(tabId, width, height) {
  try {
    if (![width, height].every(value => Number.isFinite(value) && value > 0)) {
      return { error: 'width and height must be positive numbers' };
    }
    const tab = tabId != null ? await browser.tabs.get(tabId)
      : (await browser.tabs.query({ active: true, currentWindow: true }))[0];
    if (!tab) return noActiveTab('No active tab found');
    const updated = await browser.windows.update(tab.windowId, {
      state: 'normal', width: Math.round(width), height: Math.round(height),
    });
    return { ok: true, windowId: updated.id, width: updated.width, height: updated.height };
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
  if (!r || r.error) {
    const error = new Error(r?.error || `No response from content operation ${action}`);
    error.screenshotResult = r;
    throw error;
  }
  return r;
}

async function zlScroll(tabId, scroll) {
  const r = await zlContent(tabId, 'captureScroll', scroll);
  return r.sy;
}

// 2.1.1: scale (1-4) = output pixels per CSS pixel. captureTab renders the viewport at that scale, so
// scale 2 gives a genuinely sharper image (text in article screenshots), not an upscale. Default 1 = old size.
async function elementScreenshot(tabId, selector, scale, preview, format) {
  let hidFixed = false, id = null, captureAttribute = null, stage = 'resolve target';
  const want = Math.min(Math.max(Number(scale) || 1, 1), 4);
  try {
    format = screenshotFormat(format);
    id = tabId || (await getActiveTabId());
    if (!id || !selector) return { error: 'tabId and selector required' };
    // Every selector uses the shared resolver, then capture/cleanup holds the exact node
    // (including shadow CSS and Playwright forms), not another document-only lookup.
    const target = await forwardToContent('markScreenshotTarget', { tabId: id, selector });
    if (target.error) return target;
    selector = target.selector;
    captureAttribute = target.attribute;
    const tab = await browser.tabs.get(id);
    if (isRestrictedUrl(tab.url)) return { error: 'Cannot access browser internal page' };
    const measure = async () => {
      stage = 'captureMeasure';
      const r = await zlContent(id, 'captureMeasure', { selector });
      return r && !r.missing ? r : null;
    };
    let m = await measure();
    if (!m) return { error: 'No element matches: ' + selector };
    if (m.w < 1 || m.h < 1) return { error: 'Element has no size: ' + selector };
    const tall = m.h > m.vh;
    stage = 'captureScrollIntoView';
    await zlContent(id, 'captureScrollIntoView', { selector, block: tall ? 'start' : 'center' });
    await zlSleep(150);
    m = await measure();
    if (!m) return { error: 'No element matches: ' + selector };
    const cssW = Math.min(m.w, CANVAS_MAX / want), cssH = Math.min(m.h, CANVAS_MAX / want);
    const W = Math.max(1, Math.round(cssW * want)), H = Math.max(1, Math.round(cssH * want));
    stage = 'create element canvas';
    const canvas = new OffscreenCanvas(W, H);
    const ctx = canvas.getContext('2d');
    const shot = want > 1 ? { format: 'png', scale: want } : { format: 'png' };
    let covered = 0, segments = 0;
    // 2.1.0: elements taller than the viewport are scrolled and stitched (they used to come back blank below the fold)
    for (let guard = 0; guard < 60 && covered < cssH; guard++) {
      stage = 'tabs.captureTab';
      const dataUrl = await browser.tabs.captureTab(id, shot);
      stage = 'decode element segment';
      const img = await loadDataUrl(dataUrl);
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
      stage = 'hideFixed';
      if (!hidFixed) { await zlContent(id, 'hideFixed', { selector }); hidFixed = true; }
      const before = m.sy;
      stage = 'captureScroll';
      await zlScroll(id, { by: Math.max(50, Math.floor(m.vh * 0.85)) });
      await zlSleep(160);
      m = await measure();
      if (!m) return { error: 'No element matches: ' + selector };
      if (m.sy === before) break;   // can't scroll any further
    }
    stage = 'encode element image';
    const cropped = await encodeScreenshotCanvas(canvas, format);
    stage = 'image preview';
    return { ok: true, selector, bounds: { x: m.x, y: m.y, width: W, height: H }, scale: want, segments,
             complete: covered >= cssH - 1, dataUrl: preview ? await imagePreview(cropped) : cropped };
  } catch (e) { return screenshotFailure(e, stage, id); }
  finally {
    if (captureAttribute) {
      try { await forwardToContent('unmarkScreenshotTarget', { tabId: id, attribute: captureAttribute }); } catch (e) {}
    }
    if (hidFixed) { try { await zlContent(id, 'restoreFixed'); } catch (e) {} }
  }
}

async function fullPageScreenshot(tabId, preview, format) {
  let hidFixed = false, id = null, stage = 'resolve tab';
  try {
    format = screenshotFormat(format);
    id = tabId || (await getActiveTabId());
    if (!id) return { error: 'No tab' };
    const tab = await browser.tabs.get(id);
    if (isRestrictedUrl(tab.url)) return { error: 'Cannot access browser internal page' };
    stage = 'fullPageMetrics';
    const m = await zlContent(id, 'fullPageMetrics');
    const vh = m.viewportHeight, vw = m.viewportWidth;
    const docH = Math.min(m.docHeight, CANVAS_MAX), docW = Math.min(Math.max(m.docWidth, 1), CANVAS_MAX);
    const segments = [];
    let y = 0;
    for (let i = 0; i < 40 && y < docH; i++) {
      stage = 'captureScroll';
      await zlScroll(id, { to: y });
      await zlSleep(150);   // let lazy content settle
      const sy = Number(await zlScroll(id, {})) || 0;   // the page may clamp the last scroll
      stage = 'tabs.captureTab';
      segments.push({ y: sy, data: await browser.tabs.captureTab(id, { format: 'png' }) });
      stage = 'hideFixed';
      if (!hidFixed) { await zlContent(id, 'hideFixed', { selector: null }); hidFixed = true; }
      if (sy + vh >= docH) break;
      y = sy + vh;
    }
    stage = 'stitch full-page canvas';
    const canvas = new OffscreenCanvas(Math.round(Math.min(Math.max(docW, vw), CANVAS_MAX)), Math.round(docH));
    const ctx = canvas.getContext('2d');
    for (const seg of segments) {
      const img = await loadDataUrl(seg.data);
      const scale = img.width / vw;   // device pixels per CSS pixel (2.1.0: was ignored -> cropped on HiDPI)
      const drawH = Math.min(vh, docH - seg.y);
      if (drawH <= 0) continue;
      ctx.drawImage(img, 0, 0, img.width, drawH * scale, 0, seg.y, vw, drawH);
    }
    stage = 'encode full-page image';
    const dataUrl = await encodeScreenshotCanvas(canvas, format);
    stage = 'image preview';
    return { ok: true, width: canvas.width, height: canvas.height, segments: segments.length,
             truncated: m.docHeight > CANVAS_MAX, dataUrl: preview ? await imagePreview(dataUrl) : dataUrl };
  } catch (e) { return screenshotFailure(e, stage, id); }
  finally {
    if (hidFixed) { try { await zlContent(id, 'restoreFixed'); } catch (e) {} }
  }
}

async function encodeScreenshotCanvas(canvas, format) {
  const mime = `image/${format}`;
  if (format === 'jpeg') {
    // JPEG has no alpha channel; composite transparent/uncovered pixels onto white.
    const ctx = canvas.getContext('2d');
    ctx.globalCompositeOperation = 'destination-over';
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  const blob = await canvas.convertToBlob({ type: mime, quality: 0.9 });
  if (blob.type !== mime) throw new Error(`Screenshot encoder returned ${blob.type || 'no MIME type'} instead of ${mime}`);
  return arrayBufferToDataUrl(await blob.arrayBuffer(), blob.type);
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
  const frame = await targetFrameInfo(tabId, params);
  if (!params.inspect) return { tabId, url: state.url, ...frame };
  const target = await forwardToContent('describeTarget', { ...params, tabId });
  return target.error ? target : { tabId, url: state.url, ...frame, target };
}

// { frame: { url } } when the command's ref points into a cross-origin frame (#343): url is
// the frame document's origin and path, null when the frame does not answer within a second.
async function targetFrameInfo(tabId, params) {
  const expanded = expandShortRefs(params, tabId);
  const frameId = expanded.params ? targetFrame(tabId, expanded.params) : 0;
  if (!frameId) return {};
  let timer;
  const ping = await Promise.race([
    browser.tabs.sendMessage(tabId, { action: 'ping' }, { frameId }).catch(() => null),
    new Promise(resolve => { timer = setTimeout(() => resolve(null), 1000); }),
  ]);
  clearTimeout(timer);
  return { frame: { url: typeof ping?.frameUrl === 'string' ? ping.frameUrl : null } };
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
    // Nothing reports a popup this click opens, so the content script does not keep watching.
    const click = await forwardToContent('click', { tabId: id, selector, first, reportPopup: false });
    if (click.error) return { ...click, clicked: false };
    const nav = await navPromise;
    return { ok: true, clicked: true, navigation: nav };
  } catch (e) { return { error: e.message }; }
  finally { cleanup?.(); }
}

// == Init ==
connect();
// onStartup and onInstalled fire just after the connect() above, on browser start and on
// install or update. Connecting again would replace a connection that is opening or open (#299).
function connectUnlessConnected() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
  connect();
}
browser.runtime.onStartup.addListener(connectUnlessConnected);
browser.runtime.onInstalled.addListener(() => {
  console.log('[Kitsune] Extension installed');
  connectUnlessConnected();
});
