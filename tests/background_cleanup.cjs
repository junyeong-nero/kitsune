// Run with: node tests/background_cleanup.cjs (no browser or npm dependencies).
// Issue #194: navigation listeners, temporary activation, refs and vanished captures.
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

const copy = value => JSON.parse(JSON.stringify(value));
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  let now = 0, nextTimer = 0;
  const timers = new Map(), stored = {};
  const onUpdated = event(), onRemoved = event();
  const tabs = {
    1: { id: 1, windowId: 1, active: true, url: 'https://example.com/user' },
    2: { id: 2, windowId: 1, active: false, url: 'https://example.com/agent' },
  };
  const activations = [];
  const browser = {
    tabs: {
      onCreated: event(), onUpdated, onRemoved,
      get: async id => ({ ...tabs[id] }),
      query: async query => Object.values(tabs).filter(tab => tab.active && tab.windowId === query.windowId),
      update: async (id, props) => {
        if (props.active) {
          Object.values(tabs).forEach(tab => { tab.active = tab.id === id; });
          activations.push(id);
        }
        return { ...tabs[id] };
      },
      getZoom: async () => 1,
    },
    windows: { get: async id => ({ id, left: 0, top: 0, width: 1440, height: 900 }) },
    storage: { local: {
      get: async () => ({ ...stored }),
      set: async value => { Object.assign(stored, value); },
    } },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
  };
  const context = vm.createContext({
    browser, console: { log() {}, error() {} },
    WebSocket: class { close() {} },
    Date: class extends Date { static now() { return now; } },
    setTimeout: (fn, delay) => {
      const id = ++nextTimer;
      timers.set(id, { fn, at: now + delay });
      return id;
    },
    clearTimeout: id => timers.delete(id),
    setInterval: () => { throw new Error('Unexpected interval'); },
    clearInterval: () => {},
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  // Keep tests focused on background ownership/cleanup, not content injection or user activity.
  context.userActiveError = async () => null;
  return {
    context, timers, tabs, activations, onUpdated, onRemoved, stored,
    refs: () => vm.runInContext('({ short: shortRefs.size, public: publicRefs.size })', context),
    async compact(tabId, ref = 's123e0') {
      return copy(await context.compactContentRefs('find', { ref }, tabId, {})).ref;
    },
    async tick(ms) {
      const end = now + ms;
      await flush();
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.at <= end)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        const [id, timer] = next;
        now = timer.at;
        timers.delete(id);
        timer.fn();
        await flush();
      }
      now = end;
      await flush();
    },
  };
}

async function clickAndWaitNavigationCleanup() {
  for (const throws of [false, true]) {
    const h = harness();
    const listeners = h.onUpdated.listeners.size;
    h.context.forwardToContent = async (action, params) => {
      assert.equal(action, 'click');
      assert.equal(params.first, true);
      assert.equal(h.onUpdated.listeners.size, listeners + 1, 'wait starts before the click');
      if (throws) throw new Error('Click failed');
      return { error: 'No element matches: #gone', code: 'element_not_found' };
    };
    const result = copy(await h.context.clickAndWaitNavigation(2, '#gone', undefined, true));
    assert.deepEqual(result, throws ? { error: 'Click failed' }
      : { error: 'No element matches: #gone', code: 'element_not_found', clicked: false });
    assert.equal(h.onUpdated.listeners.size, listeners, 'failed click removes the listener immediately');
    assert.equal(h.timers.size, 0, 'failed click cancels the navigation timeout');
  }
  for (const completes of [true, false]) {
    const h = harness();
    const listeners = h.onUpdated.listeners.size;
    h.context.forwardToContent = async () => ({ ok: true });
    const pending = h.context.clickAndWaitNavigation(2, '#link', 1000);
    await flush();
    if (completes) h.onUpdated.emit(2, { status: 'complete' }, { url: 'https://example.com/end', title: 'End' });
    else await h.tick(1000);
    const result = copy(await pending);
    assert.equal(result.clicked, true);
    assert.deepEqual(result.navigation, completes
      ? { ok: true, url: 'https://example.com/end', title: 'End', elapsed: 0 }
      : { ok: false, timedOut: true });
    assert.equal(h.onUpdated.listeners.size, listeners);
    assert.equal(h.timers.size, 0);
  }
}

async function screenPointForClickRestoration() {
  for (const outcome of ['throws', 'error', 'success']) {
    const h = harness();
    h.context.forwardToContent = async action => {
      assert.equal(action, 'screenPoint');
      assert.equal(h.tabs[2].active, true);
      if (outcome === 'throws') throw new Error('Screen point failed');
      return outcome === 'error' ? { error: 'No target' } : { x: 12, y: 34 };
    };
    const pending = h.context.screenPointForClick({ tabId: 2, click: true });
    // Attach the rejection assertion before advancing the simulated paint delay.
    const result = outcome === 'throws' ? assert.rejects(pending, /Screen point failed/) : pending;
    await h.tick(150);
    if (outcome === 'throws') await result;
    else assert.deepEqual(copy(await result), outcome === 'error'
      ? { error: 'No target' }
      : { x: 12, y: 34, window: { left: 0, top: 0, width: 1440, height: 900 }, tabId: 2, previousTabId: 1 });
    assert.deepEqual(h.activations, outcome === 'success' ? [2] : [2, 1]);
    assert.equal(h.tabs[1].active, outcome !== 'success', 'only success leaves restoration to the bridge');
    assert.equal(h.timers.size, 0);
  }
  const h = harness();
  h.context.forwardToContent = async () => { throw new Error('Already active'); };
  await assert.rejects(h.context.screenPointForClick({ tabId: 1, click: true }), /Already active/);
  assert.deepEqual(h.activations, [], 'an already active target needs no restoration');
}

async function shortRefPruning() {
  for (const eventName of ['close', 'load']) {
    const h = harness();
    const old = await h.compact(1);
    const other = await h.compact(2);
    assert.deepEqual(copy(h.refs()), { short: 2, public: 2 });
    if (eventName === 'close') h.onRemoved.emit(1);
    else h.onUpdated.emit(1, { status: 'loading' }, h.tabs[1]);
    assert.deepEqual(copy(h.refs()), { short: 1, public: 1 }, 'both maps prune only the retired tab');
    const expired = copy(h.context.expandShortRefs({ selector: old }, 1)).error;
    assert.equal(expired.code, 'stale_ref');
    assert.equal(expired.state.reason, 'other_document', 'a retired alias still reports its document');
    assert.equal(expired.state.ref, old);
    assert.equal(h.context.expandShortRefs({ selector: other }, 2).params.selector, 's123e0');
    const fresh = await h.compact(1); // Even identical private refs/tab IDs must get new public aliases.
    assert.ok(BigInt(fresh.slice(1)) > BigInt(other.slice(1)));
    assert.equal(h.context.expandShortRefs({ selector: old }, 1).error.code, 'stale_ref');
    assert.deepEqual(copy(h.refs()), { short: 2, public: 2 });
  }
  const h = harness();
  const ref = await h.compact(1);
  h.onUpdated.emit(1, { url: 'https://example.com/user#section' }, h.tabs[1]);
  assert.equal(await h.compact(1), ref, 'same-document URL changes retain refs');
}

async function elementScreenshotVanishedTarget() {
  for (const missingAt of [1, 2, 3]) {
    const h = harness();
    const operations = [];
    let measures = 0;
    h.context.zlContent = async (tabId, action) => {
      operations.push(action);
      if (action === 'captureMeasure') {
        if (++measures === missingAt) return { missing: true };
        return { x: 0, y: 0, w: 100, h: 300, vw: 100, vh: 100, sy: 0 };
      }
      return { ok: true };
    };
    h.context.forwardToContent = async action => {
      operations.push(action);
      return { selector: '[data-capture]', attribute: 'data-capture' };
    };
    h.context.zlScroll = async () => {};
    h.context.loadDataUrl = async () => ({ width: 100, height: 100 });
    h.context.browser.tabs.captureTab = async () => 'data:image/png;base64,mock';
    h.context.OffscreenCanvas = class { getContext() { return { drawImage() {} }; } };
    const pending = h.context.elementScreenshot(2, 's123e0');
    await h.tick(1000);
    const result = copy(await pending);
    assert.deepEqual(result, { error: 'No element matches: [data-capture]' });
    assert.equal(measures, missingAt);
    assert.equal(operations.at(-1), missingAt === 3 ? 'restoreFixed' : 'unmarkScreenshotTarget');
    assert.ok(operations.includes('unmarkScreenshotTarget'), 'capture marker is removed on failure');
    if (missingAt > 1) assert.ok(operations.includes('captureScrollIntoView'));
    if (missingAt === 3) assert.ok(operations.includes('restoreFixed'), 'fixed elements are restored on failure');
  }
}

// Issue #207: a public short ref (eN) is marked in the page like a full ref, not used as CSS.
async function elementScreenshotShortRef() {
  const h = harness();
  const marked = [];
  h.context.forwardToContent = async (action, params) => {
    marked.push([action, params.selector]);
    return { error: 'stop' };
  };
  h.context.zlContent = async () => ({ missing: true });
  const pending = h.context.elementScreenshot(2, 'e5');
  await h.tick(1000);
  assert.deepEqual(copy(await pending), { error: 'stop' });
  assert.deepEqual(marked, [['markScreenshotTarget', 'e5']]);
}

async function main() {
  await clickAndWaitNavigationCleanup();
  await screenPointForClickRestoration();
  await shortRefPruning();
  await elementScreenshotVanishedTarget();
  await elementScreenshotShortRef();
  console.log('background cleanup: ok');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
