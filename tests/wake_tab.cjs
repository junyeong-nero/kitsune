// Run with: node tests/wake_tab.cjs (no browser or npm dependencies).
// Issue #95: wakeTab reports ready only once the woken tab's page can be scripted.
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
const URL_ = 'https://example.com/doc';

// A discarded tab 7. onReload(h) runs inside tabs.reload. The page answers the content script
// once h.scriptable is true; until then injection fails as it does on Gecko's placeholder.
function harness(onReload = () => {}) {
  let now = 0, nextId = 0;
  const timers = new Map();
  const onUpdated = event(), onRemoved = event();
  const webRequest = { onBeforeRequest: event(), onHeadersReceived: event() };
  let tab = { id: 7, url: URL_, title: 'Doc', status: 'complete', discarded: true, active: false };
  const h = {
    reloads: 0,
    scriptable: false,
    onPing: () => {},
    send: command => context.handleCommand({ includeSnapshot: false, ...command })
      .then(result => JSON.parse(JSON.stringify(result))),
    update: changes => {
      tab = { ...tab, ...changes };
      onUpdated.emit(7, changes, { ...tab });
    },
    close: () => { tab = null; onRemoved.emit(7); },
    requestId: 0,
    // The reload's main-frame request starts, then its headers arrive.
    request: () => webRequest.onBeforeRequest.emit({ tabId: 7, requestId: ++h.requestId, url: URL_ }),
    response: statusCode => webRequest.onHeadersReceived.emit({ tabId: 7, requestId: h.requestId, url: URL_, statusCode }),
    async tick(ms) {
      const end = now + ms;
      await flush();
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        timers.delete(next[0]);
        now = next[1].at;
        next[1].fn();
        await flush();
      }
      now = end;
      await flush();
    },
    assertClean() {
      assert.equal(timers.size, 0, 'timer leaked');
    },
  };
  const browser = {
    tabs: {
      onCreated: event(), onUpdated, onRemoved,
      query: async () => tab ? [{ ...tab }] : [],
      get: async id => {
        if (!tab || id !== 7) throw new Error(`Invalid tab ID: ${id}`);
        return { ...tab };
      },
      reload: async () => { h.reloads++; await onReload(h); },
      sendMessage: async (id, msg) => {
        if (!h.scriptable) throw new Error('Could not establish connection. Receiving end does not exist.');
        h.onPing();
        return { ok: true, version: Infinity };
      },
      executeScript: async () => {
        if (!h.scriptable) throw new Error('Missing host permission for the tab');
      },
    },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
    webRequest,
  };
  const context = vm.createContext({
    browser, URL, console: { log() {}, error() {} },
    WebSocket: class { close() {} },
    Date: class extends Date { static now() { return now; } },
    setTimeout: (fn, delay = 0) => { timers.set(++nextId, { fn, at: now + delay }); return nextId; },
    clearTimeout: id => timers.delete(id),
    setInterval: () => { throw new Error('Unexpected interval'); },
    clearInterval: () => {},
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

// Gecko restores the discarded document first: complete at about:blank, then with the tab's URL and title.
const restorePlaceholder = h => {
  h.update({ discarded: false, url: 'about:blank' });
  h.update({ status: 'complete', url: URL_ });
};

// The reload's response replaces the placeholder: the tab reports loading, then complete.
const loadResponse = (h, statusCode, complete = {}) => {
  h.update({ status: 'loading' });
  h.request();
  h.response(statusCode);
  h.update({ status: 'complete', ...complete });
};

async function main() {
  // An alive tab is not reloaded.
  const alive = harness();
  alive.update({ discarded: false });
  alive.scriptable = true;
  assert.deepEqual(await alive.send({ action: 'wakeTab', tabId: 7 }), { ok: true, woken: false, discarded: false });
  assert.equal(alive.reloads, 0);

  // The placeholder is complete but unscriptable; ready waits for the reloaded page to answer.
  const waking = harness(restorePlaceholder);
  const woken = start(waking, { action: 'wakeTab', tabId: 7 });
  await waking.tick(300);
  assert.equal(woken.settled, false, 'the placeholder is not ready');
  // The reload's response arrives while the tab still reports the placeholder complete: the tab
  // has not reported loading since the wake, so the refused injection does not end the wait.
  waking.request();
  waking.response(200);
  await waking.tick(1000);
  assert.equal(woken.settled, false, 'the placeholder in the gap does not end the wait');
  // The new document's content script answers at document_start, before the load completes.
  waking.update({ status: 'loading' });
  waking.scriptable = true;
  await waking.tick(300);
  assert.equal(woken.settled, false, 'a loading page is not ready');
  waking.update({ status: 'complete' });
  await waking.tick(100);
  assert.equal(woken.settled, true);
  assert.deepEqual(await woken.result, { ok: true, woken: true, ready: true });
  assert.equal(waking.reloads, 1);
  waking.assertClean();

  // A placeholder that answers the content script is still not ready: a command sent to it is
  // lost when the reload replaces it ("Receiving end does not exist").
  const answering = harness(h => { restorePlaceholder(h); h.scriptable = true; });
  const early = start(answering, { action: 'wakeTab', tabId: 7 });
  await answering.tick(1000);
  assert.equal(early.settled, false, 'an answering placeholder is not ready');
  loadResponse(answering, 200);
  await answering.tick(100);
  assert.deepEqual(await early.result, { ok: true, woken: true, ready: true });
  answering.assertClean();
  const blank = harness(h => { h.update({ discarded: false, url: 'about:blank' }); h.scriptable = true; });
  const blankWake = start(blank, { action: 'wakeTab', tabId: 7 });
  await blank.tick(1000);
  assert.equal(blankWake.settled, false, 'an answering about:blank placeholder is not ready');
  blank.update({ url: URL_ });
  loadResponse(blank, 200);
  await blank.tick(100);
  assert.deepEqual(await blankWake.result, { ok: true, woken: true, ready: true });
  blank.assertClean();

  // A page that answers while a new load starts is not ready until that load completes, even if
  // the tab reports complete again before the check ends.
  const reloading = harness(h => { restorePlaceholder(h); loadResponse(h, 200); h.scriptable = true; });
  reloading.onPing = () => {
    reloading.onPing = () => {};
    reloading.update({ status: 'loading' });
    reloading.update({ status: 'complete' });
    reloading.update({ status: 'loading' });
  };
  const second = start(reloading, { action: 'wakeTab', tabId: 7 });
  await reloading.tick(300);
  assert.equal(second.settled, false, 'a new load started during the check');
  reloading.update({ status: 'complete' });
  await reloading.tick(100);
  assert.deepEqual(await second.result, { ok: true, woken: true, ready: true });
  reloading.assertClean();

  // A reloaded page that finished loading and refuses scripts (a PDF viewer) ends the wait at
  // once, without a timeout. Reads report navigating while the reload settles, then error_page.
  const viewer = harness(h => { restorePlaceholder(h); loadResponse(h, 200, { title: 'doc.pdf' }); });
  const viewing = start(viewer, { action: 'wakeTab', tabId: 7 });
  await viewer.tick(0);
  assert.equal(viewing.settled, true, 'an unscriptable page does not wait for the timeout');
  assert.deepEqual(await viewing.result, { ok: true, woken: true, ready: false, unscriptable: true,
    state: { tabId: 7, url: URL_, status: 'complete', title: 'doc.pdf', httpStatus: 200 } });
  const read = async () => {
    const result = await viewer.send({ action: 'getSnapshot', tabId: 7 });
    return [result.code, result.retryable, result.state.httpStatus];
  };
  assert.deepEqual(await read(), ['navigating', true, 200]);
  await viewer.tick(5000);
  assert.deepEqual(await read(), ['error_page', false, 200]);
  viewer.assertClean();

  // webRequest events can arrive after the tabs events of the same load: the early exit waits
  // for the status of a request since the wake (a pre-discard 404 does not count).
  const late = harness(h => {
    restorePlaceholder(h);
    h.update({ status: 'loading' });
    h.update({ status: 'complete' });
  });
  late.request();
  late.response(404);
  await late.tick(10);
  const lateWake = start(late, { action: 'wakeTab', tabId: 7 });
  await late.tick(50);
  assert.equal(lateWake.settled, false, 'waiting for the fresh status');
  late.request();
  late.response(200);
  await late.tick(0);
  assert.deepEqual(await lateWake.result, { ok: true, woken: true, ready: false, unscriptable: true,
    state: { tabId: 7, url: URL_, status: 'complete', title: 'Doc', httpStatus: 200 } });
  late.assertClean();

  // An empty 404 also ends the wait, and reads as empty text at once.
  const notFound = harness(h => { restorePlaceholder(h); loadResponse(h, 404); });
  const missing = start(notFound, { action: 'wakeTab', tabId: 7 });
  await notFound.tick(0);
  assert.deepEqual(await missing.result, { ok: true, woken: true, ready: false, unscriptable: true,
    state: { tabId: 7, url: URL_, status: 'complete', title: 'Doc', httpStatus: 404 } });
  assert.deepEqual(await notFound.send({ action: 'getPageText', tabId: 7 }),
    { text: '', empty: true, httpStatus: 404, tabId: 7, url: URL_, title: 'Doc' });
  notFound.assertClean();

  // A placeholder that is never replaced times out, reporting the last state.
  const stuck = harness(restorePlaceholder);
  const hanging = start(stuck, { action: 'wakeTab', tabId: 7 });
  await stuck.tick(14999);
  assert.equal(hanging.settled, false);
  await stuck.tick(1);
  assert.equal(hanging.settled, true);
  assert.deepEqual(await hanging.result, { ok: true, woken: true, ready: false, timedOut: true,
    state: { tabId: 7, url: URL_, status: 'complete' } });
  stuck.assertClean();

  // A tab closed while waking reports the error.
  const closing = harness(restorePlaceholder);
  const closed = start(closing, { action: 'wakeTab', tabId: 7 });
  await closing.tick(300);
  closing.close();
  await closing.tick(100);
  assert.deepEqual(await closed.result, { error: 'Invalid tab ID: 7' });
  closing.assertClean();

  console.log('wake_tab: all tests passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
