// Kitsune - Background Script
// Connects to local bridge server via WebSocket, relays commands to content scripts

'use strict';

const EXPECTED_CONTENT_VERSION = 31;
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
  mainFrameStatus.delete(tabId);
  tabReloads.delete(tabId);
  tabLoads.delete(tabId);
  notifyStatusWaiters(tabId, true);
});
browser.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.discarded === true) mainFrameStatus.delete(tabId);
  // The browser also wakes discarded tabs, e.g. when one is selected.
  if (changeInfo.discarded === false) tabReloads.set(tabId, Date.now());
  if (changeInfo.status === 'loading') tabLoads.set(tabId, { at: Date.now(), complete: false });
  const load = tabLoads.get(tabId);
  if (load && changeInfo.status === 'complete') load.complete = true;
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
  }
});
browser.tabs.onRemoved.addListener(tabId => {
  actionStarts.delete(tabId);
  linkFallbackTabs.delete(tabId);
  for (let i = openedTabs.length - 1; i >= 0; i--) {
    if (openedTabs[i].tabId === tabId || openedTabs[i].openerTabId === tabId) openedTabs.splice(i, 1);
  }
});
browser.runtime.onMessage.addListener((msg, sender) => {
  if (msg.action === 'getDialogPolicy') {
    return Promise.resolve(dialogPolicies.get(sender.tab?.id) || null);
  }
});

// == WebSocket Connection ==

// Fingerprint of the scripts this copy loaded, sent in hello: the bridge compares it with the
// unpacked extension on disk to report a developer build edited but not reloaded. Mirrors
// extension_source_hash in src/kitsune/version.py; null when it cannot be computed.
const SOURCE_HASH = (async () => {
  try {
    const manifest = browser.runtime.getManifest();
    const scripts = [...new Set([...(manifest.background?.scripts || []),
      ...(manifest.content_scripts || []).flatMap(entry => entry.js || [])])];
    if (!scripts.length) return null;
    const encoder = new TextEncoder();
    const separator = new Uint8Array([0]);
    const parts = [];
    for (const name of scripts) {
      const bytes = await (await fetch(browser.runtime.getURL(name))).arrayBuffer();
      parts.push(encoder.encode(name), separator, new Uint8Array(bytes), separator);
    }
    const digest = await crypto.subtle.digest('SHA-256', await new Blob(parts).arrayBuffer());
    return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
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
      const sourceHash = await SOURCE_HASH;
      if (ws !== socket) return; // replaced while the fingerprint was computed
      ws.send(JSON.stringify({ type: 'hello', version: browser.runtime.getManifest().version, build: KITSUNE_BUILD,
        sourceHash }));
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
// Page actions on one existing tab (the bridge's outcome actions) report opened_tabs.
const OPENER_ACTIONS = new Set([
  'click', 'doubleClick', 'type', 'fill', 'setEditableContent', 'selectOption', 'checkBox',
  'keypress', 'submitForm', 'formFill', 'hover', 'scroll', 'drag', 'focusElement', 'blurElement',
  'navigate', 'goBack', 'goForward', 'reloadTabBrowser', 'clickAndWaitNavigation',
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
  // Text snapshots report which tab they read.
  const textSnapshot = params.action === 'getSnapshot' && !!params.text;
  const tracksOpenedTabs = OPENER_ACTIONS.has(params.action);
  if ((withSnapshot || textSnapshot || tracksOpenedTabs) && params.tabId == null) params.tabId = await getActiveTabId();
  if (tracksOpenedTabs && params.tabId != null) actionStarts.set(params.tabId, tabsCreated);
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
      });
      if (snapshot.error) result.snapshot_error = snapshot.error;
      else {
        result.snapshot = snapshot.snapshot;
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
      });
      linkFallbackTabs.add(tab.id);
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
    case 'closeTab':     return await closeTab(params.tabId);
    case 'wakeTab':      return await wakeTab(params.tabId);
    case 'reloadExtension': return reloadExtensionAction();
    case 'getPageTextFromTab': return await getPageText(params);

    // 1.4.0 browser-level additions
    case 'pinTab':           return await updateTab(params.tabId, { pinned: !!params.pinned });
    case 'muteTab':          return await updateTab(params.tabId, { muted: !!params.muted });
    case 'duplicateTab':     return await duplicateTab(params.tabId);
    case 'reloadTabBrowser': return await reloadTabBrowser(params.tabId, params.bypassCache);
    case 'goBack':           return await goBack(params.tabId);
    case 'goForward':        return await goForward(params.tabId);
    case 'getZoom':          return await getZoom(params.tabId);
    case 'setZoom':          return await setZoom(params.tabId, params.factor);
    case 'getWindows':       return await getWindowsList();
    case 'createWindow':     return await createWindow(params.url, params.incognito);
    case 'closeWindow':      return await closeWindow(params.windowId);
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
    case 'clickAndWaitNavigation': return await clickAndWaitNavigation(params.tabId, params.selector, params.timeout);
    case 'describeTarget':   return await describeTarget(params);

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
    case 'screenPoint':
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

async function captureScreenshot(params = {}) {
  try {
    const dataUrl = await browser.tabs.captureVisibleTab(null, { format: 'png', quality: 90 });
    const tabs = await browser.tabs.query({ active: true, currentWindow: true });
    const tab = tabs[0];
    return {
      dataUrl: params.imagePreview ? await imagePreview(dataUrl) : dataUrl,
      url: tab?.url || '',
      title: tab?.title || '',
      width: tab?.width || 0,
      height: tab?.height || 0
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
        active: t.active, windowId: t.windowId, index: t.index
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

async function switchTab(tabId) {
  try {
    await browser.tabs.update(tabId, { active: true });
    return { ok: true };
  } catch (e) {
    return await tabApiError(tabId, e);
  }
}

// Immediate by default. With wait, resolves once the tab loads (or the timeout passes, leaving
// the tab open) and reports the actual URL, so redirects are visible.
async function createTab(url, { wait = false, timeout = 15000 } = {}) {
  try {
    const target = url || 'about:blank';
    const tab = await browser.tabs.create({ url: target });
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

async function closeTab(tabId) {
  try {
    await browser.tabs.remove(tabId);
    return { ok: true };
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

async function forwardToContent(action, params) {
  const tabId = params.tabId || (await getActiveTabId());
  if (!tabId) return noActiveTab('No active tab found');

  // Verify the target tab before anything can reach a page.
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

  let response;
  try {
    response = await browser.tabs.sendMessage(tabId, { action, ...params });
  } catch (e) {
    // Without a receiver the message never reached the page.
    return await tabFailure(tabId, `Content script error: ${e.message}`,
      !/Receiving end does not exist/.test(e.message));
  }
  if (!response) return await tabFailure(tabId, 'No response from content script', true);
  return response.code ? await withTabState(tabId, response) : response;
}

async function getActiveTabId() {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  return tabs[0]?.id || null;
}

// ══════════════════════════════════════════════════════════════
// 1.4.0 — browser-level handler implementations
// ══════════════════════════════════════════════════════════════

async function updateTab(tabId, props) {
  try {
    const id = tabId || (await getActiveTabId());
    if (!id) return { error: 'No tab' };
    const tab = await browser.tabs.update(id, props);
    return { ok: true, tabId: id, pinned: tab.pinned, muted: tab.mutedInfo?.muted };
  } catch (e) { return { error: e.message }; }
}

async function duplicateTab(tabId) {
  try {
    const id = tabId || (await getActiveTabId());
    if (!id) return { error: 'No tab' };
    const dup = await browser.tabs.duplicate(id);
    return { ok: true, tabId: dup.id, sourceTabId: id };
  } catch (e) { return { error: e.message }; }
}

async function reloadTabBrowser(tabId, bypassCache) {
  try {
    const id = tabId || (await getActiveTabId());
    if (!id) return { error: 'No tab' };
    markReload(id);
    await browser.tabs.reload(id, { bypassCache: !!bypassCache });
    return { ok: true, tabId: id, bypassCache: !!bypassCache };
  } catch (e) { return { error: e.message }; }
}

async function goBack(tabId) {
  try {
    const id = tabId || (await getActiveTabId());
    if (!id) return { error: 'No tab' };
    await browser.tabs.goBack(id);
    return { ok: true, tabId: id };
  } catch (e) { return { error: e.message }; }
}

async function goForward(tabId) {
  try {
    const id = tabId || (await getActiveTabId());
    if (!id) return { error: 'No tab' };
    await browser.tabs.goForward(id);
    return { ok: true, tabId: id };
  } catch (e) { return { error: e.message }; }
}

async function getZoom(tabId) {
  try {
    const id = tabId || (await getActiveTabId());
    if (!id) return { error: 'No tab' };
    const factor = await browser.tabs.getZoom(id);
    return { ok: true, tabId: id, factor };
  } catch (e) { return { error: e.message }; }
}

async function setZoom(tabId, factor) {
  try {
    const id = tabId || (await getActiveTabId());
    if (!id) return { error: 'No tab' };
    await browser.tabs.setZoom(id, Number(factor) || 1);
    return { ok: true, tabId: id, factor: Number(factor) || 1 };
  } catch (e) { return { error: e.message }; }
}

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
    const w = await browser.windows.create({
      url: urls,
      incognito: !!incognito,
    });
    return { ok: true, windowId: w.id, tabIds: (w.tabs || []).map(t => t.id) };
  } catch (e) { return { error: e.message }; }
}

async function closeWindow(windowId) {
  try {
    await browser.windows.remove(windowId);
    return { ok: true, windowId };
  } catch (e) { return { error: e.message }; }
}

async function focusWindow(windowId) {
  try {
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
    const w = await browser.windows.create({ tabId });
    return { ok: true, windowId: w.id, tabId };
  } catch (e) { return { error: e.message }; }
}

async function activateTabForCapture(tabId) {
  const tab = await browser.tabs.get(tabId);
  const activeTabs = await browser.tabs.query({ windowId: tab.windowId, active: true });
  const previousTabId = activeTabs[0]?.id !== tabId ? activeTabs[0]?.id : null;
  if (previousTabId) {
    await browser.tabs.update(tabId, { active: true });
    await new Promise(r => setTimeout(r, 100));
  }
  return { tab, previousTabId };
}

async function restoreTabAfterCapture(previousTabId) {
  if (!previousTabId) return;
  try { await browser.tabs.update(previousTabId, { active: true }); } catch {}
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

async function zlHideFixed(tabId, selector) {
  await zlContent(tabId, 'hideFixed', { selector });
}

async function zlRestoreFixed(tabId) {
  await zlContent(tabId, 'restoreFixed');
}

async function zlScroll(tabId, scroll) {
  const r = await zlContent(tabId, 'captureScroll', scroll);
  return r ? r.sy : null;
}

// 2.1.1: scale (1-4) = output pixels per CSS pixel. captureVisibleTab renders the viewport at that scale, so
// scale 2 gives a genuinely sharper image (text in article screenshots), not an upscale. Default 1 = old size.
async function elementScreenshot(tabId, selector, scale, preview) {
  let previousTabId = null, hidFixed = false, id = null, captureAttribute = null;
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
    ({ previousTabId } = await activateTabForCapture(id));
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
    const cssW = Math.min(m.w, CANVAS_MAX / want), cssH = Math.min(m.h, CANVAS_MAX / want);
    const W = Math.max(1, Math.round(cssW * want)), H = Math.max(1, Math.round(cssH * want));
    const canvas = new OffscreenCanvas(W, H);
    const ctx = canvas.getContext('2d');
    const shot = want > 1 ? { format: 'png', scale: want } : { format: 'png' };
    let covered = 0, segments = 0;
    // 2.1.0: elements taller than the viewport are scrolled and stitched (they used to come back blank below the fold)
    for (let guard = 0; guard < 60 && covered < cssH; guard++) {
      const img = await loadDataUrl(await browser.tabs.captureVisibleTab(tab.windowId, shot));
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
      if (!hidFixed) { await zlHideFixed(id, selector); hidFixed = true; }
      const before = m.sy;
      await zlScroll(id, { by: Math.max(50, Math.floor(m.vh * 0.85)) });
      await zlSleep(160);
      m = await measure();
      if (!m || m.sy === before) break;   // can't scroll any further
    }
    const blob = await canvas.convertToBlob({ type: 'image/png' });
    const cropped = arrayBufferToDataUrl(await blob.arrayBuffer(), 'image/png');
    return { ok: true, selector, bounds: { x: m.x, y: m.y, width: W, height: H }, scale: want, segments,
             complete: covered >= cssH - 1, dataUrl: preview ? await imagePreview(cropped) : cropped };
  } catch (e) { return { error: 'elementScreenshot failed: ' + e.message }; }
  finally {
    if (captureAttribute && id) {
      try { await forwardToContent('unmarkScreenshotTarget', { tabId: id, attribute: captureAttribute }); } catch (e) {}
    }
    if (hidFixed && id) { try { await zlRestoreFixed(id); } catch (e) {} }
    await restoreTabAfterCapture(previousTabId);
  }
}

async function fullPageScreenshot(tabId, preview) {
  let previousTabId = null, hidFixed = false, id = null;
  try {
    id = tabId || (await getActiveTabId());
    if (!id) return { error: 'No tab' };
    const tab = await browser.tabs.get(id);
    if (isRestrictedUrl(tab.url)) return { error: 'Cannot access browser internal page' };
    ({ previousTabId } = await activateTabForCapture(id));
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
      segments.push({ y: sy, data: await browser.tabs.captureVisibleTab(tab.windowId, { format: 'png' }) });
      if (!hidFixed) { await zlHideFixed(id, null); hidFixed = true; }
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
    if (hidFixed && id) { try { await zlRestoreFixed(id); } catch (e) {} }
    await restoreTabAfterCapture(previousTabId);
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
async function clickAndWaitNavigation(tabId, selector, timeout) {
  try {
    const id = tabId || (await getActiveTabId());
    if (!id || !selector) return { error: 'tabId and selector required' };
    const t0 = Date.now();
    const navPromise = new Promise((resolve) => {
      const max = timeout || 15000;
      const to = setTimeout(() => { browser.tabs.onUpdated.removeListener(listener); resolve({ ok: false, timedOut: true }); }, max);
      function listener(updatedTabId, changeInfo, tab) {
        if (updatedTabId === id && changeInfo.status === 'complete') {
          clearTimeout(to);
          browser.tabs.onUpdated.removeListener(listener);
          resolve({ ok: true, url: tab.url, title: tab.title, elapsed: Date.now() - t0 });
        }
      }
      browser.tabs.onUpdated.addListener(listener);
    });
    const click = await forwardToContent('click', { tabId: id, selector });
    if (click.error) return { ...click, clicked: false };
    const nav = await navPromise;
    return { ok: true, clicked: true, navigation: nav };
  } catch (e) { return { error: e.message }; }
}

// == Init ==
connect();
browser.runtime.onStartup.addListener(connect);
browser.runtime.onInstalled.addListener(() => {
  console.log('[Kitsune] Extension installed');
  connect();
});
