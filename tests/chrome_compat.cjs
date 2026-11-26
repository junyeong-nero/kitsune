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
    tabs: { sendMessage: (tabId, message) => deliver(message) },
    scripting: {
      executeScript: async details => { injected.push(details); return [{ frameId: 0, result: 'done' }]; },
    },
  };
}

function load(globals) {
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

  // tabs.executeScript's file form injects compat.js first, into the frames Firefox would.
  assert.deepEqual(await browser.tabs.executeScript(5, { file: '/content/content.js' }), ['done']);
  await browser.tabs.executeScript(5, { file: '/content/content.js', frameId: 3 });
  await browser.tabs.executeScript(5, { file: '/content/frame-input.js', allFrames: true, matchAboutBlank: true });
  assert.deepEqual(JSON.parse(JSON.stringify(chrome.injected)), [
    { target: { tabId: 5 }, files: ['/chrome/compat.js', '/content/content.js'] },
    { target: { tabId: 5, frameIds: [3] }, files: ['/chrome/compat.js', '/content/content.js'] },
    { target: { tabId: 5, allFrames: true }, files: ['/chrome/compat.js', '/content/frame-input.js'] },
  ]);
  console.log('chrome_compat: ok');
})().catch(error => { console.error(error); process.exit(1); });
