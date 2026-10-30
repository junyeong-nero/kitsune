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
// focusOnCreate: windows.create focuses the new window despite focused: false, 'before' it resolves
// or 'after'. sync: Zen's window sync, see windows.create and tabs.remove below.
// holdViewedTab: commands are held on the tab the user views (#326, testViewedTabIsHeld); the other
// checks act on the user's shown tab 1 for other rules, so they send holdViewedTab: false.
function harness({ onClick = () => {}, activity = {}, focusOnCreate = null, sync = false, holdViewedTab = false } = {}) {
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
    send: command => context.handleCommand({ includeSnapshot: false, ...(holdViewedTab ? {} : { holdViewedTab: false }),
      ...command }).then(copy),
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
    // A tab the browser (or the user) adds to window 1 in the background.
    add(props) {
      const id = nextTab++;
      tabs[id] = { id, windowId: 1, url: 'about:blank', status: 'complete', active: false, ...props };
      tabsEvents.onCreated.emit({ ...tabs[id] });
      return id;
    },
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
  const syncCopy = (original, windowId) => {
    original.syncId ??= original.id;
    return h.add({ windowId, title: original.title, discarded: true, syncId: original.syncId });
  };
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
      getZoom: async id => { if (!tabs[id]) throw missing(id); return 1; },
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
        const { syncId } = tabs[id];
        delete tabs[id];
        tabsEvents.onRemoved.emit(id);
        // Zen closes a tab's copies in the other windows with it, the original included.
        if (sync && syncId != null) {
          Object.values(tabs).filter(t => t.syncId === syncId).forEach(t => { delete tabs[t.id]; tabsEvents.onRemoved.emit(t.id); });
        }
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
        const windowTabs = Object.values(tabs).filter(t => t.windowId === 1);
        let tab;
        if (props.tabId != null) {
          tabs[props.tabId].windowId = 9;
          tab = { ...tabs[props.tabId] };
        } else {
          tab = created({ url: props.url?.[0], windowId: 9 });
        }
        if (focusOnCreate === 'before') h.focusWindow(9);
        if (focusOnCreate === 'after') schedule(() => h.focusWindow(9), 20);
        // Zen's window sync: the new window gets a lazy copy of each tab of window 1, and window 1
        // gets one of the new window's tab once the window is shown (h.agentCopy).
        const copies = sync ? windowTabs.filter(t => t.id !== props.tabId).map(t => ({ ...tabs[syncCopy(t, 9)] })) : [];
        if (sync) schedule(() => { h.agentCopy = syncCopy(tabs[tab.id], 1); }, 50);
        return { id: 9, tabs: [tab, ...copies] };
      },
      update: async (id, props) => { h.calls.push(['windows.update', id, copy(props)]); if (props.focused) h.focusWindow(id); return { id }; },
      // focusedWindow -1: the user is in another application.
      getLastFocused: async () => ({ id: h.focusedWindow === -1 ? 1 : h.focusedWindow, focused: h.focusedWindow !== -1 }),
      get: async id => ({ id, tabs: Object.values(tabs).filter(t => t.windowId === id).map(t => ({ ...t })) }),
      remove: async id => {
        h.calls.push(['windows.remove', id]);
        const closing = Object.values(tabs).filter(t => t.windowId === id);
        // Zen moves the closing window's page into its copies elsewhere; closing a window closes no copies.
        for (const t of closing) {
          for (const other of Object.values(tabs).filter(o => o.windowId !== id && o.syncId === t.syncId && o.discarded)) {
            Object.assign(other, { url: t.url, discarded: false });
            tabsEvents.onUpdated.emit(other.id, { discarded: false }, { ...other });
            tabsEvents.onUpdated.emit(other.id, { url: t.url }, { ...other });
          }
        }
        closing.forEach(t => { delete tabs[t.id]; tabsEvents.onRemoved.emit(t.id); });
        windowEvents.onRemoved.emit(id);
      },
      getAll: async () => [{ id: 1 }],
    },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
    permissions: { contains: async () => true },
  };
  const context = vm.createContext({
    browser, URL, console: { log() {}, error() {} },
    WebSocket: class { close() {} send() {} },
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

async function testClickAndWaitNavigationForwardsFirst() {
  const clicks = [];
  const h = harness({ onClick: (_, msg) => { clicks.push(copy(msg)); } });
  const result = await run(h, { action: 'clickAndWaitNavigation', tabId: 1,
    selector: 'button', first: true, timeout: 10 }, 20);
  assert.equal(result.clicked, true);
  assert.equal(clicks.length, 1);
  assert.equal(clicks[0].selector, 'button');
  assert.equal(clicks[0].first, true);
}

async function testNeverTakeFocus() {
  const h = harness();
  // browser_tabs new: a background tab the agent owns; the user's tab stays shown.
  const opened = await h.send({ action: 'newTab', url: 'https://agent.example/a' });
  assert.deepEqual(h.calls[0], ['tabs.create', { url: 'https://agent.example/a', active: false }]);
  assert.deepEqual(h.shown(), [1]);
  assert.deepEqual(await owned(h), { 1: false, 2: false, [opened.tabId]: true });

  // Duplicates and new windows open in the background and are owned too. A new window gives the
  // focus back to the user's window in case the browser focused it anyway.
  const dup = await h.send({ action: 'duplicateTab', tabId: 2 });
  assert.deepEqual(h.calls.at(-1), ['tabs.duplicate', 2, { active: false }]);
  const win = await h.send({ action: 'createWindow', url: 'https://agent.example/w' });
  assert.deepEqual(h.calls.at(-2), ['windows.create', { url: ['https://agent.example/w'], incognito: false, focused: false }]);
  assert.deepEqual(h.calls.at(-1), ['windows.update', 1, { focused: true }]);
  await h.send({ action: 'detachTab', tabId: dup.tabId });
  assert.deepEqual(h.calls.at(-2), ['windows.create', { tabId: dup.tabId, focused: false }]);
  assert.deepEqual(h.calls.at(-1), ['windows.update', 1, { focused: true }]);
  const tabs = await owned(h);
  assert.equal(tabs[dup.tabId], true);
  assert.equal(tabs[win.tabIds[0]], true);

  // Reading and acting on a background tab never activates it.
  await h.send({ action: 'click', tabId: opened.tabId, selector: '#go' });
  await run(h, { action: 'navigate', tabId: opened.tabId, url: 'https://agent.example/b' }, 16000);
  assert.deepEqual(h.shown(), [1]);
  assert.deepEqual(h.activations(), [], 'nothing activated a tab');
  assert.ok(h.calls.filter(([name]) => name === 'windows.update').every(([, id]) => id === 1),
    'nothing focused a window but the user\'s');
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

// #326: viewing counts as using. Writes and navigations on the tab shown in the browser window the
// user last focused are held without recent input, also while another app has the focus.
async function testViewedTabIsHeld() {
  const h = harness({ holdViewedTab: true });
  for (const command of [
    { action: 'click', selector: '#go' },  // no tabId: the shown tab
    { action: 'type', tabId: 1, selector: '#q', text: 'x' },
    { action: 'keypress', tabId: 1, key: 'Enter' },
    { action: 'formFill', tabId: 1, fields: { q: 'x' } },
    { action: 'scroll', tabId: 1, direction: 'down' },
    { action: 'hover', tabId: 1, selector: '#go' },
    { action: 'executeJS', tabId: 1, code: '1' },
    { action: 'navigate', tabId: 1, url: 'https://agent.example/' },
    { action: 'goBack', tabId: 1 },
    { action: 'reloadTabBrowser', tabId: 1 },
    { action: 'screenPoint', tabId: 1, selector: '#go', click: true },
  ]) {
    h.sent.length = 0;
    const result = await h.send(command);
    assert.deepEqual([result.code, result.retryable], ['user_viewing', false], command.action);
    assert.deepEqual(result.state, { tabId: 1, url: 'https://user.example/one', windowId: 1, reason: 'viewing',
      browserFocused: true });
    assert.match(result.hint, /confirm_token/);
    assert.deepEqual(h.sent, [['userActivity', 1]], `${command.action} never reached the page`);
  }
  assert.equal(h.tabs[1].url, 'https://user.example/one', 'the user page was not replaced');
  assert.deepEqual(h.activations(), []);

  // The user switched to another app (the agent's terminal): their window still shows the tab.
  h.focusWindow(-1);
  const away = await h.send({ action: 'click', tabId: 1, selector: '#go' });
  assert.deepEqual([away.code, away.state.browserFocused], ['user_viewing', false]);
  h.focusWindow(1);

  // Reads, captures and tabs the user does not see go on.
  assert.equal((await h.send({ action: 'getSnapshot', tabId: 1 })).code, undefined);
  assert.ok((await h.send({ action: 'screenshot', tabId: 1 })).dataUrl);
  assert.equal((await h.send({ action: 'click', tabId: 2, selector: '#go' })).ok, true, 'a background user tab');
  const elsewhere = h.add({ windowId: 3, active: true, url: 'https://user.example/three' });
  assert.equal((await h.send({ action: 'click', tabId: elsewhere, selector: '#go' })).ok, true,
    'the shown tab of a window the user did not focus last');
  // Closing a user tab asks for approval as before (user_tab), whether or not it is shown.
  assert.equal((await h.send({ action: 'closeTab', tabId: 1 })).code, 'user_tab');

  // A tab the agent opened is not held, even once the user shows it.
  const agentTab = (await h.send({ action: 'newTab', url: 'https://agent.example/a' })).tabId;
  h.activate(agentTab);
  assert.equal((await h.send({ action: 'click', tabId: agentTab, selector: '#go' })).ok, true);
  h.activate(1);

  // Only the bridge releases the hold: the call was approved (its token, or the user_tab approval
  // of the same navigation), or the client set KITSUNE_HOLD_VIEWED_TAB=0.
  for (const release of [{ viewedTabApproved: true }, { holdViewedTab: false }]) {
    assert.equal((await h.send({ action: 'click', tabId: 1, selector: '#go', ...release })).ok, true,
      JSON.stringify(release));
  }
  // userTabApproved only approves a navigation: the bridge strips a caller's own only on those routes.
  assert.equal((await h.send({ action: 'click', tabId: 1, selector: '#go', userTabApproved: true })).code,
    'user_viewing');
  await run(h, { action: 'navigate', tabId: 1, url: 'https://agent.example/approved', userTabApproved: true }, 16000);
  assert.equal(h.tabs[1].url, 'https://agent.example/approved', 'an approved navigation of the viewed tab runs');

  // An explicit switch away is not held; the trusted click's brief showing of a background tab is
  // not either: only the user's input on the tab it hides stops it.
  const point = await run(h, { action: 'screenPoint', tabId: agentTab, selector: '#go', click: true }, 200);
  assert.deepEqual([point.tabId, point.previousTabId], [agentTab, 1]);
  assert.deepEqual(await h.send({ action: 'restoreActiveTab', tabId: 1, from: agentTab }), { ok: true, restored: true });
  assert.deepEqual(await h.send({ action: 'switchTab', tabId: 2 }), { ok: true });
  assert.deepEqual(h.shown(), [2]);
  assert.equal((await h.send({ action: 'click', tabId: 1, selector: '#go' })).ok, true, 'tab 1 is not shown now');
  assert.equal((await h.send({ action: 'click', tabId: 2, selector: '#go' })).code, 'user_viewing');
}

// Zen focuses a new window despite focused: false; the user's window gets the focus back.
async function testAgentWindowsGiveFocusBack() {
  const focusCalls = h => h.calls.filter(([name]) => name === 'windows.update');
  for (const focusOnCreate of ['before', 'after']) {
    for (const command of [{ action: 'createWindow', url: 'https://agent.example/w' }, { action: 'detachTab', tabId: 2 }]) {
      const h = harness({ focusOnCreate });
      assert.equal((await h.send(command)).windowId, 9);
      await h.tick(100);
      assert.equal(h.focusedWindow, 1, `${command.action}, focused ${focusOnCreate} windows.create resolved`);
      assert.ok(focusCalls(h).length && focusCalls(h).every(([, id, props]) => id === 1 && props.focused));
    }
  }

  // The user was in another application: no browser window is focused, so none is.
  const away = harness();
  away.focusWindow(-1);
  await away.send({ action: 'createWindow', url: 'https://agent.example/w' });
  assert.deepEqual(focusCalls(away), []);

  // After the first moments, focusing the new window is the user's (or an explicit focusWindow).
  const late = harness();
  await late.send({ action: 'createWindow', url: 'https://agent.example/w' });
  await late.tick(2000);
  late.focusWindow(9);
  await late.tick(10);
  assert.equal(late.focusedWindow, 9);
  const asked = harness();
  await asked.send({ action: 'createWindow', url: 'https://agent.example/w' });
  assert.deepEqual(await asked.send({ action: 'focusWindow', windowId: 9 }), { ok: true, windowId: 9 });
  await asked.tick(10);
  assert.equal(asked.focusedWindow, 9);
}

// Zen's window sync copies the user's tabs into an agent window and the agent window's tab into
// the user's window; closing a copy closes its original.
async function testZenSyncCopies() {
  const h = harness({ sync: true });
  const win = await h.send({ action: 'createWindow', url: 'https://agent.example/w' });
  await h.tick(100);
  const agentTab = win.tabIds[0];
  const copies = Object.values(h.tabs).filter(t => t.windowId === 9 && t.id !== agentTab).map(t => t.id);
  assert.equal(copies.length, 2, 'copies of the user tabs 1 and 2');
  assert.deepEqual(win.tabIds, [agentTab], 'only the tab the agent asked for is listed');
  assert.ok(h.agentCopy, 'a copy of the agent window\'s tab in the user\'s window');
  const tabs = await owned(h);
  assert.deepEqual([tabs[agentTab], tabs[h.agentCopy], tabs[1], tabs[2]], [true, true, false, false]);
  assert.deepEqual(copies.map(id => tabs[id]), [false, false], 'copies of user tabs are never owned');

  // Closing a copy of a user tab would close the user's tab, so it is refused, even after the
  // copy stops being discarded (ownership was decided when the window opened).
  const copyOfOne = copies.find(id => h.tabs[id].syncId === 1);
  assert.equal((await h.send({ action: 'closeTab', tabId: copyOfOne })).code, 'user_tab');
  Object.assign(h.tabs[copyOfOne], { discarded: false });
  h.load(copyOfOne, 'https://user.example/one');
  assert.equal((await owned(h))[copyOfOne], false);
  assert.equal((await h.send({ action: 'closeTab', tabId: copyOfOne })).code, 'user_tab');
  assert.ok(h.tabs[1] && h.tabs[copyOfOne], 'the user tab and its copy stay open');

  // closeWindow closes the agent window with its copies, then the agent's copy in the user's window
  // (which by then shows the agent's page); the user's tabs stay as they were.
  assert.deepEqual(await h.send({ action: 'closeWindow', windowId: 9 }), { ok: true, windowId: 9 });
  assert.deepEqual(Object.keys(h.tabs).map(Number), [1, 2]);
  assert.deepEqual([h.tabs[1].url, h.tabs[2].url], ['https://user.example/one', 'https://user.example/two']);
  assert.ok(h.calls.some(([name, id]) => name === 'tabs.remove' && id === h.agentCopy));
  assert.ok(!h.calls.some(([name, id]) => name === 'tabs.remove' && copies.includes(id)));

  // A detached window's copies are not recorded, so closing it still needs the user's approval.
  const d = harness({ sync: true });
  const detached = (await d.send({ action: 'newTab', url: 'https://agent.example/d' })).tabId;
  await d.send({ action: 'detachTab', tabId: detached });
  await d.tick(100);
  assert.equal((await owned(d))[d.agentCopy], true);
  assert.equal((await d.send({ action: 'closeWindow', windowId: 9 })).code, 'user_tab');
}

// Only a lazy background copy, without an opener, at about:blank or the agent window's URL, opening
// within 1.5 s of the agent window, is adopted; the user taking it over gives it back.
async function testNeverAdoptUserTabs() {
  const h = harness();
  await h.send({ action: 'createWindow', url: 'https://agent.example/w' });
  // Each differs from a copy in one condition only.
  const user = {
    shown: h.add({ discarded: true, active: true }),
    opener: h.add({ discarded: true, openerTabId: 1 }),
    loads: h.add({}),                                                 // e.g. a bookmark opened in the background
    elsewhere: h.add({ discarded: true, url: 'https://user.example/x' }),
  };
  await h.tick(1600);
  user.late = h.add({ discarded: true });
  const copy = h.add({ discarded: true });
  let tabs = await owned(h);
  for (const [name, id] of Object.entries(user)) assert.equal(tabs[id], false, name);
  assert.equal(tabs[copy], false, 'after 1.5 s even a copy-like tab stays the user\'s');

  const adopt = async () => {
    const t = harness();
    await t.send({ action: 'createWindow', url: 'https://agent.example/w' });
    const id = t.add({ discarded: true });
    assert.equal((await owned(t))[id], true);
    return [t, id];
  };
  const [shown, shownCopy] = await adopt();
  shown.activate(shownCopy);
  assert.equal((await owned(shown))[shownCopy], false, 'the user showed it');
  const [loaded, loadedCopy] = await adopt();
  loaded.load(loadedCopy, 'https://user.example/typed');
  assert.equal((await owned(loaded))[loadedCopy], false, 'it loaded a page the agent did not');
  for (const t of [shown, loaded]) {
    await t.send({ action: 'closeWindow', windowId: 9 });
    assert.ok(t.tabs[shownCopy] || t.tabs[loadedCopy], 'a returned copy stays open');
  }
  const [active, activeCopy] = await adopt();
  active.activity[activeCopy] = { reason: 'input', input: 'keydown' };
  Object.assign(active.tabs[activeCopy], { url: 'https://agent.example/w', discarded: false });
  await active.send({ action: 'closeWindow', windowId: 9 });
  assert.ok(active.tabs[activeCopy], 'the user is using it, so it stays open');
  assert.equal((await owned(active))[activeCopy], false);
}

(async () => {
  await testClickAndWaitNavigationForwardsFirst();
  await testNeverTakeFocus();
  await testScreenshotsCaptureInPlace();
  await testOpenedTabsStayInBackground();
  await testOnlyOwnedTabsClose();
  await testUserActivityBlocksAgentWrites();
  await testUserNavigationBlocksAgentWrites();
  await testTrustedClickShowsTheTabOnlyForTheClick();
  await testViewedTabIsHeld();
  await testAgentWindowsGiveFocusBack();
  await testZenSyncCopies();
  await testNeverAdoptUserTabs();
  console.log('Shared browser checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
