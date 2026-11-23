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

// onNavigate(h, call) runs inside tabs.update/tabs.reload, before the API call resolves. page(),
// when given, answers the content script's getPageInfo: [location.href, document.readyState], or
// a promise that never settles for a page that does not answer. documentStart(frameId) is the
// first message of a new document's content script, from the top frame unless frameId says otherwise.
function harness(startUrl, onNavigate = () => {}, page = null) {
  let now = 0, nextId = 0;
  const timers = new Map();
  const schedule = (fn, delay, interval = false) => {
    const id = ++nextId;
    timers.set(id, { fn, at: now + delay, interval, delay });
    return id;
  };
  const onUpdated = event(), onRemoved = event(), onMessage = event();
  const webRequest = { onBeforeRequest: event(), onHeadersReceived: event(), onCompleted: event(), onErrorOccurred: event() };
  let tab = { id: 7, url: startUrl, title: 'Start', status: 'complete', discarded: false };
  const calls = [];
  const h = {
    calls,
    pageInfoRequests: 0,
    listenersAtNavigate: null,
    send: command => context.handleCommand({ includeSnapshot: false, ...command })
      .then(result => JSON.parse(JSON.stringify(result))),
    update: changes => {
      tab = { ...tab, ...changes };
      onUpdated.emit(7, changes, { ...tab });
    },
    silentUpdate: changes => { tab = { ...tab, ...changes }; },
    documentStart: (frameId = 0) => onMessage.emit({ action: 'getDialogPolicy' }, { tab: { id: 7 }, frameId }),
    // The tab's main-frame request (#605): sent, then answered with a status (a redirect sends again).
    request: (requestId, url) => webRequest.onBeforeRequest.emit({ tabId: 7, requestId, url, type: 'main_frame' }),
    headers: (requestId, url, statusCode) =>
      webRequest.onHeadersReceived.emit({ tabId: 7, requestId, url, statusCode, type: 'main_frame' }),
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
      sendMessage: async (id, message) => {
        if (!page || message.action !== 'getPageInfo') throw new Error('Unexpected content message');
        h.pageInfoRequests++;
        const [url, readyState] = await page();
        return { url, readyState, title: tab.title };
      },
      executeScript: async () => { throw new Error('Unexpected content injection'); },
    },
    runtime: { onMessage, onStartup: event(), onInstalled: event() },
    webRequest,
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
  context.markAgentTab(7);
  return h;
}

// Tracks whether a command has resolved, so a hang fails an assertion instead of exiting silently.
function start(h, command) {
  const pending = { settled: false };
  pending.result = h.send(command).then(result => { pending.settled = true; return result; });
  return pending;
}

const PAGE_URL_TIMEOUT_MS = 500;

function assertStalled(result, url) {
  const { code, retryable, hint, ...state } = result;
  assert.deepEqual(state, { ok: false, timedOut: true, url });
  assert.equal(code, 'navigation_stalled');
  assert.equal(retryable, false);
  assert.match(hint, /about:blank/);
  assert.match(hint, /browser_status\(diagnostics=true\)/);
  assert.match(hint, /Retrying will not fix this/);
  assert.match(hint, /update, a restart or another fix/);
  assert.match(hint, /save their work and restart or update/);
  assert.match(hint, /not a confirmed diagnosis/);
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

  // #605: a timed-out navigation says what it still waits for: the server's answer, or the page
  // after one (a redirect waits for an answer again). Nothing when no request was seen (above).
  const waitingCases = [
    ['response', h => h.request('r1', 'https://example.com/slow'), undefined],
    ['page', h => { h.request('r1', 'https://example.com/slow'); h.headers('r1', 'https://example.com/slow', 200); },
      '(HTTP 200)'],
    ['response', h => {
      h.request('r1', 'https://example.com/slow'); h.headers('r1', 'https://example.com/slow', 302);
      h.request('r1', 'https://example.com/after-redirect');
    }, undefined],
  ];
  for (const [waitingFor, requests, status] of waitingCases) {
    const waiting = harness('https://example.com/start', h => { h.update({ status: 'loading' }); requests(h); });
    const waitingNav = start(waiting, { action: 'navigate', url: 'https://example.com/slow' });
    await waiting.tick(15000);
    assert.equal(waitingNav.settled, true);
    const result = await waitingNav.result;
    assert.deepEqual({ ok: result.ok, timedOut: result.timedOut, waiting_for: result.waiting_for },
      { ok: false, timedOut: true, waiting_for: waitingFor });
    if (status) assert.ok(result.hint.includes(status), result.hint);
    else assert.ok(result.hint.includes('has not answered'), result.hint);
    waiting.assertClean();
  }
  // A request seen before the navigation began is not this navigation's.
  const earlier = harness('https://example.com/start', h => h.update({ status: 'loading' }));
  earlier.request('r0', 'https://example.com/start');
  const earlierNav = start(earlier, { action: 'navigate', url: 'https://example.com/slow' });
  await earlier.tick(15000);
  assert.deepEqual(await earlierNav.result, { ok: false, timedOut: true, url: 'https://example.com/slow' });
  earlier.assertClean();

  // #561: an image-heavy page parses its DOM long before it finishes loading. Its document started
  // after the navigation and can be read: ok with load_pending, once it has been parsed for
  // ACTION_LOAD_GRACE_MS (#601), not at the 15 s deadline.
  let heavyPage = ['https://example.com/start', 'complete'];
  const heavy = harness('https://example.com/start', h => h.update({ status: 'loading' }), () => heavyPage);
  const heavyNav = start(heavy, { action: 'navigate', url: 'https://example.com/heavy' });
  await heavy.tick(2000);
  heavy.documentStart();
  heavy.update({ url: 'https://example.com/heavy' });
  heavyPage = ['https://example.com/heavy', 'interactive'];
  await heavy.tick(400);
  assert.equal(heavyNav.settled, false, 'a parsed DOM gets a grace period for its load');
  await heavy.tick(600);
  assert.equal(heavyNav.settled, true, 'a parsed DOM ends the load wait after the grace period');
  assert.deepEqual(await heavyNav.result, { ok: true, url: 'https://example.com/heavy', load_pending: true });
  assert.equal(heavy.pageInfoRequests, 2, 'asked once until parsed, once for the result');
  heavy.assertClean();

  // A parsed page whose load completes within the grace period is loaded, with no flag.
  let quickPage = ['https://example.com/start', 'complete'];
  const quick = harness('https://example.com/start', h => h.update({ status: 'loading' }), () => quickPage);
  const quickNav = start(quick, { action: 'navigate', url: 'https://example.com/quick' });
  await quick.tick(200);
  quick.documentStart();
  quickPage = ['https://example.com/quick', 'interactive'];
  await quick.tick(300);
  quickPage = ['https://example.com/quick', 'complete'];
  quick.update({ status: 'complete', url: 'https://example.com/quick' });
  await quick.tick(0);
  assert.equal(quickNav.settled, true);
  assert.deepEqual(await quickNav.result, { ok: true, url: 'https://example.com/quick' });
  quick.assertClean();

  // The document being navigated away from answers until the new one starts: its parsed DOM is
  // not the destination's. A frame's content script starting in it is not a new document either.
  for (const readyState of ['interactive', 'complete']) {
    const old = harness('https://example.com/start', h => h.update({ status: 'loading' }),
      () => ['https://example.com/start', readyState]);
    const oldNav = start(old, { action: 'navigate', url: 'https://example.com/unanswered' });
    await old.tick(2000);
    old.documentStart(3);
    await old.tick(13000);
    assert.equal(oldNav.settled, true);
    assert.deepEqual(await oldNav.result, { ok: false, timedOut: true, url: 'https://example.com/unanswered' });
    assert.equal(old.pageInfoRequests, 0, 'the old document is not asked');
    old.assertClean();
  }

  // A new document whose DOM is not parsed by the deadline (a blocking script, a stalled response
  // body) cannot be read yet: still a timeout.
  const parsing = harness('https://example.com/start', h => h.update({ status: 'loading' }),
    () => ['https://example.com/blocked', 'loading']);
  const parsingNav = start(parsing, { action: 'navigate', url: 'https://example.com/blocked' });
  await parsing.tick(2000);
  parsing.documentStart();
  parsing.update({ url: 'https://example.com/blocked' });
  await parsing.tick(13000);
  assert.equal(parsingNav.settled, true);
  assert.deepEqual(await parsingNav.result, { ok: false, timedOut: true, url: 'https://example.com/blocked' });
  parsing.assertClean();

  const failing = harness('https://example.com/start', () => { throw new Error('Illegal URL'); });
  const failed = start(failing, { action: 'navigate', url: 'https://example.com/next' });
  await failing.tick(0);
  assert.equal(failed.settled, true);
  assert.deepEqual(await failed.result, { error: 'Illegal URL' });
  failing.assertClean();

  // #360: while Zen's first-run welcome screen hides the tab area, the tab's url stays about:blank
  // after the load; the navigation counts once the page itself reports it loaded.
  let shown = ['about:blank', 'complete'];
  const stale = harness('about:blank', h => h.update({ status: 'loading' }), () => shown);
  const staleNav = start(stale, { action: 'navigate', url: 'https://example.com/user', timeout: 3000 });
  await stale.tick(200);
  stale.update({ status: 'complete', title: 'user' });
  await stale.tick(400);
  assert.equal(staleNav.settled, false, 'the page still reports about:blank');
  shown = ['https://example.com/user', 'complete'];
  await stale.tick(200);
  assert.equal(staleNav.settled, true);
  assert.deepEqual(await staleNav.result, { ok: true, url: 'https://example.com/user' });
  stale.assertClean();

  // A page that does not answer is asked once at a time, not once per poll.
  const silent = harness('about:blank', h => h.update({ status: 'loading' }), () => new Promise(() => {}));
  const silentNav = start(silent, { action: 'navigate', url: 'https://example.com/user' });
  await silent.tick(0);
  silent.update({ status: 'complete' });
  await silent.tick(1000);
  assert.equal(silentNav.settled, false);
  assert.equal(silent.pageInfoRequests, 2, 'polls at 200 and 800 ms; the first waits 500 ms for its answer');
  await silent.tick(14000);
  assert.equal(silentNav.settled, false, 'the timeout asks the page once more');
  await silent.tick(PAGE_URL_TIMEOUT_MS); // that request's and the last poll's own bound
  assert.equal(silentNav.settled, true);
  assertStalled(await silentNav.result, 'https://example.com/user');
  silent.assertClean();

  // #366: after an on-disk browser update, an old process's blank tab ignores navigation too:
  // complete/about:blank, no loading event, no content-script receiver.
  const inert = harness('about:blank');
  const inertNav = start(inert, { action: 'navigate', url: 'https://example.com/inert' });
  await inert.tick(14999);
  assert.equal(inertNav.settled, false, 'allow the load time before diagnosing');
  await inert.tick(1);
  assert.equal(inertNav.settled, true);
  assertStalled(await inertNav.result, 'https://example.com/inert');
  assert.deepEqual(inert.calls, [{ method: 'update', id: 7, url: 'https://example.com/inert' }],
    'navigated once, no retry');
  inert.assertClean();

  console.log('navigate_wait: all tests passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
