// Run with: node tests/navigate_invalid_url.cjs (no browser or npm dependencies).
// #571: a navigate url that is not an absolute URL gets a coded error before tabs.update.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function event() {
  const listeners = new Set();
  return { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn) };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

// The command's result, which must arrive without a timer: a refusal waits for no load. A
// command still pending fails here instead of letting the process exit silently.
async function answer(pending) {
  let settled = null;
  pending.then(result => { settled = { result }; });
  for (let i = 0; i < 20 && !settled; i++) await flush();
  assert.ok(settled, 'the command did not answer');
  return settled.result;
}

// One agent tab (id 7) at startUrl. Timers never fire: a refusal needs none, and the accepted
// navigation is only followed as far as its tabs.update call.
function harness(startUrl) {
  const tab = { id: 7, url: startUrl, title: 'Start', status: 'complete', discarded: false };
  const updates = [];
  const browser = {
    tabs: {
      onCreated: event(), onUpdated: event(), onRemoved: event(),
      query: async () => [{ ...tab }],
      get: async () => ({ ...tab }),
      update: async (id, props) => { updates.push({ id, ...props }); return { ...tab }; },
      sendMessage: async () => { throw new Error('Unexpected content message'); },
      executeScript: async () => { throw new Error('Unexpected content injection'); },
    },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
  };
  const context = vm.createContext({
    browser, URL, console: { log() {}, error() {} },
    WebSocket: class { close() {} },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  context.markAgentTab(7);
  return {
    updates,
    send: url => context.handleCommand({ action: 'navigate', tabId: 7, includeSnapshot: false, url })
      .then(result => JSON.parse(JSON.stringify(result))),
  };
}

async function main() {
  const ORIGIN = 'http://localhost:9101';

  // The issue's script: the origin prepended to a query href that was already absolute.
  const doubled = `${ORIGIN}${ORIGIN}/deals/48`;
  // A relative url reaches the extension only when the bridge could not resolve it; the
  // extension must not resolve it itself, or it would load unseen by the bridge's URL policy.
  for (const url of [doubled, '/deals/48', 'deals/48', '?page=2', '', 'http://']) {
    const h = harness(`${ORIGIN}/deals`);
    const result = await answer(h.send(url));
    const { hint, ...rest } = result;
    assert.deepEqual(rest, { error: `Invalid URL: ${url}`, code: 'invalid_url', retryable: false, state: { url } });
    assert.match(hint, /absolute URL, or a path/);
    assert.match(hint, /query's href and src values are already absolute/);
    assert.deepEqual(h.updates, [], `${JSON.stringify(url)} must not reach tabs.update`);
  }

  // A missing url is refused the same way.
  const missing = harness(`${ORIGIN}/deals`);
  assert.equal((await answer(missing.send(undefined))).code, 'invalid_url');
  assert.deepEqual(missing.updates, []);

  // Absolute URLs still reach tabs.update unchanged, whatever their scheme.
  for (const url of [`${ORIGIN}/deals/48`, 'https://example.com/a?b=1#c', 'about:blank', 'file:///tmp/a.html']) {
    const h = harness(`${ORIGIN}/deals`);
    h.send(url).catch(() => {});
    for (let i = 0; i < 20 && !h.updates.length; i++) await flush();
    assert.deepEqual(h.updates, [{ id: 7, url }]);
  }

  console.log('navigate_invalid_url: all tests passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
