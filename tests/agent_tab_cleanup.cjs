// Run with: node tests/agent_tab_cleanup.cjs (no browser or npm dependencies).
// Agent tab cleanup: the toolbar popup closes the agent's tabs, and an opt-in idle limit closes
// those nobody used for that long. User tabs and pinned tabs always stay.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const MINUTE = 60000;
const POPUP_URL = 'moz-extension://kitsune/popup/popup.html';
const flush = async () => { for (let i = 0; i < 10; i++) await new Promise(r => setImmediate(r)); };

function event() {
  const listeners = new Set();
  return {
    addListener: fn => listeners.add(fn),
    removeListener: fn => listeners.delete(fn),
    emit: (...args) => [...listeners].map(fn => fn(...args)),
  };
}

function load({ idleMinutes } = {}) {
  const clock = { now: 1_000_000 };
  const tabs = new Map();
  const removed = [];
  const onActivated = event(), onMessage = event(), onAlarm = event();
  const alarms = new Map(); // name -> create() info
  const browser = {
    tabs: {
      onCreated: event(), onUpdated: event(), onRemoved: event(), onActivated,
      get: async id => {
        if (!tabs.has(id)) throw new Error(`Invalid tab ID: ${id}`);
        return { ...tabs.get(id) };
      },
      query: async () => [...tabs.values()].map(tab => ({ ...tab })),
      remove: async id => {
        if (!tabs.has(id)) throw new Error(`Invalid tab ID: ${id}`);
        tabs.delete(id);
        removed.push(id);
      },
      sendMessage: async () => null,
    },
    alarms: { create: (name, info) => alarms.set(name, info), onAlarm },
    windows: { getLastFocused: async () => ({ id: 1, focused: true }), getAll: async () => [{ id: 1 }] },
    storage: { local: {
      get: async () => (idleMinutes === undefined ? {} : { agentTabIdleMinutes: idleMinutes }),
      set: async () => {},
    } },
    runtime: {
      onMessage, onStartup: event(), onInstalled: event(),
      getURL: path => `moz-extension://kitsune/${path}`,
    },
  };
  class WebSocket { constructor() { this.readyState = 1; } send() {} close() {} }
  const context = vm.createContext({
    browser, URL, WebSocket, console: { log() {}, error() {} },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  });
  vm.runInContext('Date', context).now = () => clock.now;
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  const addTab = (tab, { agent = false } = {}) => {
    tabs.set(tab.id, { windowId: 1, active: false, pinned: false, audible: false, url: `https://example.com/${tab.id}`, ...tab });
    if (agent) context.markAgentTab(tab.id);
  };
  const message = (msg, sender) => Promise.resolve(onMessage.emit(msg, sender).find(r => r !== undefined));
  return { context, clock, tabs, removed, addTab, onActivated, message, alarms, onAlarm, onCreated: browser.tabs.onCreated };
}

async function popupClosesEveryUnpinnedAgentTab() {
  const h = load();
  h.addTab({ id: 1 });                                 // the user's
  h.addTab({ id: 2 }, { agent: true });
  h.addTab({ id: 3, active: true }, { agent: true });  // shown: an explicit close still closes it
  h.addTab({ id: 4, pinned: true }, { agent: true });  // pinned: kept
  const popup = { url: POPUP_URL };
  assert.deepEqual({ ...await h.message({ action: 'agentTabCount' }, popup) }, { count: 2 });
  assert.deepEqual({ ...await h.message({ action: 'closeAgentTabs' }, popup) }, { closed: 2 });
  assert.deepEqual(h.removed.sort(), [2, 3]);
  assert.deepEqual({ ...await h.message({ action: 'agentTabCount' }, popup) }, { count: 0 });
}

async function pagesCannotCloseAgentTabs() {
  const h = load();
  h.addTab({ id: 2 }, { agent: true });
  // A content script's message comes from a tab; another extension page has another URL.
  assert.equal(await h.message({ action: 'closeAgentTabs' }, { tab: { id: 2 }, url: POPUP_URL }), undefined);
  assert.equal(await h.message({ action: 'closeAgentTabs' }, { url: 'moz-extension://kitsune/options/options.html' }), undefined);
  await flush();
  assert.deepEqual(h.removed, []);
}

async function idleLimitIsOffByDefault() {
  const h = load();
  h.addTab({ id: 2 }, { agent: true });
  h.clock.now += 24 * 60 * MINUTE;
  assert.deepEqual({ ...await h.context.sweepIdleAgentTabs() }, { closed: 0 });
  assert.deepEqual(h.removed, []);
}

async function idleLimitClosesOnlyIdleBackgroundAgentTabs() {
  const h = load({ idleMinutes: 30 });
  h.addTab({ id: 1 });                                  // the user's
  h.addTab({ id: 2 }, { agent: true });                 // idle
  h.addTab({ id: 3, active: true }, { agent: true });   // shown
  h.addTab({ id: 4, pinned: true }, { agent: true });   // pinned
  h.addTab({ id: 5, audible: true }, { agent: true });  // playing sound
  h.addTab({ id: 6 }, { agent: true });                 // the agent acts on it later
  h.addTab({ id: 7 }, { agent: true });                 // the user looks at it later
  h.addTab({ id: 8 }, { agent: true });                 // an agent command still runs on it
  h.clock.now += 20 * MINUTE;
  h.context.touchStart(6);
  h.context.touchEnd(6);
  h.onActivated.emit({ tabId: 7, previousTabId: 1, windowId: 1 });
  h.context.touchStart(8);
  h.clock.now += 10 * MINUTE;
  assert.deepEqual({ ...await h.context.sweepIdleAgentTabs() }, { closed: 1 });
  assert.deepEqual(h.removed, [2]);
  h.clock.now += 20 * MINUTE;
  await h.context.sweepIdleAgentTabs();
  assert.deepEqual(h.removed.sort(), [2, 6, 7]);
}

async function aMinuteAlarmRunsTheSweep() {
  const h = load({ idleMinutes: 15 });
  assert.equal(JSON.stringify([...h.alarms]), JSON.stringify([['kitsune.agentTabIdleSweep', { periodInMinutes: 1 }]]));
  h.addTab({ id: 2 }, { agent: true });
  h.clock.now += 15 * MINUTE;
  h.onAlarm.emit({ name: 'other' });
  await flush();
  assert.deepEqual(h.removed, []);
  h.onAlarm.emit({ name: 'kitsune.agentTabIdleSweep' });
  await flush();
  assert.deepEqual(h.removed, [2]);
}

async function restoredAgentTabsCountFromTheReload() {
  // A tab owned before this load has no use time: it counts as used when background.js loaded.
  const h = load({ idleMinutes: 30 });
  h.tabs.set(9, { id: 9, windowId: 1, active: false, pinned: false, audible: false, url: 'https://example.com/9' });
  h.context.ownAgentTab(9); // ownership only, as restoreAgentTabs records it
  h.clock.now += 29 * MINUTE;
  await h.context.sweepIdleAgentTabs();
  assert.deepEqual(h.removed, []);
  h.clock.now += MINUTE;
  await h.context.sweepIdleAgentTabs();
  assert.deepEqual(h.removed, [9]);
}

async function freshInheritedTabIsNotIdleAfterLongUptime() {
  const h = load({ idleMinutes: 15 });
  h.addTab({ id: 1, pinned: true }, { agent: true });
  h.clock.now += 60 * MINUTE;
  h.addTab({ id: 2, openerTabId: 1 });
  h.onCreated.emit({ ...h.tabs.get(2) });
  await flush();
  await h.context.sweepIdleAgentTabs();
  assert.deepEqual(h.removed, [], 'a new popup is not as old as the extension');
  h.clock.now += 14 * MINUTE;
  // Reconciliation is not tab use: even a change of inherited owner must keep the first time.
  h.context.markAgentTab(1, 'other-client');
  await h.context.sweepIdleAgentTabs();
  assert.deepEqual(h.removed, []);
  h.clock.now += MINUTE;
  await h.context.sweepIdleAgentTabs();
  assert.deepEqual(h.removed, [2]);
}

(async () => {
  await freshInheritedTabIsNotIdleAfterLongUptime();
  await popupClosesEveryUnpinnedAgentTab();
  await pagesCannotCloseAgentTabs();
  await idleLimitIsOffByDefault();
  await idleLimitClosesOnlyIdleBackgroundAgentTabs();
  await aMinuteAlarmRunsTheSweep();
  await restoredAgentTabsCountFromTheReload();
  console.log('agent_tab_cleanup: ok');
})().catch(e => { console.error(e); process.exit(1); });
