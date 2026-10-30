// Run with: node tests/new_tab_wait.cjs (no browser or npm dependencies).
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

// onCreate(h, url) runs inside tabs.create, before the API call resolves. page(), when given,
// is what the content script reports to getPageInfo: [location.href, document.readyState].
function harness(onCreate = () => {}, page = null) {
  let now = 0, nextId = 0;
  const timers = new Map();
  const schedule = (fn, delay, interval = false) => {
    const id = ++nextId;
    timers.set(id, { fn, at: now + delay, interval, delay });
    return id;
  };
  const onUpdated = event(), onRemoved = event();
  let tab = null;
  const h = {
    send: command => context.handleCommand({ includeSnapshot: false, ...command })
      .then(result => JSON.parse(JSON.stringify(result))),
    update: changes => {
      tab = { ...tab, ...changes };
      onUpdated.emit(tab.id, changes, { ...tab });
    },
    silentUpdate: changes => { tab = { ...tab, ...changes }; },
    remove: () => { const id = tab.id; tab = null; onRemoved.emit(id); },
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
      assert.equal(onUpdated.listeners.size, 1, 'onUpdated leaked (the main-frame status listener remains)');
      assert.equal(timers.size, 0, 'timer leaked');
    },
  };
  const browser = {
    tabs: {
      onCreated: event(), onUpdated, onRemoved,
      query: async () => tab ? [{ ...tab }] : [],
      create: async ({ url }) => {
        // Gecko creates the tab on about:blank and navigates it afterwards.
        tab = { id: 9, url: 'about:blank', title: '', status: 'complete', active: true, windowId: 1, index: 0 };
        await onCreate(h, url);
        return { ...tab };
      },
      get: async id => {
        if (!tab || id !== tab.id) throw new Error(`Invalid tab ID: ${id}`);
        return { ...tab };
      },
      sendMessage: async (id, message) => {
        if (!page || message.action !== 'getPageInfo') throw new Error('Unexpected content message');
        const [url, readyState] = page();
        return { url, readyState, title: tab.title };
      },
      executeScript: async () => { throw new Error('Unexpected content injection'); },
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
  return h;
}

// Tracks whether a command has resolved, so a hang fails an assertion instead of exiting silently.
function start(h, command) {
  const pending = { settled: false };
  pending.result = h.send(command).then(result => { pending.settled = true; return result; });
  return pending;
}

// Checks the hint is the stayed_blank one and returns the result without it.
function withoutHint({ hint, ...rest }) {
  assert.match(hint, /never left about:blank/);
  return rest;
}

async function main() {
  // Immediate mode (the default) returns the id without waiting for the load.
  const immediate = harness((h, url) => h.update({ status: 'loading', url }));
  const opened = start(immediate, { action: 'newTab', url: 'https://example.com/slow' });
  await immediate.tick(0);
  assert.equal(opened.settled, true, 'immediate mode must not wait');
  assert.deepEqual(await opened.result, { ok: true, tabId: 9 });
  immediate.assertClean();

  // Delayed navigation: wait resolves on the destination 'complete', not the initial about:blank.
  const delayed = harness((h, url) => h.update({ status: 'loading', url }));
  const delaying = start(delayed, { action: 'newTab', url: 'https://example.com/next', wait: true });
  await delayed.tick(1000);
  assert.equal(delaying.settled, false, 'waits for the load');
  delayed.update({ status: 'complete', title: 'Next' });
  await delayed.tick(0);
  assert.equal(delaying.settled, true);
  assert.deepEqual(await delaying.result, {
    ok: true, tabId: 9, url: 'https://example.com/next', status: 'complete',
  });
  delayed.assertClean();

  // Redirects report the actual URL the tab ended on.
  const redirect = harness((h, url) => h.update({ status: 'loading', url }));
  const redirecting = start(redirect, { action: 'newTab', url: 'https://example.com/old', wait: true });
  await redirect.tick(50);
  redirect.update({ url: 'https://example.com/new' });
  redirect.update({ status: 'complete' });
  await redirect.tick(0);
  assert.deepEqual(await redirecting.result, {
    ok: true, tabId: 9, url: 'https://example.com/new', status: 'complete',
  });
  redirect.assertClean();

  // A 'complete' fired before tabs.create resolves is caught by polling.
  const fast = harness((h, url) => {
    h.update({ status: 'loading', url });
    h.update({ status: 'complete' });
  });
  const fastOpen = start(fast, { action: 'newTab', url: 'https://example.com/fast', wait: true });
  await fast.tick(199);
  assert.equal(fastOpen.settled, false);
  await fast.tick(1);
  assert.equal(fastOpen.settled, true, 'fast complete must not be missed');
  assert.deepEqual(await fastOpen.result, {
    ok: true, tabId: 9, url: 'https://example.com/fast', status: 'complete',
  });
  fast.assertClean();

  // A load that never completes times out at the requested bound and leaves the tab open.
  const stuck = harness((h, url) => h.update({ status: 'loading', url }));
  const hanging = start(stuck, {
    action: 'newTab', url: 'https://example.com/slow', wait: true, timeout: 3000,
  });
  await stuck.tick(2999);
  assert.equal(hanging.settled, false);
  await stuck.tick(1);
  assert.equal(hanging.settled, true);
  assert.deepEqual(await hanging.result, {
    ok: false, timedOut: true, tabId: 9, url: 'https://example.com/slow', status: 'loading',
  });
  stuck.assertClean();

  // about: targets have no load to wait for.
  const blank = harness();
  const blankOpen = start(blank, { action: 'newTab', wait: true });
  await blank.tick(0);
  assert.equal(blankOpen.settled, true);
  assert.deepEqual(await blankOpen.result, { ok: true, tabId: 9, url: 'about:blank', status: 'complete' });
  blank.assertClean();

  // A tab closed while loading is an error, not a hang.
  const closing = harness((h, url) => h.update({ status: 'loading', url }));
  const closed = start(closing, { action: 'newTab', url: 'https://example.com/gone', wait: true });
  await closing.tick(50);
  closing.remove();
  await closing.tick(200);
  assert.equal(closed.settled, true);
  assert.deepEqual(await closed.result, { error: 'Invalid tab ID: 9' });
  closing.assertClean();

  // #360: while Zen's first-run welcome screen hides the tab area, a loaded tab keeps reporting
  // about:blank (its title is the page's); the page itself reports where it is.
  let shown = ['about:blank', 'complete'];
  const stale = harness(h => h.update({ status: 'loading' }), () => shown);
  const staleOpen = start(stale, { action: 'newTab', url: 'https://example.com/user', wait: true, timeout: 3000 });
  await stale.tick(200);
  shown = ['https://example.com/user', 'loading'];
  stale.update({ title: 'user' });
  await stale.tick(200);
  assert.equal(staleOpen.settled, false, 'a page still loading is not loaded');
  shown = ['https://example.com/user', 'complete'];
  stale.update({ status: 'complete' });
  await stale.tick(200);
  assert.equal(staleOpen.settled, true);
  assert.deepEqual(await staleOpen.result, {
    ok: true, tabId: 9, url: 'https://example.com/user', status: 'complete',
  });
  stale.assertClean();

  // A tab that really shows about:blank still times out, reports about:blank and gets the
  // stayed_blank hint (#366).
  const blankPage = harness(h => h.update({ status: 'loading' }), () => ['about:blank', 'complete']);
  const blankWait = start(blankPage, { action: 'newTab', url: 'https://example.com/none', wait: true, timeout: 1000 });
  await blankPage.tick(0);
  blankPage.update({ status: 'complete' });
  await blankPage.tick(1000);
  assert.equal(blankWait.settled, true);
  assert.deepEqual(withoutHint(await blankWait.result), {
    ok: false, timedOut: true, tabId: 9, url: 'about:blank', status: 'complete',
    state: { reason: 'stayed_blank', url: 'about:blank', status: 'complete' },
  });
  blankPage.assertClean();

  // #366: the tab is created but never starts loading and no content script answers; the timeout
  // says so instead of leaving an unexplained blank tab.
  const inert = harness();
  const inertWait = start(inert, { action: 'newTab', url: 'https://example.com/inert', wait: true, timeout: 1000 });
  await inert.tick(1000);
  assert.equal(inertWait.settled, true);
  assert.deepEqual(withoutHint(await inertWait.result), {
    ok: false, timedOut: true, tabId: 9, url: 'about:blank', status: 'complete',
    state: { reason: 'stayed_blank', url: 'about:blank', status: 'complete' },
  });
  inert.assertClean();

  // A load that timed out on a web URL is not blank, so it gets no hint.
  assert.equal((await hanging.result).hint, undefined);

  console.log('new_tab_wait: all tests passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
