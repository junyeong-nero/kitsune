// Run with: node tests/shared_browser.cjs (no browser or npm dependencies).
// Issue #139: the user and the agent share one browser. Agent work never takes the user's focus,
// closes only the tabs the agent opened, and backs off from a tab the user is using.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function event() {
  const listeners = new Set();
  return {
    listeners,
    addListener: fn => listeners.add(fn),
    removeListener: fn => listeners.delete(fn),
    emit: (...args) => [...listeners].forEach(fn => fn(...args)),
  };
}

const flush = () => new Promise(resolve => setImmediate(resolve));
const copy = value => JSON.parse(JSON.stringify(value));

// Window 1 shows the user's tab 1; tab 2 is another user tab. The browser activates a tab a page
// opens when foreground is set, like Firefox does for target=_blank links.
function harness({ onClick = () => {}, activity = {} } = {}) {
  let now = 1000, nextId = 0, nextTab = 20;
  const timers = new Map();
  const schedule = (fn, delay) => { const id = ++nextId; timers.set(id, { fn, at: now + delay }); return id; };
  const tabsEvents = { onCreated: event(), onUpdated: event(), onRemoved: event(), onActivated: event() };
  const windowEvents = { onCreated: event(), onRemoved: event(), onFocusChanged: event() };
  const tabs = {
    1: { id: 1, windowId: 1, url: 'https://user.example/one', status: 'complete', active: true },
    2: { id: 2, windowId: 1, url: 'https://user.example/two', status: 'complete', active: false },
  };
  const h = {
    tabs, activity, calls: [], sent: [], focusedWindow: 1,
    send: command => context.handleCommand({ includeSnapshot: false, ...command }).then(copy),
    activate(id) {
      const tab = tabs[id];
      const previous = Object.values(tabs).find(t => t.windowId === tab.windowId && t.active);
      if (previous) previous.active = false;
      tab.active = true;
      tabsEvents.onActivated.emit({ tabId: id, previousTabId: previous?.id, windowId: tab.windowId });
    },
    // A page opens a tab: Firefox creates it, then shows it when foreground.
    // activatedFirst: onActivated reaches the extension before onCreated.
    open(openerTabId, { foreground = true, windowId = 1, activatedFirst = false } = {}) {
      const id = nextTab++;
      tabs[id] = { id, windowId, url: 'about:blank', status: 'loading', active: false, openerTabId };
      if (foreground && activatedFirst) h.activate(id);
      tabsEvents.onCreated.emit({ ...tabs[id] });
      if (foreground && !activatedFirst) h.activate(id);
      return id;
    },
    focusWindow(windowId) { h.focusedWindow = windowId; windowEvents.onFocusChanged.emit(windowId); },
    load(id, url) {
      tabs[id] = { ...tabs[id], url, status: 'loading' };
      tabsEvents.onUpdated.emit(id, { status: 'loading', url }, { ...tabs[id] });
    },
    // The tabs shown in the user's window 1.
    shown: () => Object.values(tabs).filter(t => t.windowId === 1 && t.active).map(t => t.id),
    activations: () => h.calls.filter(([name, , props]) => name === 'tabs.update' && props?.active),
    async tick(ms) {
      const end = now + ms;
      await flush();
      while (true) {
        const next = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        timers.delete(next[0]);
        now = next[1].at;
        next[1].fn();
        await flush();
      }
      now = end;
      await flush();
    },
  };
  const missing = id => new Error(`Invalid tab ID: ${id}`);
  const created = props => {
    const id = nextTab++;
    tabs[id] = { id, windowId: props.windowId ?? 1, url: props.url || 'about:blank', status: 'complete',
      active: props.active !== false, openerTabId: props.openerTabId };
    tabsEvents.onCreated.emit({ ...tabs[id] });
    return { ...tabs[id] };
  };
  const browser = {
    tabs: {
      ...tabsEvents,
      query: async query => Object.values(tabs)
        .filter(t => (query.active == null || t.active === query.active)
          && (query.windowId == null || t.windowId === query.windowId)).map(t => ({ ...t })),
      get: async id => { if (!tabs[id]) throw missing(id); return { ...tabs[id] }; },
      create: async props => { h.calls.push(['tabs.create', copy(props)]); return created(props); },
      duplicate: async (id, props) => {
        h.calls.push(['tabs.duplicate', id, copy(props)]);
        return created({ ...props, url: tabs[id].url, windowId: tabs[id].windowId });
      },
      update: async (id, props) => {
        h.calls.push(['tabs.update', id, copy(props)]);
        if (!tabs[id]) throw missing(id);
        if (props.active) h.activate(id);
        if (props.url) tabs[id].url = props.url;
        return { ...tabs[id] };
      },
      remove: async id => {
        h.calls.push(['tabs.remove', id]);
        if (!tabs[id]) throw missing(id);
        delete tabs[id];
        tabsEvents.onRemoved.emit(id);
      },
      captureTab: async (id, options) => { h.calls.push(['tabs.captureTab', id, copy(options)]); return 'data:image/png;base64,AA=='; },
      captureVisibleTab: async () => { throw new Error('captureVisibleTab only sees the shown tab'); },
      sendMessage: async (id, msg) => {
        if (msg.action === 'ping') return { ok: true, version: Infinity };
        h.sent.push([msg.action, id]);
        if (msg.action === 'userActivity') return h.activity[id] ? { active: true, state: h.activity[id] } : { active: false };
        if (msg.action === 'click') return (await onClick(h, msg)) || { ok: true };
        if (msg.action === 'screenPoint') return { screenX: 10, screenY: 20, viewportX: 1, viewportY: 2, title: 't' };
        return { ok: true };
      },
      executeScript: async () => {},
    },
    windows: {
      ...windowEvents, WINDOW_ID_NONE: -1,
      create: async props => {
        h.calls.push(['windows.create', copy(props)]);
        const tab = created({ url: props.url?.[0], windowId: 9 });
        return { id: 9, tabs: [tab] };
      },
      update: async (id, props) => { h.calls.push(['windows.update', id, copy(props)]); if (props.focused) h.focusWindow(id); return { id }; },
      getLastFocused: async () => ({ id: h.focusedWindow }),
      get: async id => ({ id, tabs: Object.values(tabs).filter(t => t.windowId === id).map(t => ({ ...t })) }),
      remove: async id => {
        h.calls.push(['windows.remove', id]);
        Object.values(tabs).filter(t => t.windowId === id).forEach(t => { delete tabs[t.id]; tabsEvents.onRemoved.emit(t.id); });
      },
      getAll: async () => [{ id: 1 }],
    },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
  };
  const context = vm.createContext({
    browser, URL, console: { log() {}, error() {} },
    WebSocket: class { close() {} },
    Date: class extends Date { static now() { return now; } },
    setTimeout: (fn, delay) => schedule(fn, delay),
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => schedule(fn, delay),
    clearInterval: id => timers.delete(id),
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  return h;
}

// Sends a command while time passes, for commands that wait (opened tab URLs, navigation).
async function run(h, command, ms = 2000) {
  const pending = h.send(command);
  await h.tick(ms);
  return pending;
}

const owned = async h => Object.fromEntries((await h.send({ action: 'getTabs' })).tabs.map(t => [t.id, t.owned]));

async function testNeverTakeFocus() {
  const h = harness();
  // browser_tabs new: a background tab the agent owns; the user's tab stays shown.
  const opened = await h.send({ action: 'newTab', url: 'https://agent.example/a' });
  assert.deepEqual(h.calls[0], ['tabs.create', { url: 'https://agent.example/a', active: false }]);
  assert.deepEqual(h.shown(), [1]);
  assert.deepEqual(await owned(h), { 1: false, 2: false, [opened.tabId]: true });

  // Duplicates and new windows open in the background and are owned too.
  const dup = await h.send({ action: 'duplicateTab', tabId: 2 });
  assert.deepEqual(h.calls.at(-1), ['tabs.duplicate', 2, { active: false }]);
  const win = await h.send({ action: 'createWindow', url: 'https://agent.example/w' });
  assert.equal(h.calls.at(-1)[1].focused, false);
  await h.send({ action: 'detachTab', tabId: dup.tabId });
  assert.deepEqual(h.calls.at(-1), ['windows.create', { tabId: dup.tabId, focused: false }]);
  const tabs = await owned(h);
  assert.equal(tabs[dup.tabId], true);
  assert.equal(tabs[win.tabIds[0]], true);

  // Reading and acting on a background tab never activates it.
  await h.send({ action: 'click', tabId: opened.tabId, selector: '#go' });
  await run(h, { action: 'navigate', tabId: opened.tabId, url: 'https://agent.example/b' }, 16000);
  assert.deepEqual(h.shown(), [1]);
  assert.deepEqual(h.activations(), [], 'nothing activated a tab');
  assert.ok(!h.calls.some(([name]) => name === 'windows.update'), 'nothing focused a window');
}

async function testScreenshotsCaptureInPlace() {
  const h = harness();
  const viewport = await h.send({ action: 'screenshot', tabId: 2 });
  assert.equal(viewport.url, 'https://user.example/two');
  assert.deepEqual(h.calls.at(-1), ['tabs.captureTab', 2, { format: 'png', quality: 90 }]);
  await h.send({ action: 'screenshot' });
  assert.deepEqual(h.calls.at(-1).slice(0, 2), ['tabs.captureTab', 1], 'without tabId: the shown tab');
  // Element and full-page captures of a background tab no longer activate it.
  const element = h.send({ action: 'elementScreenshot', tabId: 2, selector: '#x' });
  const page = h.send({ action: 'fullPageScreenshot', tabId: 2 });
  await h.tick(5000);
  await Promise.all([element, page]);
  assert.deepEqual(h.activations(), []);
  assert.deepEqual(h.shown(), [1]);
}

async function testOpenedTabsStayInBackground() {
  // A target=_blank click opens a foreground tab; the user's tab is shown again at once.
  let openedId;
  const h = harness({ onClick: h => { openedId = h.open(1); } });
  const result = await run(h, { action: 'click', tabId: 1, selector: '#new' });
  assert.deepEqual(result.opened_tabs.map(t => t.tabId), [openedId]);
  assert.deepEqual(h.shown(), [1], 'the opener is shown again');
  assert.deepEqual(h.activations(), [['tabs.update', 1, { active: true }]]);
  assert.equal((await owned(h))[openedId], true, 'a tab the agent action opened is owned');

  // Either event order is undone.
  const early = harness({ onClick: h => { h.open(1, { activatedFirst: true }); } });
  await run(early, { action: 'click', tabId: 1, selector: '#new' });
  assert.deepEqual(early.shown(), [1]);
  assert.deepEqual(early.activations(), [['tabs.update', 1, { active: true }]]);

  // The user's own click later activates the opened tab; that is not undone.
  h.activate(openedId);
  await h.tick(10);
  assert.deepEqual(h.shown(), [openedId]);
  assert.equal(h.activations().length, 1);

  // A tab the user opens from a tab the agent did not touch is the user's and stays shown.
  const user = harness();
  const userTab = user.open(2);
  await user.tick(10);
  assert.deepEqual(user.shown(), [userTab]);
  assert.deepEqual(user.activations(), []);
  assert.equal((await owned(user))[userTab], false);

  // The popup-blocker fallback (#97) opens its tab in the background.
  const blocked = harness({ onClick: () => ({ ok: true, targetBlankLink: { url: 'https://x.example/', opener: false } }) });
  const fallbackTab = (await run(blocked, { action: 'click', tabId: 1, selector: '#new' })).opened_tabs[0];
  assert.equal(fallbackTab.via, 'link_fallback');
  assert.equal(blocked.calls.find(([name]) => name === 'tabs.create')[1].active, false);
  assert.deepEqual(blocked.shown(), [1]);
  assert.equal((await owned(blocked))[fallbackTab.tabId], true);

  // A popup window an agent action opens gives the focus back to the user's window.
  const popup = harness({ onClick: h => { h.open(1, { foreground: false, windowId: 5 }); } });
  popup.focusWindow(1);
  const popupClick = popup.send({ action: 'click', tabId: 1, selector: '#popup' });
  await popup.tick(0);
  popup.focusWindow(5);
  await popup.tick(1000);
  await popupClick;
  assert.deepEqual(popup.calls.filter(([name]) => name === 'windows.update'), [['windows.update', 1, { focused: true }]]);
  assert.equal(popup.focusedWindow, 1);
}

async function testOnlyOwnedTabsClose() {
  const h = harness();
  const refused = await h.send({ action: 'closeTab', tabId: 2, force: true });
  assert.deepEqual([refused.code, refused.retryable, refused.state],
    ['user_tab', false, { tabId: 2, url: 'https://user.example/two' }]);
  assert.match(refused.hint, /confirm_token/);
  assert.ok(h.tabs[2], 'the user tab stays open');
  assert.ok(!h.sent.some(([action]) => action === 'neuterUnload'), 'beforeunload stays armed on a refused close');

  const plain = (await h.send({ action: 'newTab', url: 'https://agent.example/a' })).tabId;
  assert.deepEqual(await h.send({ action: 'closeTab', tabId: plain }), { ok: true });
  assert.ok(!h.sent.some(([action]) => action === 'neuterUnload'), 'only force disarms beforeunload');
  const { tabId } = await h.send({ action: 'newTab', url: 'https://agent.example/a' });
  assert.deepEqual(await h.send({ action: 'closeTab', tabId, force: true }), { ok: true });
  assert.deepEqual(h.sent.at(-1), ['neuterUnload', tabId]);
  assert.equal(h.tabs[tabId], undefined);
  assert.equal((await h.send({ action: 'closeTab', tabId })).code, 'tab_closed');

  // Closing a window closes its tabs: one with user tabs is refused the same way.
  const win = await h.send({ action: 'createWindow', url: 'https://agent.example/w' });
  assert.deepEqual(await h.send({ action: 'closeWindow', windowId: win.windowId }), { ok: true, windowId: 9 });
  const userWindow = await h.send({ action: 'closeWindow', windowId: 1 });
  assert.deepEqual([userWindow.code, userWindow.retryable, userWindow.state], ['user_tab', false, { windowId: 1, tabIds: [1, 2] }]);
  assert.ok(!h.calls.some(([name, id]) => name === 'windows.remove' && id === 1));

  // The bridge sends userTabApproved only after the user approved this close.
  assert.deepEqual(await h.send({ action: 'closeTab', tabId: 2, userTabApproved: true }), { ok: true });
  assert.equal(h.tabs[2], undefined);
}

async function testUserActivityBlocksAgentWrites() {
  const h = harness({ activity: { 1: { reason: 'input', input: 'keydown', sinceMs: 200, windowMs: 3000 } } });
  for (const command of [
    { action: 'click', selector: '#go' },  // no tabId: the shown tab
    { action: 'type', tabId: 1, selector: '#q', text: 'x' },
    { action: 'navigate', tabId: 1, url: 'https://agent.example/' },
    { action: 'reloadTabBrowser', tabId: 1 },
  ]) {
    h.sent.length = 0;
    const result = await h.send(command);
    assert.deepEqual([result.code, result.retryable], ['user_active', true], command.action);
    assert.deepEqual(result.state, { tabId: 1, url: 'https://user.example/one', reason: 'input', input: 'keydown',
      sinceMs: 200, windowMs: 3000 });
    assert.deepEqual(h.sent, [['userActivity', 1]], `${command.action} never reached the page`);
  }
  assert.equal(h.tabs[1].url, 'https://user.example/one', 'the user page was not replaced');
  // Reads go on, and so do writes on other tabs.
  assert.equal((await h.send({ action: 'getSnapshot', tabId: 1 })).code, undefined);
  assert.equal((await h.send({ action: 'click', tabId: 2, selector: '#go' })).ok, true);
  // A viewport capture is a read; element and full-page captures scroll the user's page.
  assert.ok((await h.send({ action: 'screenshot', tabId: 1 })).dataUrl);
  assert.equal((await h.send({ action: 'fullPageScreenshot', tabId: 1 })).code, 'user_active');
  // Switching the view away from the tab the user is using waits too; showing it again does not.
  assert.equal((await h.send({ action: 'switchTab', tabId: 2 })).code, 'user_active');
  assert.deepEqual(h.shown(), [1]);
  assert.deepEqual(await h.send({ action: 'switchTab', tabId: 1 }), { ok: true });
  delete h.activity[1];
  assert.deepEqual(await h.send({ action: 'switchTab', tabId: 2 }), { ok: true }, 'an explicit switch is allowed');
  assert.deepEqual(h.shown(), [2]);
}

async function testUserNavigationBlocksAgentWrites() {
  const h = harness();
  // The user navigates the shown tab (address bar, back button): no agent command started it.
  h.load(1, 'https://user.example/next');
  const blocked = await h.send({ action: 'click', tabId: 1, selector: '#go' });
  assert.deepEqual([blocked.code, blocked.state.reason, blocked.state.sinceMs], ['user_active', 'navigating', 0]);
  assert.deepEqual(h.sent, [], 'nothing reached the page');
  // The same load in a background tab is not the user's.
  h.load(2, 'https://user.example/redirect');
  assert.equal((await h.send({ action: 'click', tabId: 2, selector: '#go' })).ok, true);
  // After the window the load no longer counts.
  await h.tick(10000);
  assert.equal((await h.send({ action: 'click', tabId: 1, selector: '#go' })).ok, true);

  // A load the agent starts on the shown tab is not the user's.
  const agent = harness({ onClick: h => { h.load(1, 'https://user.example/clicked'); } });
  await run(agent, { action: 'click', tabId: 1, selector: '#link' }, 500);
  assert.equal((await agent.send({ action: 'click', tabId: 1, selector: '#go' })).ok, true);
}

async function testTrustedClickShowsTheTabOnlyForTheClick() {
  const h = harness();
  const point = await run(h, { action: 'screenPoint', tabId: 2, selector: '#go', click: true }, 200);
  assert.deepEqual([point.tabId, point.previousTabId, point.screenX], [2, 1, 10]);
  assert.deepEqual(h.shown(), [2], 'shown for the OS click');
  assert.deepEqual(await h.send({ action: 'restoreActiveTab', tabId: 1, from: 2 }), { ok: true, restored: true });
  assert.deepEqual(h.shown(), [1]);
  // The user moved on meanwhile: their choice stands.
  assert.deepEqual(await h.send({ action: 'restoreActiveTab', tabId: 2, from: 2 }), { ok: true, restored: false });
  assert.deepEqual(h.shown(), [1]);
  // The shown tab is never left while the user is using it.
  h.activity[1] = { reason: 'typing', field: 'textarea' };
  assert.equal((await h.send({ action: 'screenPoint', tabId: 2, selector: '#go', click: true })).code, 'user_active');
  assert.deepEqual(h.shown(), [1]);
  // A plain screenPoint (no click) is a read and changes nothing.
  delete h.activity[1];
  assert.equal((await h.send({ action: 'screenPoint', tabId: 2, selector: '#go' })).previousTabId, undefined);
  assert.deepEqual(h.shown(), [1]);
}

(async () => {
  await testNeverTakeFocus();
  await testScreenshotsCaptureInPlace();
  await testOpenedTabsStayInBackground();
  await testOnlyOwnedTabsClose();
  await testUserActivityBlocksAgentWrites();
  await testUserNavigationBlocksAgentWrites();
  await testTrustedClickShowsTheTabOnlyForTheClick();
  console.log('Shared browser checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
