// Run with: node tests/chrome_compat.cjs (no browser or npm dependencies).
// Issue #589: extension/chrome/compat.js gives the Chrome build Firefox's `browser` API.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'extension', 'chrome', 'compat.js'), 'utf8');

// A chrome global whose onMessage passes messages to its listeners as Chrome does: a listener
// answers through sendResponse, and only after returning true if it answers later.
function chromeApi() {
  const listeners = new Set();
  const injected = [];
  const stored = {};
  const removedListeners = new Set();
  const deliver = message => new Promise((resolve, reject) => {
    let answered = false;
    const sendResponse = value => { answered = true; resolve(value); };
    const waits = [...listeners].map(fn => fn(message, { id: 'sender' }, sendResponse)).some(r => r === true);
    if (!waits && !answered) reject(new Error('The message port closed before a response was received.'));
  });
  return {
    injected,
    runtime: {
      id: 'kitsune',
      onMessage: { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn) },
      sendMessage: deliver,
    },
    stored,
    closeTab: tabId => [...removedListeners].forEach(fn => fn(tabId)),
    pageHistory: { canGoBack: false, steps: [] },
    tabs: {
      sendMessage: (tabId, message) => deliver(message),
      onRemoved: { addListener: fn => removedListeners.add(fn) },
      goBack: async tabId => { if (tabId !== 1) throw new Error('Cannot find a next page in history.'); },
      goForward: async () => { throw new Error('Cannot find a next page in history.'); },
    },
    sessions: {},
    storage: {
      session: {
        get: async key => key === null ? { ...stored } : (key in stored ? { [key]: stored[key] } : {}),
        set: async items => { Object.assign(stored, items); },
        remove: async keys => { for (const key of [keys].flat()) delete stored[key]; },
      },
    },
    scripting: {
      executeScript: async function (details) {
        if (details.func) {
          // The page's history, in a tab whose entries Chrome's tabs.goBack skips.
          const { pageHistory } = this.owner;
          const navigation = { canGoBack: pageHistory.canGoBack, canGoForward: false };
          const history = { back: () => pageHistory.steps.push('back'), forward: () => pageHistory.steps.push('forward') };
          const result = new Function('navigation', 'history', `return (${details.func})(...arguments[2])`)(
            navigation, history, details.args);
          return [{ frameId: 0, result }];
        }
        injected.push(details);
        return [{ frameId: 0, result: 'done' }];
      },
    },
  };
}

function load(globals) {
  if (globals.chrome) globals.chrome.scripting.owner = globals.chrome;
  const context = vm.createContext({ ...globals });
  context.globalThis = context;
  vm.runInContext(SOURCE, context);
  return context;
}

(async () => {
  // Firefox: `browser` exists and stays untouched.
  const firefox = { runtime: {} };
  assert.equal(load({ browser: firefox, chrome: chromeApi() }).browser, firefox);

  const chrome = chromeApi();
  const { browser } = load({ chrome });
  assert.equal(browser, chrome);

  // A listener's promise answers the message; its rejection rejects sendMessage.
  const listener = async message => {
    if (message.fail) throw new Error('boom');
    return { echo: message.value };
  };
  browser.runtime.onMessage.addListener(listener);
  assert.deepEqual(await browser.runtime.sendMessage({ value: 1 }), { echo: 1 });
  assert.deepEqual(await browser.tabs.sendMessage(7, { value: 2 }), { echo: 2 });
  await assert.rejects(browser.runtime.sendMessage({ fail: true }), /boom/);
  browser.runtime.onMessage.removeListener(listener);
  await assert.rejects(browser.runtime.sendMessage({ value: 3 }), /port closed/);

  // A listener that answers through sendResponse and returns true keeps working.
  browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
    setTimeout(() => sendResponse({ late: message.value }), 0);
    return true;
  });
  assert.deepEqual(await browser.runtime.sendMessage({ value: 4 }), { late: 4 });

  // tabs.executeScript's file form injects compat.js first, into the frames Firefox would;
  // content.js also into the main world.
  assert.deepEqual(await browser.tabs.executeScript(5, { file: '/content/content.js' }), ['done']);
  await browser.tabs.executeScript(5, { file: '/content/content.js', frameId: 3 });
  await browser.tabs.executeScript(5, { file: '/content/frame-input.js', allFrames: true, matchAboutBlank: true });
  assert.deepEqual(JSON.parse(JSON.stringify(chrome.injected)), [
    { target: { tabId: 5 }, files: ['/chrome/page-world.js'], world: 'MAIN' },
    { target: { tabId: 5 }, files: ['/chrome/compat.js', '/content/content.js'] },
    { target: { tabId: 5, frameIds: [3] }, files: ['/chrome/page-world.js'], world: 'MAIN' },
    { target: { tabId: 5, frameIds: [3] }, files: ['/chrome/compat.js', '/content/content.js'] },
    { target: { tabId: 5, allFrames: true }, files: ['/chrome/compat.js', '/content/frame-input.js'] },
  ]);

  // Session tab values live in storage.session, per tab, and go when the tab closes.
  await browser.sessions.setTabValue(1, 'kitsune.owned', { v: 2, owner: 'a' });
  await browser.sessions.setTabValue(10, 'kitsune.owned', { v: 1 });
  assert.deepEqual({ ...(await browser.sessions.getTabValue(1, 'kitsune.owned')) }, { v: 2, owner: 'a' });
  assert.equal(await browser.sessions.getTabValue(2, 'kitsune.owned'), undefined);
  chrome.closeTab(1);
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(await browser.sessions.getTabValue(1, 'kitsune.owned'), undefined);
  assert.deepEqual({ ...(await browser.sessions.getTabValue(10, 'kitsune.owned')) }, { v: 1 });
  await browser.sessions.removeTabValue(10, 'kitsune.owned');
  assert.deepEqual(Object.keys(chrome.stored), []);

  // goBack/goForward fall back to the page's history for entries Chrome's tabs API skips, and fail
  // as before when the page has none either.
  await browser.tabs.goBack(1);
  assert.deepEqual(chrome.pageHistory.steps, []);
  await assert.rejects(browser.tabs.goBack(2), /Cannot find a next page/);
  chrome.pageHistory.canGoBack = true;
  await browser.tabs.goBack(2);
  assert.deepEqual(chrome.pageHistory.steps, ['back']);
  await assert.rejects(browser.tabs.goForward(2), /Cannot find a next page/);
  console.log('chrome_compat: ok');
})().catch(error => { console.error(error); process.exit(1); });
