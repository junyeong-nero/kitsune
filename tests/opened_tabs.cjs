// Run with: node tests/opened_tabs.cjs (no browser or npm dependencies).
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

// onClick(h, msg) runs when a click reaches the page, before the click replies.
function harness(onClick = () => {}) {
  let now = 0, nextId = 0;
  const timers = new Map();
  const schedule = (fn, delay, interval = false) => {
    const id = ++nextId;
    timers.set(id, { fn, at: now + delay, interval, delay });
    return id;
  };
  const onCreated = event(), onUpdated = event(), onRemoved = event();
  const tabs = { 1: { id: 1, url: 'https://example.com/ok', status: 'complete', active: true } };
  const listenersAtStart = { updated: onUpdated.listeners.size, removed: onRemoved.listeners.size };
  const h = {
    tabs,
    send: command => context.handleCommand({ includeSnapshot: false, ...command })
      .then(result => JSON.parse(JSON.stringify(result))),
    // Gecko creates a link's tab on about:blank and commits its URL afterwards.
    open: (id, openerTabId, url = 'about:blank') => {
      tabs[id] = { id, url, status: 'loading', active: false, openerTabId };
      onCreated.emit({ ...tabs[id] });
    },
    commit: (id, url) => {
      tabs[id] = { ...tabs[id], url };
      onUpdated.emit(id, { url }, { ...tabs[id] });
    },
    close: id => { delete tabs[id]; onRemoved.emit(id); },
    async tick(ms) {
      const end = now + ms;
      await flush();
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.at <= end)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        const [id, timer] = next;
        now = timer.at;
        if (timer.interval) timer.at += timer.delay;
        else timers.delete(id);
        timer.fn();
        await flush();
      }
      now = end;
      await flush();
    },
    assertClean() {
      assert.equal(onUpdated.listeners.size, listenersAtStart.updated, 'onUpdated leaked');
      assert.equal(onRemoved.listeners.size, listenersAtStart.removed, 'onRemoved leaked');
      assert.equal(timers.size, 0, 'timer leaked');
    },
  };
  const missing = id => new Error(`Invalid tab ID: ${id}`);
  const browser = {
    tabs: {
      onCreated, onUpdated, onRemoved,
      query: async () => Object.values(tabs).filter(tab => tab.active).map(tab => ({ ...tab })),
      get: async id => { if (!tabs[id]) throw missing(id); return { ...tabs[id] }; },
      sendMessage: async (id, msg) => {
        if (msg.action === 'ping') return { ok: true, version: Infinity };
        if (msg.action === 'click') await onClick(h, msg);
        return { ok: true };
      },
      executeScript: async () => {},
    },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
  };
  const context = vm.createContext({
    browser, URL, console: { log() {}, error() {} },
    WebSocket: class { close() {} },
    Date: class extends Date { static now() { return now; } },
    setTimeout: (fn, delay) => schedule(fn, delay),
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => schedule(fn, delay, true),
    clearInterval: id => timers.delete(id),
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  listenersAtStart.updated = onUpdated.listeners.size;
  listenersAtStart.removed = onRemoved.listeners.size;
  return h;
}

// Tracks whether a command has resolved, so a hang fails an assertion instead of exiting silently.
function start(h, command) {
  const pending = { settled: false };
  pending.result = h.send(command).then(result => { pending.settled = true; return result; });
  return pending;
}

async function main() {
  // A target=_blank click reports the new tab once its URL leaves about:blank.
  const blank = harness((h, msg) => { if (msg.selector === '#new') h.open(4, 1); });
  const clicking = start(blank, { action: 'click', tabId: 1, selector: '#new' });
  await blank.tick(50);
  assert.equal(clicking.settled, false, 'waits briefly for the opened tab URL');
  blank.commit(4, 'https://example.com/ok2');
  await blank.tick(0);
  assert.deepEqual(await clicking.result, {
    ok: true, opened_tabs: [{ tabId: 4, url: 'https://example.com/ok2' }],
  });
  blank.assertClean();

  // A no-op click is unchanged; the earlier tab is not reported again by a later action.
  assert.deepEqual(await blank.send({ action: 'click', tabId: 1, selector: '#noop' }), { ok: true });
  assert.deepEqual(await blank.send({ action: 'getOpenedTabs', tabId: 1 }), { opened_tabs: [] });

  // Without tabId the active tab is tracked; tabs opened by other tabs or with no opener are ignored.
  const many = harness(h => {
    h.open(5, 1, 'https://example.com/a');
    h.open(6, 2, 'https://example.com/b');
    h.open(7, undefined, 'https://example.com/c');
    h.open(8, 1, 'https://example.com/d');
  });
  assert.deepEqual(await many.send({ action: 'click', selector: '#many' }), {
    ok: true, opened_tabs: [{ tabId: 5, url: 'https://example.com/a' }, { tabId: 8, url: 'https://example.com/d' }],
  });
  many.assertClean();

  // A tab opened after the action replied is still found by getOpenedTabs; closed tabs drop out.
  many.open(9, 1, 'https://example.com/late');
  many.close(5);
  assert.deepEqual(await many.send({ action: 'getOpenedTabs', tabId: 1 }), {
    opened_tabs: [{ tabId: 8, url: 'https://example.com/d' }, { tabId: 9, url: 'https://example.com/late' }],
  });
  assert.deepEqual(await many.send({ action: 'getOpenedTabs', tabId: 2 }), { opened_tabs: [] });
  // Closing the opener forgets its opened tabs.
  many.close(1);
  assert.deepEqual(await many.send({ action: 'getOpenedTabs', tabId: 1 }), { opened_tabs: [] });

  // A tab that stays on about:blank is reported as such after the bounded wait.
  const stuck = harness(h => h.open(4, 1));
  const waiting = start(stuck, { action: 'click', tabId: 1, selector: '#popup' });
  await stuck.tick(999);
  assert.equal(waiting.settled, false);
  await stuck.tick(1);
  assert.deepEqual(await waiting.result, { ok: true, opened_tabs: [{ tabId: 4, url: 'about:blank' }] });
  stuck.assertClean();

  // A tab that closes while its URL is awaited is not reported.
  const closing = harness(h => h.open(4, 1));
  const closingClick = start(closing, { action: 'click', tabId: 1, selector: '#popup' });
  await closing.tick(10);
  closing.close(4);
  await closing.tick(0);
  assert.deepEqual(await closingClick.result, { ok: true });
  closing.assertClean();

  console.log('Opened tab checks passed');
  finished = true;
}

// A command that never settles empties the event loop; fail instead of exiting 0.
let finished = false;
process.on('exit', () => { if (!finished) process.exitCode = 1; });
main().catch(error => { console.error(error); process.exitCode = 1; });
