// Run with: node tests/wait_for_navigation.cjs (no browser or npm dependencies).
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

const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

function harness() {
  let now = 0, nextId = 0;
  const timers = new Map();
  const schedule = (fn, delay, interval = false) => {
    const id = ++nextId;
    timers.set(id, { fn, at: now + delay, interval, delay });
    return id;
  };
  const onUpdated = event(), onRemoved = event();
  let tab = { id: 7, url: 'https://example.com/start', title: 'Start' };
  let get = async () => ({ ...tab });
  let gets = 0;
  const browser = {
    tabs: {
      onCreated: event(), onUpdated, onRemoved,
      query: async () => tab ? [{ ...tab }] : [],
      get: async id => { assert.equal(id, 7); gets++; return get(); },
      sendMessage: async () => { throw new Error('Could not establish connection. Receiving end does not exist.'); },
      executeScript: async () => { throw new Error('Must not inject content for metadata waits'); },
    },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
  };
  const context = vm.createContext({
    browser, console: { log() {}, error() {} },
    WebSocket: class { close() {} },
    Date: class extends Date { static now() { return now; } },
    setTimeout: (fn, delay) => schedule(fn, delay),
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => schedule(fn, delay, true),
    clearInterval: id => timers.delete(id),
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  return {
    send: command => context.handleCommand(command).then(result => JSON.parse(JSON.stringify(result))),
    update: changes => {
      tab = { ...tab, ...changes };
      onUpdated.emit(7, changes, { ...tab });
    },
    silentUpdate: changes => { tab = { ...tab, ...changes }; },
    otherUpdate: changes => onUpdated.emit(8, changes, { id: 8, ...changes }),
    close: () => onRemoved.emit(7),
    noActiveTab: () => { tab = null; },
    setGet: fn => { get = fn; },
    get gets() { return gets; },
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
      assert.equal(onUpdated.listeners.size, 0, 'onUpdated leaked');
      assert.equal(onRemoved.listeners.size, 2, 'wait onRemoved leaked (dialog policy and opened-tab listeners remain)');
      assert.equal(timers.size, 0, 'timer leaked');
    },
  };
}

async function main() {
  for (const [action, property, pattern, destination] of [
    ['waitForUrl', 'url', '/search/goods\\?', 'https://example.com/search/goods?q=coat'],
    ['waitForTitle', 'title', '^Search results$', 'Search results'],
  ]) {
    const h = harness();
    // Content messaging is unavailable, as when navigation destroys the document.
    const pending = h.send({ action, pattern, timeout: 1000 });
    await flush();
    assert.equal(h.gets, 1, 'initial tabs.get check');
    h.otherUpdate({ [property]: destination });
    await h.tick(50);
    h.update({ status: 'loading', url: 'https://example.com/redirect', title: 'Loading' });
    await h.tick(50);
    h.update({ [property]: destination });
    assert.deepEqual(await pending, { ok: true, [property]: destination, elapsed: 100 });
    h.assertClean();
  }

  for (const [action, property, value] of [
    ['waitForUrl', 'url', 'https://example.com/start'], ['waitForTitle', 'title', 'Start'],
  ]) {
    const h = harness();
    assert.deepEqual(await h.send({ action, tabId: 7, pattern: 'Start|start' }),
      { ok: true, [property]: value, elapsed: 0 });
    h.assertClean();
  }

  const polling = harness();
  const polled = polling.send({ action: 'waitForTitle', tabId: 7, pattern: 'Ready' });
  await flush();
  polling.silentUpdate({ title: 'Ready' });
  await polling.tick(200);
  assert.deepEqual(await polled, { ok: true, title: 'Ready', elapsed: 200 });
  assert.equal(polling.gets, 2, 'periodic tabs.get fallback');
  polling.assertClean();

  for (const [action, property] of [['waitForUrl', 'url'], ['waitForTitle', 'title']]) {
    const h = harness();
    let settled = false;
    const pending = h.send({ action, pattern: 'never', timeout: 450 }).then(result => {
      settled = true;
      return result;
    });
    await flush();
    h.update({ [property]: 'Latest value' });
    await h.tick(449);
    assert.equal(settled, false);
    await h.tick(1);
    assert.deepEqual(await pending, { ok: false, [property]: 'Latest value', error: 'Timed out' });
    h.assertClean();
  }

  const closing = harness();
  const closed = closing.send({ action: 'waitForUrl', tabId: 7, pattern: 'never' });
  await flush();
  closing.close();
  assert.match((await closed).error, /Tab closed while waiting for url/);
  closing.assertClean();

  const missing = harness();
  missing.setGet(async () => { throw new Error('Invalid tab ID: 7'); });
  assert.match((await missing.send({ action: 'waitForTitle', tabId: 7, pattern: 'x' })).error, /Invalid tab ID/);
  missing.assertClean();

  const invalid = harness();
  assert.match((await invalid.send({ action: 'waitForUrl', pattern: '[' })).error, /regular expression/i);
  assert.match((await invalid.send({ action: 'waitForUrl', timeout: -1 })).error, /timeout/);
  invalid.noActiveTab();
  assert.match((await invalid.send({ action: 'waitForUrl', pattern: 'x' })).error, /No active tab/);
  invalid.assertClean();

  // A navigation event must not be overwritten by an older tabs.get result.
  const racing = harness();
  let resolveGet;
  racing.setGet(() => new Promise(resolve => { resolveGet = resolve; }));
  const pending = racing.send({ action: 'waitForUrl', pattern: 'never', timeout: 100 });
  await flush();
  racing.update({ url: 'https://example.com/new-document' });
  resolveGet({ url: 'https://example.com/old-document' });
  await racing.tick(100);
  assert.deepEqual(await pending, { ok: false, url: 'https://example.com/new-document', error: 'Timed out' });
  racing.assertClean();
  console.log('wait_for_navigation: all tests passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
