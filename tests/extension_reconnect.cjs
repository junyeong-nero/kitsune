// Run with: node tests/extension_reconnect.cjs (no browser or npm dependencies).
// Issue #299: on every startup the extension dropped its first bridge connection. connect() ran
// when the script loaded and again from runtime.onStartup / onInstalled, and the replaced socket's
// onclose then closed its replacement and scheduled a reconnect.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function event() {
  const listeners = new Set();
  return { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
    fire: (...args) => [...listeners].forEach(fn => fn(...args)) };
}

// The extension's background script with a WebSocket that opens and closes when the test says so.
function background({ tabs = {} } = {}) {
  const sockets = [];
  const logs = [];
  class WebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 3;
    constructor(url) { Object.assign(this, { url, readyState: WebSocket.CONNECTING, messages: [], closed: false }); sockets.push(this); }
    send(message) { this.messages.push(JSON.parse(message)); }
    close() { Object.assign(this, { closed: true, readyState: WebSocket.CLOSED }); }
    open() { this.readyState = WebSocket.OPEN; return this.onopen(); }
  }
  const browser = {
    tabs: {
      onCreated: event(), onUpdated: event(), onRemoved: event(),
      query: async () => Object.values(tabs).map(tab => ({ ...tab })),
      get: async id => { if (!tabs[id]) throw new Error(`Invalid tab ID: ${id}`); return { ...tabs[id] }; },
      sendMessage: async () => ({ ok: true }),
      executeScript: async () => [],
    },
    windows: { onCreated: event(), onRemoved: event(), getAll: async () => [{ id: 1 }] },
    permissions: { onAdded: event(), onRemoved: event(), contains: async () => true },
    runtime: {
      onMessage: event(), onStartup: event(), onInstalled: event(),
      getManifest: () => ({ version: '0.2.1' }),
      getURL: path => `moz-extension://kitsune/${path}`,
    },
    webRequest: { onBeforeRequest: event(), onHeadersReceived: event() },
  };
  const context = vm.createContext({
    browser, URL, WebSocket, Date, crypto, TextEncoder, Blob,
    fetch: async () => { throw new TypeError('NetworkError when attempting to fetch resource.'); },
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    console: { log: (...args) => logs.push(args.join(' ')), error() {} },
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  return {
    browser, context, sockets,
    socket: () => sockets[sockets.length - 1],
    reconnects: () => logs.filter(line => line.startsWith('[Kitsune] Reconnecting in')).length,
  };
}

async function testStartupEventsKeepTheLoadTimeConnection() {
  for (const fire of ['onInstalled', 'onStartup']) {
    for (const opened of [false, true]) {
      const h = background();
      const first = h.socket();
      if (opened) await first.open();
      h.browser.runtime[fire].fire({ reason: 'install' });
      assert.equal(h.sockets.length, 1, `${fire} with the connection ${opened ? 'open' : 'opening'}: no second socket`);
      assert.equal(first.closed, false);
      assert.equal(h.reconnects(), 0);
    }
  }
  // Without a live connection (the bridge was down at load) the events still connect at once.
  const h = background();
  h.socket().onclose();
  h.browser.runtime.onStartup.fire();
  assert.equal(h.sockets.length, 2);
}

async function testReplacedSocketDoesNotCloseItsReplacement() {
  const h = background();
  const first = h.socket();
  h.context.connect();  // replaces it, as the startup listeners used to
  const second = h.socket();
  assert.equal(first.closed, true);
  first.onclose();  // the browser reports the replaced socket closed afterwards
  assert.equal(second.closed, false, 'the replacement stays open');
  assert.equal(h.reconnects(), 0, 'and no reconnect is scheduled');
  await second.open();
  assert.equal(second.messages[0].type, 'hello');
  second.onclose();  // the live connection closing still reconnects
  assert.equal(h.reconnects(), 1);
}

async function testARepliedCommandOutlivesItsReplacedSocket() {
  // The bridge matches replies to pending commands by id on any connection, and fails a
  // command only once it handles the close of the socket it was sent on. A command that
  // finishes after its socket was replaced therefore still answers on the live one.
  const h = background({ tabs: { 1: { id: 1, url: 'https://a.example/', title: 'A', active: true } } });
  const first = h.socket();
  await first.open();
  const handled = first.onmessage({ data: JSON.stringify({ id: 'c1', action: 'getTabs' }) });
  h.context.connect();  // replaced while the command runs
  const second = h.socket();
  await second.open();
  first.onclose();
  await handled;
  assert.deepEqual(second.messages.find(message => message.id === 'c1').result.tabs.map(tab => tab.id), [1]);
  assert.equal(second.closed, false);
}

(async () => {
  await testStartupEventsKeepTheLoadTimeConnection();
  await testReplacedSocketDoesNotCloseItsReplacement();
  await testARepliedCommandOutlivesItsReplacedSocket();
  console.log('extension reconnect ok');
})().catch(error => { console.error(error); process.exit(1); });
