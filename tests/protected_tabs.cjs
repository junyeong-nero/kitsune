// Run with: node tests/protected_tabs.cjs (no browser or npm dependencies).
// #395 replaces navigation approval with deterministic ownership refusal.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const event = () => ({ addListener() {}, removeListener() {} });
const copy = value => JSON.parse(JSON.stringify(value));

// focusedWindow: the browser window the user last focused. By default another one, so tab 1 is not
// held for being viewed (#326, see shared_browser.cjs).
function harness({ focusedWindow = 2 } = {}) {
  const tabs = {
    1: { id: 1, windowId: 1, active: true, url: 'https://example.com/start', title: 'Start' },
    2: { id: 2, windowId: 1, active: false, url: 'https://example.com/helper', title: 'Helper' },
  };
  const moved = [];
  const browser = {
    tabs: {
      onCreated: event(), onUpdated: event(), onRemoved: event(), onActivated: event(),
      get: async id => ({ ...tabs[id] }),
      query: async query => Object.values(tabs).filter(tab => !query.active || tab.active),
      goBack: async id => { moved.push(['goBack', id]); },
      goForward: async id => { moved.push(['goForward', id]); },
      reload: async id => { moved.push(['reload', id]); },
    },
    windows: { getLastFocused: async () => ({ id: focusedWindow, focused: true }) },
    storage: { local: { get: async () => ({}), set: async () => {} } },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
  };
  const context = vm.createContext({
    browser, console: { log() {}, error() {} },
    WebSocket: class { close() {} },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  context.userActiveError = async () => null;
  context.navigateTab = async (url, tabId) => {
    moved.push(['navigate', tabId || 1]);
    return { ok: true, url };
  };
  context.markAgentTab(2);
  const run = async command => copy(await context.handleCommand({ includeSnapshot: false, ...command }));
  return { run, moved };
}

async function protectedInitialTabIsRefused() {
  const h = harness();
  for (const command of [
    { action: 'navigate', url: 'https://example.com/other', tabId: 1 },
    { action: 'navigate', url: 'https://example.com/other' },
    { action: 'goBack', tabId: 1 },
    { action: 'goForward', tabId: 1 },
  ]) {
    const result = await h.run({ ...command, protectUserTabs: true });
    assert.equal(result.code, 'user_tab_read_only', JSON.stringify(result));
    assert.equal(result.retryable, false);
    assert.deepEqual(result.state, { tabId: 1, index: 0, action: command.action });
    assert.match(result.hint, /browser_tabs/);
  }
  assert.deepEqual(h.moved, [], 'nothing navigated, and no other tab was used instead');
}

async function ownedHelperTabNavigates() {
  const h = harness();
  assert.equal((await h.run({ action: 'navigate', url: 'https://example.com/x', tabId: 2, protectUserTabs: true })).ok, true);
  assert.equal((await h.run({ action: 'goBack', tabId: 2, protectUserTabs: true })).ok, true);
  assert.deepEqual(h.moved, [['navigate', 2], ['goBack', 2]]);
}

async function authorizedNavigationRuns() {
  const h = harness();
  for (const action of ['navigate', 'goBack', 'goForward', 'reloadTabBrowser']) {
    const command = { action, tabId: 1, url: 'https://example.com/x', userTabApproved: true };
    assert.equal((await h.run(command)).code, 'user_tab_read_only', 'old approval is not ownership');
    assert.equal((await h.run({ ...command, userTabsWritable: true })).ok, true);
  }
  assert.equal(h.moved.length, 4);
}

(async () => {
  await protectedInitialTabIsRefused();
  await ownedHelperTabNavigates();
  await authorizedNavigationRuns();
  console.log('protected_tabs: ok');
})().catch(e => { console.error(e); process.exit(1); });
