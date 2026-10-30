// Run with: node tests/protected_tabs.cjs (no browser or npm dependencies).
// Issue #199: with protectUserTabs, navigate/back/forward leave tabs the agent did not open alone.
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
      query: async () => Object.values(tabs).filter(tab => tab.active),
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
    assert.equal(result.code, 'user_tab', JSON.stringify(result));
    assert.equal(result.retryable, false);
    assert.deepEqual(result.state, { tabId: 1, url: 'https://example.com/start', title: 'Start' });
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
  // Approved by the user through the bridge's confirm token.
  const approved = await h.run({ action: 'navigate', url: 'https://example.com/x', tabId: 1,
    protectUserTabs: true, userTabApproved: true });
  assert.equal(approved.ok, true);
  // Without protection, navigating a user tab is unchanged.
  assert.equal((await h.run({ action: 'navigate', url: 'https://example.com/y', tabId: 1 })).ok, true);
  assert.equal((await h.run({ action: 'goForward', tabId: 1 })).ok, true);
  // Limits: reload keeps the URL and is not guarded.
  assert.equal((await h.run({ action: 'reloadTabBrowser', tabId: 1, protectUserTabs: true })).ok, true);
  assert.deepEqual(h.moved, [['navigate', 1], ['navigate', 1], ['goForward', 1], ['reload', 1]]);
}

// The user's approval of navigating their tab also covers it while they view it (#326).
async function approvedNavigationOfTheViewedTabRuns() {
  const h = harness({ focusedWindow: 1 });
  const held = await h.run({ action: 'navigate', url: 'https://example.com/x', tabId: 1 });
  assert.equal(held.code, 'user_viewing', JSON.stringify(held));
  assert.equal((await h.run({ action: 'navigate', url: 'https://example.com/x', tabId: 1, protectUserTabs: true })).code,
    'user_tab', 'a protected tab asks for one approval: user_tab');
  const approved = await h.run({ action: 'navigate', url: 'https://example.com/x', tabId: 1,
    protectUserTabs: true, userTabApproved: true });
  assert.equal(approved.ok, true);
  assert.deepEqual(h.moved, [['navigate', 1]]);
}

(async () => {
  await protectedInitialTabIsRefused();
  await ownedHelperTabNavigates();
  await authorizedNavigationRuns();
  await approvedNavigationOfTheViewedTabRuns();
  console.log('protected_tabs: ok');
})().catch(e => { console.error(e); process.exit(1); });
