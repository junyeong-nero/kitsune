// Run with: node tests/navigate_wait.cjs (no browser or npm dependencies).
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

// onNavigate(h, call) runs inside tabs.update/tabs.reload, before the API call resolves.
function harness(startUrl, onNavigate = () => {}) {
  let now = 0, nextId = 0;
  const timers = new Map();
  const schedule = (fn, delay, interval = false) => {
    const id = ++nextId;
    timers.set(id, { fn, at: now + delay, interval, delay });
    return id;
  };
  const onUpdated = event(), onRemoved = event();
  let tab = { id: 7, url: startUrl, title: 'Start', status: 'complete', discarded: false };
  const calls = [];
  const h = {
    calls,
    listenersAtNavigate: null,
    send: command => context.handleCommand({ includeSnapshot: false, ...command })
      .then(result => JSON.parse(JSON.stringify(result))),
    update: changes => {
      tab = { ...tab, ...changes };
      onUpdated.emit(7, changes, { ...tab });
    },
    silentUpdate: changes => { tab = { ...tab, ...changes }; },
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
  const navigate = async call => {
    calls.push(call);
    h.listenersAtNavigate = onUpdated.listeners.size;
    await onNavigate(h, call);
    return { ...tab };
  };
  const browser = {
    tabs: {
      onCreated: event(), onUpdated, onRemoved,
      query: async () => [{ ...tab }],
      get: async id => { assert.equal(id, 7); return { ...tab }; },
      update: async (id, props) => navigate({ method: 'update', id, ...props }),
      reload: async id => navigate({ method: 'reload', id }),
      sendMessage: async () => { throw new Error('Unexpected content message'); },
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

async function main() {
  // Hash-only and identical-with-fragment targets are same-document: no load to wait for.
  for (const [from, to] of [
    ['https://example.com/page', 'https://example.com/page#x'],
    ['https://example.com/page#a', 'https://example.com/page#b'],
    ['https://example.com/page#x', 'https://example.com/page#x'],
  ]) {
    const h = harness(from, (h, call) => h.silentUpdate({ url: call.url }));
    const pending = start(h, { action: 'navigate', url: to });
    await h.tick(0);
    assert.equal(pending.settled, true, `same-document navigation to ${to} must not wait`);
    assert.deepEqual(await pending.result, { ok: true, url: to });
    h.assertClean();
  }

  // A 'complete' fired before tabs.update resolves is still seen.
  const fast = harness('https://example.com/start', (h, call) => {
    h.update({ status: 'loading', url: call.url });
    h.update({ status: 'complete' });
  });
  const fastNav = start(fast, { action: 'navigate', url: 'https://example.com/next' });
  await fast.tick(0);
  assert.equal(fast.listenersAtNavigate, 2, 'listener registered before tabs.update');
  assert.equal(fastNav.settled, true, 'fast complete must not be missed');
  assert.deepEqual(await fastNav.result, { ok: true, url: 'https://example.com/next' });
  fast.assertClean();

  // Same URL without a fragment is a reload; Gecko may emit no fresh 'complete'.
  for (const [from, to] of [
    ['https://example.com/frames', 'https://example.com/frames'],
    ['https://example.com/frames#x', 'https://example.com/frames'],
  ]) {
    const h = harness(from, (h, call) => h.silentUpdate({ url: call.url }));
    const pending = start(h, { action: 'navigate', url: to });
    await h.tick(199);
    assert.equal(pending.settled, false, 'reload waits for its load');
    await h.tick(1);
    assert.equal(pending.settled, true, 'reload without a fresh complete must not hang');
    assert.deepEqual(await pending.result, { ok: true, url: to });
    h.assertClean();
  }

  // A reload still loading at the first poll waits for 'complete' even without an event.
  const slowReload = harness('https://example.com/frames', h => h.update({ status: 'loading' }));
  const reloading = start(slowReload, { action: 'navigate', url: 'https://example.com/frames' });
  await slowReload.tick(1000);
  assert.equal(reloading.settled, false);
  slowReload.silentUpdate({ status: 'complete' });
  await slowReload.tick(200);
  assert.equal(reloading.settled, true);
  assert.deepEqual(await reloading.result, { ok: true, url: 'https://example.com/frames' });
  slowReload.assertClean();

  // A different URL ignores the stale 'complete' of the old document, but accepts a
  // polled loading -> complete sequence.
  const polled = harness('https://example.com/start');
  const navigating = start(polled, { action: 'navigate', url: 'https://example.com/next' });
  await polled.tick(1000);
  assert.equal(navigating.settled, false, 'stale complete before loading must not count');
  polled.silentUpdate({ status: 'loading', url: 'https://example.com/next' });
  await polled.tick(200);
  polled.silentUpdate({ status: 'complete' });
  await polled.tick(200);
  assert.equal(navigating.settled, true);
  assert.deepEqual(await navigating.result, { ok: true, url: 'https://example.com/next' });
  polled.assertClean();

  // Transient about:blank completes are not the destination.
  const blank = harness('https://example.com/start', (h, call) => {
    h.update({ status: 'loading', url: 'about:blank' });
    h.update({ status: 'complete' });
    h.update({ status: 'loading', url: call.url });
  });
  const blanking = start(blank, { action: 'navigate', url: 'https://example.com/next' });
  await blank.tick(50);
  assert.equal(blanking.settled, false);
  blank.update({ status: 'complete' });
  await blank.tick(0);
  assert.equal(blanking.settled, true);
  assert.deepEqual(await blanking.result, { ok: true, url: 'https://example.com/next' });
  blank.assertClean();

  // A load that never completes is reported as timed out, not ok (and gets no snapshot).
  const stuck = harness('https://example.com/start', h => h.update({ status: 'loading' }));
  const hanging = start(stuck, {
    action: 'navigate', url: 'https://example.com/slow', expectTitle: 'Slow', includeSnapshot: true,
  });
  await stuck.tick(14999);
  assert.equal(hanging.settled, false);
  await stuck.tick(1);
  assert.equal(hanging.settled, true);
  assert.deepEqual(await hanging.result, {
    ok: false, timedOut: true, url: 'https://example.com/slow', title: 'Start',
    warning: 'Title "Start" does not contain expected "Slow" — possible redirect', redirected: true,
  });
  stuck.assertClean();

  const failing = harness('https://example.com/start', () => { throw new Error('Illegal URL'); });
  const failed = start(failing, { action: 'navigate', url: 'https://example.com/next' });
  await failing.tick(0);
  assert.equal(failed.settled, true);
  assert.deepEqual(await failed.result, { error: 'Illegal URL' });
  failing.assertClean();

  console.log('navigate_wait: all tests passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
