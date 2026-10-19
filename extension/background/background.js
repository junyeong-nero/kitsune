// Claude Bridge for Zen - Background Script
// Connects to local bridge server via WebSocket, relays commands to content scripts

'use strict';

const EXPECTED_CONTENT_VERSION = 22;
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
browser.tabs.onRemoved.addListener(tabId => dialogPolicies.delete(tabId));
browser.runtime.onMessage.addListener((msg, sender) => {
  if (msg.action === 'getDialogPolicy') {
    return Promise.resolve(dialogPolicies.get(sender.tab?.id) || null);
  }
});

// == WebSocket Connection ==

function connect() {
  // Kill any existing connection first
  cleanup();
  
  try {
    ws = new WebSocket(WS_URL);
    
    ws.onopen = () => {
      console.log('[Claude Bridge] Connected to bridge server');
      reconnectDelay = 1000; // Reset backoff
      stopReconnect();
      ws.send(JSON.stringify({ type: 'hello', version: browser.runtime.getManifest().version, build: KITSUNE_BUILD }));
      startHeartbeat();
      injectAllTabs(); // Inject content scripts into all existing tabs
    };
    
    ws.onmessage = async (event) => {
      let cmdId = null;
      try {
        const command = JSON.parse(event.data);
        cmdId = command.id;
        console.log('[Claude Bridge] <<', command.action, cmdId);
        
        const result = await handleCommand(command);
        
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ id: cmdId, result }));
        }
      } catch (e) {
        console.error('[Claude Bridge] Command error:', e);
        if (cmdId && ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ id: cmdId, result: { error: e.message } }));
        }
      }
    };
    
    ws.onclose = () => {
      console.log('[Claude Bridge] Disconnected');
      cleanup();
      scheduleReconnect();
    };
    
    ws.onerror = () => {
      console.error('[Claude Bridge] WebSocket error');
      // onclose will fire after this, which handles reconnect
    };
  } catch (e) {
    console.error('[Claude Bridge] Connection failed:', e);
    cleanup();
    scheduleReconnect();
  }
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
  console.log(`[Claude Bridge] Reconnecting in ${reconnectDelay}ms...`);
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
      console.log('[Claude Bridge] Heartbeat: connection dead');
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
    console.log(`[Claude Bridge] Injected content script: ${injected} tabs, ${skipped} skipped`);
  } catch (e) {
    console.error('[Claude Bridge] Tab injection error:', e);
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

const POST_SNAPSHOT_ACTIONS = new Set([
  'click', 'type', 'fill', 'selectOption', 'checkBox', 'keypress', 'submitForm', 'navigate'
]);

async function handleCommand(command) {
  const params = { ...command };
  // Descriptions are audit metadata only; never use them to locate elements.
  if (params.ref) params.selector = params.ref;
  if (params.action === 'drag') {
    if (params.ref) params.from = params.ref;
    if (params.toRef) params.to = params.toRef;
  }
  const withSnapshot = POST_SNAPSHOT_ACTIONS.has(params.action) && params.includeSnapshot !== false;
  if (withSnapshot && params.tabId == null) params.tabId = await getActiveTabId();
  const result = await dispatchCommand(params);
  if (withSnapshot && result && !result.error && result.ok !== false) {
    try {
      const snapshot = await forwardToContent('getSnapshot', {
        tabId: params.tabId, maxChars: 4000, focusLastAction: params.action !== 'navigate',
      });
      if (snapshot.error) result.snapshot_error = snapshot.error;
      else {
        result.snapshot = snapshot.snapshot;
        if (snapshot.truncated) result.snapshot_truncated = true;
        if (snapshot.target_ref) result.target_ref = snapshot.target_ref;
      }
    } catch (e) { result.snapshot_error = e.message; }
  }
  return result;
}

async function dispatchCommand(command) {
  const { action, id, ...params } = command;
  
  switch (action) {
    // Browser-level commands (handled here)
    case 'screenshot':   return await captureScreenshot(params);
    case 'getTabs':      return await getOpenTabs();
    case 'waitForUrl':   return await waitForTabProperty('url', params);
    case 'waitForTitle': return await waitForTabProperty('title', params);
    case 'navigate':     return await navigateTab(params.url, params.tabId, params.expectTitle);
    case 'switchTab':    return await switchTab(params.tabId);
    case 'newTab':       return await createTab(params.url);
    case 'closeTab':     return await closeTab(params.tabId);
    case 'wakeTab':      return await wakeTab(params.tabId);
    case 'reloadExtension': return reloadExtensionAction();
    case 'getPageTextFromTab': return await forwardToContent('getPageText', params);

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
    case 'waitForText':
    case 'getPageInfo':
    case 'getPageText':
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
    if (targetTabId == null) return { error: 'No active tab found' };
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

async function navigateTab(url, tabId, expectTitle) {
  try {
    const targetTabId = tabId || (await getActiveTabId());
    if (!targetTabId) return { error: 'No active tab' };
    await browser.tabs.update(targetTabId, { url });

    // Wait for page to finish loading before returning
    await new Promise((resolve) => {
      const timeout = setTimeout(() => {
        browser.tabs.onUpdated.removeListener(listener);
        resolve();
      }, 15000);

      function listener(updatedTabId, changeInfo, tab) {
        if (updatedTabId === targetTabId && changeInfo.status === 'complete' && tab.url && !tab.url.startsWith('about:')) {
          clearTimeout(timeout);
          browser.tabs.onUpdated.removeListener(listener);
          resolve();
        }
      }
      browser.tabs.onUpdated.addListener(listener);
    });

    // Check if page title matches expectation (catches silent redirects)
    const result = { ok: true, url };
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
    return { error: e.message };
  }
}

async function switchTab(tabId) {
  try {
    await browser.tabs.update(tabId, { active: true });
    return { ok: true };
  } catch (e) {
    return { error: e.message };
  }
}

async function createTab(url) {
  try {
    const tab = await browser.tabs.create({ url: url || 'about:blank' });
    return { ok: true, tabId: tab.id };
  } catch (e) {
    return { error: e.message };
  }
}

async function closeTab(tabId) {
  try {
    await browser.tabs.remove(tabId);
    return { ok: true };
  } catch (e) {
    return { error: e.message };
  }
}

function reloadExtensionAction() {
  // Defer the reload so we can ack first — the WS dies the moment we reload,
  // and the bridge's pending future would otherwise hang. The new background
  // script comes up cold and reconnects via the existing onclose backoff.
  setTimeout(() => {
    try { browser.runtime.reload(); } catch (e) { console.error('[Claude Bridge] Reload failed:', e); }
  }, 100);
  return { ok: true, reloading: true };
}

async function wakeTab(tabId) {
  // Reload a discarded tab and wait for it to be ready. Idempotent — returns
  // immediately if the tab is already alive. Use before sending commands to a
  // tab that may have been idle long enough for Zen's Tab Unloader to discard.
  if (!tabId) return { error: 'wakeTab requires tabId' };
  try {
    const tab = await browser.tabs.get(tabId);
    if (!tab.discarded) {
      return { ok: true, woken: false, discarded: false };
    }
    await browser.tabs.reload(tabId);
    const ready = await new Promise((resolve) => {
      const timeout = setTimeout(() => {
        browser.tabs.onUpdated.removeListener(listener);
        resolve(false);
      }, 15000);
      function listener(updatedTabId, changeInfo) {
        if (updatedTabId === tabId && changeInfo.status === 'complete') {
          clearTimeout(timeout);
          browser.tabs.onUpdated.removeListener(listener);
          resolve(true);
        }
      }
      browser.tabs.onUpdated.addListener(listener);
    });
    return { ok: true, woken: true, ready };
  } catch (e) {
    return { error: e.message };
  }
}

// == Forward to Content Script ==

async function forwardToContent(action, params) {
  const tabId = params.tabId || (await getActiveTabId());
  if (!tabId) return { error: 'No active tab found' };
  
  // Check for restricted pages
  try {
    const tab = await browser.tabs.get(tabId);
    if (isRestrictedUrl(tab.url)) {
      return { error: `Cannot access browser internal page: ${tab.url.split('?')[0]}` };
    }
  } catch {}
  
  // Check if content script is loaded and up-to-date
  let needsInject = false;
  try {
    const ping = await browser.tabs.sendMessage(tabId, { action: 'ping' });
    if (!ping || ping.version < EXPECTED_CONTENT_VERSION) needsInject = true;
  } catch {
    needsInject = true;
  }

  if (needsInject) {
    try {
      await browser.tabs.executeScript(tabId, { file: '/content/content.js' });
      await new Promise(r => setTimeout(r, 150));
    } catch (e) {
      return { error: `Cannot inject content script: ${e.message}` };
    }
  }

  try {
    const response = await browser.tabs.sendMessage(tabId, { action, ...params });
    return response || { error: 'No response from content script' };
  } catch (e) {
    return { error: `Content script error: ${e.message}` };
  }
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
    console.error('[Claude Bridge] webRequest unavailable:', e);
  }
}

function removeInterceptListener() {
  if (!__intercept.listening) return;
  try {
    browser.webRequest.onBeforeRequest.removeListener(interceptListener);
    __intercept.listening = false;
  } catch {}
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
  console.log('[Claude Bridge] Extension installed');
  connect();
});
