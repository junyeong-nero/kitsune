// Run with: node tests/agent_tab_persistence.cjs (no browser or npm dependencies).
// Issue #376: agent tab ownership is stored as a session tab value and restored when
// background.js loads again (an extension reload, or a browser restart that restores the tabs).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const KEY = 'kitsune.owned';
const copy = value => JSON.parse(JSON.stringify(value));
const flush = () => new Promise(resolve => setImmediate(resolve));

function event() {
  const listeners = new Set();
  return {
    addListener: fn => listeners.add(fn),
    removeListener: fn => listeners.delete(fn),
    emit: (...args) => [...listeners].forEach(fn => fn(...args)),
  };
}

// The browser outlives each load of background.js: tabs and stored values (sessions) stay, the
// background page's memory does not. sessions: false leaves browser.sessions out (older Gecko).
function browserState({ sessions = true } = {}) {
  return {
    sessions,
    now: 100000,
    // windowId 1 is the user's focused window; its active tab is the one the user views.
    tabs: new Map(),
    values: new Map(), // `${tabId}|${key}` -> JSON, as Firefox stores tab values
    valueRemovals: [], // tab ids, in the order removeTabValue was called
    // gate() results that hold, until released: getTabValue (gate), tabs.query, which the restore
    // calls first (queryGate), and the removal in removeTabValue (removeGate).
    gate: null,
    queryGate: null,
    removeGate: null,
    beforeDuplicateResolves: null,
    nextId: 100,
    removed: [], // closed tab ids
    navigated: [], // navigated tab ids
  };
}

function load(state) {
  const tabs = state.tabs;
  const onCreated = event(), onUpdated = event(), onActivated = event();
  const browser = {
    tabs: {
      onCreated, onUpdated, onRemoved: event(), onActivated,
      get: async id => {
        if (!tabs.has(id)) throw new Error(`Invalid tab ID: ${id}`);
        return { ...tabs.get(id) };
      },
      query: async () => {
        await state.queryGate?.promise;
        return [...tabs.values()].map(tab => ({ ...tab }));
      },
      create: async ({ url }) => {
        const tab = { id: state.nextId++, windowId: 1, active: false, url: url || 'about:blank', title: '' };
        tabs.set(tab.id, tab);
        onCreated.emit({ ...tab });
        return { ...tab };
      },
      remove: async id => { tabs.delete(id); state.removed.push(id); },
      // As Firefox does: onCreated reports the copy, then it gets the original's values and starts
      // loading, all before this resolves; state.beforeDuplicateResolves runs after that.
      duplicate: async id => {
        const tab = await duplicateTab(state, { onCreated, onUpdated }, id);
        await state.beforeDuplicateResolves?.();
        return { ...tab };
      },
      sendMessage: async () => null,
    },
    windows: {
      getLastFocused: async () => ({ id: 1, focused: true }),
      getAll: async () => [{ id: 1 }],
      create: async ({ url }) => {
        const tab = { id: state.nextId++, windowId: 3, active: true, url: url?.[0] || 'about:blank', title: '' };
        tabs.set(tab.id, tab);
        return { id: 3, tabs: [{ ...tab }] };
      },
      update: async () => ({}),
    },
    storage: { local: { get: async () => ({}), set: async () => {} } },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
  };
  if (state.sessions) {
    const at = (tabId, key) => {
      if (!tabs.has(tabId)) throw new Error(`Invalid tab ID: ${tabId}`);
      return `${tabId}|${key}`;
    };
    browser.sessions = {
      setTabValue: async (tabId, key, value) => { state.values.set(at(tabId, key), JSON.stringify(value)); },
      removeTabValue: async (tabId, key) => {
        const slot = at(tabId, key);
        state.valueRemovals.push(tabId);
        await state.removeGate?.promise;
        state.values.delete(slot);
      },
      getTabValue: async (tabId, key) => {
        await state.gate?.promise;
        const value = state.values.get(at(tabId, key));
        return value === undefined ? undefined : JSON.parse(value);
      },
    };
  }
  // Commands arrive as the bridge sends them, through the socket connect() opens on load.
  let socket = null;
  const replies = new Map(); // command id -> resolve
  class WebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    constructor() { socket = this; this.readyState = WebSocket.OPEN; }
    send(data) {
      const { id, result } = JSON.parse(data);
      replies.get(id)?.(result);
    }
    close() {}
  }
  const context = vm.createContext({
    Date: class extends Date { static now() { return state.now; } },
    browser, URL, crypto: require('node:crypto').webcrypto, console: { log() {}, error() {} },
    WebSocket,
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  context.userActiveError = async () => null;
  context.navigateTab = async (url, tabId) => {
    state.navigated.push(tabId);
    return { ok: true, url };
  };
  // Tracks whether a command has resolved, so a hang fails an assertion instead of exiting silently.
  let nextCommand = 0;
  const start = command => {
    const id = `c${++nextCommand}`;
    const pending = { settled: false };
    pending.result = new Promise(resolve => replies.set(id, resolve))
      .then(result => { pending.settled = true; return copy(result); });
    socket.onmessage({ data: JSON.stringify({ includeSnapshot: false, ...command, id }) });
    return pending;
  };
  const run = command => start(command).result;
  const owned = async ownerId => Object.fromEntries((await run({ action: 'getTabs', ownerId })).tabs.map(t => [t.id, t.owned]));
  return { run, start, owned, onCreated, onUpdated, onActivated,
    handoffs: () => context.handoffPopup(), decide: id => context.handoffDecision(id, 'approve'),
    remark: (tabId, ownerId) => context.markAgentTab(tabId, ownerId),
    input: (tabId, at = state.now) => browser.runtime.onMessage.emit(
      { action: 'trustedTabInput', at }, { tab: { id: tabId } }),
  };
}

// A duplicate of tab id, as Firefox makes one: onCreated reports it before it has the values of
// its original, which it gets once the original's state is flushed, and then it starts loading.
async function duplicateTab(state, { onCreated, onUpdated }, id) {
  const tab = { ...state.tabs.get(id), id: state.nextId++, active: false };
  state.tabs.set(tab.id, tab);
  onCreated.emit({ ...tab });
  await settle();
  for (const [slot, value] of [...state.values]) {
    if (slot.startsWith(`${id}|`)) state.values.set(`${tab.id}|${slot.slice(slot.indexOf('|') + 1)}`, value);
  }
  onUpdated.emit(tab.id, { status: 'loading' }, { ...tab });
  return tab;
}
const settle = async () => { for (let i = 0; i < 10; i++) await flush(); };
const gate = () => {
  let release;
  return { promise: new Promise(resolve => { release = resolve; }), release: () => release() };
};

const stored = (state, tabId) => {
  const value = state.values.get(`${tabId}|${KEY}`);
  return value === undefined ? undefined : JSON.parse(value);
};
function addTab(state, tab) {
  state.tabs.set(tab.id, { windowId: 1, active: false, url: `https://example.com/${tab.id}`, title: `Tab ${tab.id}`, ...tab });
}

async function markedTabsStoreTheirValue() {
  const state = browserState();
  const h = load(state);
  const { tabId } = await h.run({ action: 'newTab', url: 'https://example.com/new' });
  await flush();
  assert.deepEqual(stored(state, tabId), { v: 1 });
  assert.equal((await h.owned())[tabId], true);
}

async function aFreshLoadRestoresOwnershipBeforeTheFirstCommand() {
  const state = browserState();
  // Tab 5: the agent's, shown in the focused window. Tab 6: the user's. Tabs 7-10 carry values
  // that are not Kitsune's ownership value.
  addTab(state, { id: 5, active: true });
  addTab(state, { id: 6 });
  for (const [id, value] of [[7, { v: 2 }], [8, true], [9, 'owned'], [10, { v: '1' }]]) {
    addTab(state, { id });
    state.values.set(`${id}|${KEY}`, JSON.stringify(value));
  }
  state.values.set(`5|${KEY}`, JSON.stringify({ v: 1 }));

  const restore = state.gate = gate();
  const h = load(state); // the reload: this copy of background.js has never seen tab 5
  const navigate = h.start({ action: 'navigate', url: 'https://example.com/next', tabId: 5, protectUserTabs: true });
  const close = h.start({ action: 'closeTab', tabId: 5 });
  const list = h.start({ action: 'getTabs' });
  await flush();
  assert.equal(navigate.settled || close.settled || list.settled, false, 'commands wait for the restore');
  restore.release();

  // Owned: neither held for being viewed (#364) nor protected (#326), and it closes without approval.
  assert.equal((await navigate.result).ok, true, 'the restored agent tab is not held or protected');
  assert.deepEqual(state.navigated, [5]);
  const owned = Object.fromEntries((await list.result).tabs.map(t => [t.id, t.owned]));
  assert.deepEqual({ 5: owned[5], 6: owned[6], 7: owned[7], 8: owned[8], 9: owned[9], 10: owned[10] },
    { 5: true, 6: false, 7: false, 8: false, 9: false, 10: false });
  assert.deepEqual(await close.result, { ok: true });
  assert.deepEqual(state.removed, [5]);
}

async function aTabWithoutAValueStaysAUserTab() {
  const state = browserState();
  addTab(state, { id: 6, active: true });
  const h = load(state);
  const viewed = await h.run({ action: 'navigate', url: 'https://example.com/x', tabId: 6 });
  assert.equal(viewed.code, 'user_tab_read_only', JSON.stringify(viewed));
  const protectedTab = await h.run({ action: 'navigate', url: 'https://example.com/x', tabId: 6, protectUserTabs: true });
  assert.equal(protectedTab.code, 'user_tab_read_only', JSON.stringify(protectedTab));
  const close = await h.run({ action: 'closeTab', tabId: 6 });
  assert.equal(close.code, 'user_tab_read_only', JSON.stringify(close));
  assert.deepEqual(state.navigated, []);
  assert.deepEqual(state.removed, []);
  assert.equal((await h.owned())[6], false);
}

// A synced copy (see Agent windows) that goes back to the user loses its stored value too.
async function aTabGivenBackToTheUserLosesItsValue() {
  const state = browserState();
  addTab(state, { id: 1, active: true });
  const h = load(state);
  const created = await h.run({ action: 'createWindow', url: 'https://example.com/agent' });
  assert.equal(created.ok, true, JSON.stringify(created));
  const synced = { id: 50, windowId: 1, active: false, discarded: true, url: 'about:blank', title: '' };
  state.tabs.set(synced.id, synced);
  h.onCreated.emit({ ...synced });
  await flush();
  assert.deepEqual(stored(state, 50), { v: 1 });
  assert.equal((await h.owned())[50], true);
  h.onActivated.emit({ tabId: 50, windowId: 1 }); // the user shows it
  await flush();
  assert.equal(stored(state, 50), undefined);
  assert.equal((await h.owned())[50], false);
  assert.equal((await load(state).owned())[50], false, 'and stays the user\'s after a reload');
}

// The agent's own duplicate gets the copied value before markAgentTab runs: it stays the agent's.
async function theAgentsDuplicateStaysOwned() {
  for (const ownerId of [null, 'client-a']) for (const holdRemove of [false, true]) {
    const state = browserState();
    const h = load(state);
    const { tabId: original } = await h.run({ action: 'newTab', ownerId, url: 'https://example.com/original' });
    await settle();
    // The cleanup reads the copied value and removes it before duplicate() resolves.
    // With holdRemove, that removal lands only after markAgentTab has written the value again.
    const removal = holdRemove ? (state.removeGate = gate()) : null;
    state.beforeDuplicateResolves = settle;
    const dup = await h.run({ action: 'duplicateTab', tabId: original, ownerId });
    assert.equal(dup.ok, true, JSON.stringify(dup));
    removal?.release();
    state.removeGate = null;
    await settle();
    assert.deepEqual(state.valueRemovals, [dup.tabId], 'the race happened');
    assert.deepEqual(stored(state, dup.tabId), ownerId ? { v: 2, owner: ownerId } : { v: 1 }, `holdRemove=${holdRemove}`);
    assert.equal((await h.owned(ownerId))[dup.tabId], true);
    assert.equal((await load(state).owned(ownerId))[dup.tabId], true, 'and after a reload');
  }
}

// Tabs the agent opens, and tabs its page actions open, keep their value.
async function newAndOpenedTabsStayOwned() {
  const state = browserState();
  const h = load(state);
  const { tabId } = await h.run({ action: 'newTab', url: 'https://example.com/new' });
  // A tab the agent's page opened within the grace after its action (see agentTouches).
  const opened = { id: 60, windowId: 1, active: false, openerTabId: tabId, url: 'https://example.com/popup', title: '' };
  state.tabs.set(opened.id, opened);
  h.onCreated.emit({ ...opened });
  await settle();
  assert.deepEqual([stored(state, tabId), stored(state, 60)], [{ v: 1 }, { v: 1 }]);
  assert.deepEqual(state.valueRemovals, [], 'not even for a moment');
  const after = await load(state).owned();
  assert.deepEqual([after[tabId], after[60]], [true, true]);
}

// A tab the user duplicates from an agent tab, or a closed agent tab the user reopens, is the user's.
async function userCopiesOfAgentTabsAreTheUsers() {
  const state = browserState();
  const h = load(state);
  const { tabId: agent } = await h.run({ action: 'newTab', url: 'https://example.com/agent' });
  const { tabId: closed } = await h.run({ action: 'newTab', url: 'https://example.com/closed' });
  await settle();
  const duplicate = await duplicateTab(state, h, agent);
  assert.deepEqual(await h.run({ action: 'closeTab', tabId: closed }), { ok: true });
  // Firefox keeps a closed tab's values and gives them back when the tab is reopened, by the
  // time onCreated reports it.
  const reopened = { id: state.nextId++, windowId: 1, active: true, url: 'https://example.com/closed', title: '' };
  state.tabs.set(reopened.id, reopened);
  state.values.set(`${reopened.id}|${KEY}`, JSON.stringify({ v: 1 }));
  h.onCreated.emit({ ...reopened });
  await settle();
  for (const id of [duplicate.id, reopened.id]) assert.equal(stored(state, id), undefined, `tab ${id}`);
  for (const owned of [await h.owned(), await load(state).owned()]) {
    assert.deepEqual([owned[agent], owned[duplicate.id], owned[reopened.id]], [true, false, false]);
  }
}

// Tabs a browser restart restored are read before the onCreated cleanup acts on them, even
// when their onCreated arrives as this script loads.
async function restoredTabsKeepTheirValue() {
  const state = browserState();
  addTab(state, { id: 5 });
  state.values.set(`5|${KEY}`, JSON.stringify({ v: 1 }));
  const restore = state.queryGate = gate();
  const h = load(state);
  h.onCreated.emit({ ...state.tabs.get(5) });
  await settle();
  restore.release();
  await settle();
  assert.deepEqual(stored(state, 5), { v: 1 });
  assert.deepEqual(state.valueRemovals, []);
  assert.equal((await h.owned())[5], true);
}

// Without browser.sessions, ownership is in memory only, as before #376.
async function withoutSessionsOwnershipIsInMemory() {
  const state = browserState({ sessions: false });
  addTab(state, { id: 6 });
  const h = load(state);
  const { tabId } = await h.run({ action: 'newTab', url: 'https://example.com/new' });
  assert.deepEqual(await h.owned(), { 6: false, [tabId]: true });
  assert.equal((await h.run({ action: 'closeTab', tabId: 6 })).code, 'user_tab_read_only');
  assert.deepEqual(await h.run({ action: 'closeTab', tabId }), { ok: true });
  const { tabId: kept } = await h.run({ action: 'newTab', url: 'https://example.com/kept' });
  assert.deepEqual(await load(state).owned(), { 6: false, [kept]: false }, 'a reload forgets it');
}

// A failing tab value API leaves ownership in memory and does not fail the command.
async function failingSessionsDoNotBreakCommands() {
  const state = browserState();
  // Every sessions call rejects.
  const boom = () => { throw new Error('boom'); };
  state.values = { get: boom, set: boom, delete: boom };
  const h = load(state);
  const { tabId } = await h.run({ action: 'newTab', url: 'https://example.com/new' });
  assert.equal(typeof tabId, 'number');
  assert.equal((await h.owned())[tabId], true);
  assert.equal((await load(state).owned())[tabId], false, 'unreadable values: not owned');
}

// #394: ownership is the stable MCP owner's, never whichever client lists/acts last.
async function perClientOwnershipAndRestart() {
  const state = browserState();
  addTab(state, { id: 1 });
  addTab(state, { id: 2 });
  state.values.set(`2|${KEY}`, JSON.stringify({ v: 1 })); // migration: no invented owner
  const h = load(state);
  const a = (await h.run({ action: 'newTab', ownerId: 'client-a' })).tabId;
  const b = (await h.run({ action: 'newTab', ownerId: 'client-b' })).tabId;
  const dup = (await h.run({ action: 'duplicateTab', tabId: a, ownerId: 'client-b' })).tabId;
  const win = await h.run({ action: 'createWindow', ownerId: 'client-a' });
  await settle();
  assert.deepEqual(stored(state, a), { v: 2, owner: 'client-a' });
  assert.deepEqual(stored(state, dup), { v: 2, owner: 'client-b' });
  for (const loaded of [h, load(state)]) {
    assert.deepEqual(await loaded.owned('client-a'),
      { 1: false, 2: false, [a]: true, [b]: false, [dup]: false, [win.tabIds[0]]: true });
    assert.deepEqual(await loaded.owned('client-b'),
      { 1: false, 2: false, [a]: false, [b]: true, [dup]: true, [win.tabIds[0]]: false });
    assert.equal((await loaded.owned())[a], false, 'anonymous cannot claim a named owner');
  }
  // A restored browser assigns new tab IDs; session values follow tabs, not old IDs.
  const restored = browserState();
  for (const [id, tab] of state.tabs) {
    addTab(restored, { ...tab, id: id + 1000 });
    const value = stored(state, id);
    if (value) restored.values.set(`${id + 1000}|${KEY}`, JSON.stringify(value));
  }
  const restarted = load(restored);
  assert.equal((await restarted.owned('client-a'))[a + 1000], true);
  assert.equal((await restarted.owned('client-b'))[a + 1000], false);
  assert.equal((await restarted.owned('client-a'))[2 + 1000], false);
}

async function openerInheritanceAndTrustedInput() {
  const state = browserState();
  addTab(state, { id: 1 });
  const h = load(state);
  const a = (await h.run({ action: 'newTab', ownerId: 'client-a' })).tabId;
  state.now += 10000; // no recent agent action: window.open/target=_blank still inherits
  const open = (id, openerTabId) => {
    addTab(state, { id, openerTabId });
    h.onCreated.emit({ ...state.tabs.get(id) });
  };
  open(200, a);
  open(201, 200);
  open(202, 1);
  assert.equal((await h.owned('client-a'))[200], true);
  assert.equal((await h.owned('client-a'))[201], true, 'transitive inheritance');
  assert.equal((await h.owned('client-a'))[202], false, 'user opener');
  assert.equal((await h.owned('client-b'))[200], false);
  state.now++;
  h.input(a);
  open(203, a); // middle-click with its pointerdown already delivered
  state.now += 1000;
  open(204, a); // inclusive 1 s boundary
  state.now++;
  open(205, a);
  assert.deepEqual([203, 204, 205].map(id => stored(state, id)),
    [undefined, undefined, { v: 2, owner: 'client-a' }]);
  // Late delivery still uses event time, and revokes inherited descendants, not explicit creations.
  state.now += 2000;
  const at = state.now;
  open(206, a);
  open(207, 206);
  state.now += 1500;
  h.input(a, at);
  assert.equal((await h.owned('client-a'))[206], false);
  assert.equal((await h.owned('client-a'))[207], false);
  assert.equal((await h.owned('client-a'))[a], true, 'input never returns its opener to the user');
  // Input after creation does not change a tab that was already open.
  open(208, a);
  state.now++;
  h.input(a);
  assert.equal((await h.owned('client-a'))[208], true);
  await settle();
  const reloaded = await load(state).owned('client-a');
  for (const id of [200, 201, 205, 208]) assert.equal(reloaded[id], true, `tab ${id}`);
  for (const id of [202, 203, 204, 206, 207]) assert.equal(reloaded[id], false, `tab ${id}`);
}

async function explicitDuplicateOwnerReachesEarlyDescendants() {
  const state = browserState();
  const h = load(state);
  const a = (await h.run({ action: 'newTab', ownerId: 'client-a' })).tabId;
  addTab(state, { id: 200, openerTabId: a });
  h.onCreated.emit({ ...state.tabs.get(200) });
  state.beforeDuplicateResolves = async () => {
    // A duplicated page can open its own tab before tabs.duplicate resolves to its caller.
    addTab(state, { id: 300, openerTabId: state.nextId - 1 });
    h.onCreated.emit({ ...state.tabs.get(300) });
  };
  const dup = await h.run({ action: 'duplicateTab', tabId: 200, ownerId: 'client-b' });
  assert.equal((await h.owned('client-b'))[dup.tabId], true);
  assert.equal((await h.owned('client-b'))[300], true);
  assert.equal((await h.owned('client-a'))[300], false);
  await settle();
  assert.deepEqual(stored(state, 300), { v: 2, owner: 'client-b' });
}

async function restoredOpenerCanPassOnOwnership() {
  const state = browserState();
  addTab(state, { id: 5 });
  state.values.set(`5|${KEY}`, JSON.stringify({ v: 2, owner: 'client-a' }));
  const restore = state.gate = gate();
  const h = load(state);
  addTab(state, { id: 6, openerTabId: 5 });
  h.onCreated.emit({ ...state.tabs.get(6) });
  await settle();
  restore.release();
  await settle();
  assert.equal((await h.owned('client-a'))[6], true);
  assert.deepEqual(stored(state, 6), { v: 2, owner: 'client-a' });
}

async function inputDuringRestoreStaysUserOpened() {
  const state = browserState();
  addTab(state, { id: 5 });
  state.values.set(`5|${KEY}`, JSON.stringify({ v: 2, owner: 'client-a' }));
  const restore = state.gate = gate();
  const h = load(state);
  addTab(state, { id: 6, openerTabId: 5 });
  h.onCreated.emit({ ...state.tabs.get(6) });
  h.input(5); // qualifies at creation, but inheritance is still waiting for the owner
  state.now++;
  h.input(5); // newer input must not erase the qualifying event
  restore.release();
  await settle();
  assert.equal((await h.owned('client-a'))[6], false);
  assert.equal(stored(state, 6), undefined);
  assert.equal((await load(state).owned('client-a'))[6], false);
}

async function handoffsNeverRestoreCopiedPersistentOwnership() {
  const state = browserState();
  state.tabs.set(1, { id: 1, windowId: 1, active: true, url: 'https://user.test/', title: 'User' });
  const h = load(state);
  assert.equal((await h.owned('client-a'))[1], false);
  await h.run({ action: 'openHandoffSession', sessionId: 'session-a', ownerId: 'client-a', clientName: 'A' });
  const request = h.start({ action: 'requestTab', tabId: 1, sessionId: 'session-a', ownerId: 'client-a', reason: 'work' });
  await settle();
  // Firefox can copy a session value after onCreated, while cleanup still awaits storage.
  state.values.set(`1|${KEY}`, JSON.stringify({ v: 2, owner: 'client-a' }));
  await h.decide(h.handoffs().requests[0].id);
  assert.equal((await request.result).owned, true);
  assert.equal(stored(state, 1), undefined);
  // Late ownership bookkeeping must not promote an ephemeral grant or remove its provenance.
  h.remark(1, 'client-a');
  await settle();
  assert.equal(stored(state, 1), undefined);
  const native = await h.run({ action: 'checkTabWrite', writeAction: 'trustedClick', tabId: 1,
    ownerId: 'client-a', sessionId: 'session-a', userTabsWritable: true });
  assert.equal(native.code, 'unsupported_target');
  assert.equal(native.state.reason, 'handoff_native_input');
  assert.equal((await load(state).owned('client-a'))[1], false, 'handoff must never survive extension reload');
}

(async () => {
  await handoffsNeverRestoreCopiedPersistentOwnership();
  await inputDuringRestoreStaysUserOpened();
  await perClientOwnershipAndRestart();
  await openerInheritanceAndTrustedInput();
  await restoredOpenerCanPassOnOwnership();
  await explicitDuplicateOwnerReachesEarlyDescendants();
  await markedTabsStoreTheirValue();
  await aFreshLoadRestoresOwnershipBeforeTheFirstCommand();
  await aTabWithoutAValueStaysAUserTab();
  await aTabGivenBackToTheUserLosesItsValue();
  await theAgentsDuplicateStaysOwned();
  await newAndOpenedTabsStayOwned();
  await userCopiesOfAgentTabsAreTheUsers();
  await restoredTabsKeepTheirValue();
  await withoutSessionsOwnershipIsInMemory();
  await failingSessionsDoNotBreakCommands();
  console.log('agent_tab_persistence: ok');
})().catch(e => { console.error(e); process.exit(1); });
