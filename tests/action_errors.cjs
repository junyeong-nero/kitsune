// Run with: node tests/action_errors.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {}

class Element extends EventTarget {
  constructor(tagName, id, style = {}) {
    super();
    Object.assign(this, { tagName, id, textContent: id, isConnected: true, hidden: false, rects: 1,
      style: { display: 'block', visibility: 'visible', ...style }, events: [] });
    for (const type of ['mousedown', 'mouseover', 'click', 'dblclick']) {
      this.addEventListener(type, event => this.events.push(event.type));
    }
  }
  getAttribute() { return null; }
  hasAttribute() { return false; }
  closest() { return null; }
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
  getClientRects() { return Array.from({ length: this.rects }, () => this.getBoundingClientRect()); }
  click() { this.dispatchEvent(new MouseEvent('click', { bubbles: true })); }
}

// Each call is a separate document with its own content script and ref generations.
function contentScript(elements) {
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, PointerEvent: MouseEvent, setTimeout,
    CSS: { escape: x => x },
    window: { getComputedStyle: el => el.style },
    document: {
      body: {},
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
      elementFromPoint: () => elements['#go'],
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  return msg => new Promise(resolve => listener(msg, {}, resolve))
    .then(result => JSON.parse(JSON.stringify(result)));
}

async function testContentErrors() {
  const go = new Element('BUTTON', 'go');
  const send = contentScript({ '#go': go });
  for (const msg of [
    { action: 'click', selector: '#missing' },
    { action: 'hover', selector: '#missing' },
    { action: 'type', selector: '#missing', text: 'x' },
    { action: 'fill', selector: '#missing', value: 'x' },
    { action: 'selectOption', selector: '#missing', value: 'x' },
    { action: 'checkBox', selector: '#missing', checked: true },
    { action: 'submitForm', selector: '#missing' },
    { action: 'drag', from: '#missing', to: '#go' },
  ]) {
    const result = await send(msg);
    assert.equal(result.code, 'not_found', msg.action);
    assert.equal(result.retryable, true, 'nothing was dispatched');
    assert.match(result.hint, /browser_snapshot/);
    assert.equal(result.error, msg.action === 'drag' ? 'from element not found' : 'Element not found',
      'error text is unchanged');
  }

  const refFor = async () => (await send({ action: 'find', query: '#go' })).results[0].ref;
  const stale = async (ref, action = 'click') => {
    const result = await send({ action, selector: ref });
    assert.equal(result.error, 'stale ref, call browser_snapshot again');
    assert.equal(result.code, 'stale_ref');
    assert.equal(result.retryable, true);
    assert.equal(result.state.ref, ref);
    return result;
  };

  // Observing the document again does not expire an attached node ref.
  const older = await refFor();
  await send({ action: 'getAccessibilityTree', depth: 6 });
  assert.equal((await send({ action: 'click', selector: older })).ok, true);

  // Dynamic forms: the element left the DOM after the snapshot.
  const current = await refFor();
  go.isConnected = false;
  go.events = [];
  assert.equal((await stale(current)).state.reason, 'element_removed');
  assert.equal((await stale(current, 'waitForElement')).state.reason, 'element_removed',
    'async handlers preserve the removal reason even after the node was pruned');
  assert.deepEqual(go.events, [], 'no events reach a removed element');
  go.isConnected = true;
  const generation = current.match(/^s(\d+)e/)[1];
  assert.equal((await stale(`s${generation}e999999`)).state.reason, 'not_in_snapshot');

  // Navigation or a focus change: the ref came from a different document.
  const otherDocument = contentScript({ '#go': new Element('BUTTON', 'go') });
  const foreign = (await otherDocument({ action: 'find', query: '#go' })).results[0].ref;
  go.events = [];
  const wrongTarget = await stale(foreign);
  assert.equal(wrongTarget.state.reason, 'other_document');
  assert.match(wrongTarget.hint, /tab_id/);
  assert.deepEqual(go.events, [], 'a foreign ref never acts on this document');
  assert.equal((await stale('r0')).state.reason, 'not_in_snapshot');
  const unobserved = await otherDocument({ action: 'click', selector: 'r0' });
  assert.equal(unobserved.state.latestObservation, undefined, 'no snapshot in this document yet');
  assert.equal(unobserved.hint, 'The latest snapshot did not issue this ref; take a new browser_snapshot and use its refs.');
}

async function testNotRenderedWarnings() {
  const go = new Element('BUTTON', 'go');
  const hidden = new Element('BUTTON', 'hidden', { display: 'none' });
  const invisible = new Element('BUTTON', 'invisible', { visibility: 'hidden' });
  const attribute = Object.assign(new Element('BUTTON', 'attribute'), { hidden: true });
  const insideHidden = Object.assign(new Element('INPUT', 'inside'), { rects: 0 });
  const tiny = Object.assign(new Element('INPUT', 'tiny'), {
    getBoundingClientRect: () => ({ x: 0, y: 0, width: 0, height: 0 }),
  });
  const send = contentScript({ '#go': go, '#hidden': hidden, '#invisible': invisible,
    '#attribute': attribute, '#inside': insideHidden, '#tiny': tiny });

  for (const el of [hidden, invisible, attribute, insideHidden]) {
    for (const [action, event] of [['click', 'click'], ['doubleClick', 'dblclick'], ['hover', 'mouseover']]) {
      el.events = [];
      const result = await send({ action, selector: `#${el.id}` });
      assert.equal(result.ok, true, `${action} still succeeds on #${el.id}`);
      assert.ok(el.events.includes(event), `${action} still dispatches to #${el.id}`);
      assert.deepEqual(result.warning.state, { reason: 'not_rendered' });
      assert.equal(result.warning.code, 'hidden_or_covered');
      assert.match(result.warning.hint, /label or visible control/);
    }
  }
  // Rendered targets (including zero-size custom-checkbox inputs) and coordinates get no warning.
  for (const msg of [
    { action: 'click', selector: '#go' }, { action: 'click', selector: '#tiny' },
    { action: 'click', coords: { x: 1, y: 1 } }, { action: 'doubleClick', selector: '#go' },
    { action: 'hover', selector: '#go' }, { action: 'hover', coords: { x: 1, y: 1 } },
  ]) {
    const result = await send(msg);
    assert.equal(result.ok, true);
    assert.equal(result.warning, undefined, JSON.stringify(msg));
  }
}

function event() {
  const listeners = new Set();
  return { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
    fire: (...args) => listeners.forEach(fn => fn(...args)) };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

// A fake setTimeout/clearTimeout and Date.now; tick(ms) runs the timers due within ms.
function fakeClock() {
  let now = 0, nextId = 0;
  const timers = new Map();
  return {
    timers,
    Date: class extends Date { static now() { return now; } },
    setTimeout: (fn, delay = 0) => { timers.set(++nextId, { fn, at: now + delay }); return nextId; },
    clearTimeout: id => { timers.delete(id); },
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
  };
}

function background(tabs, content, clock = { setTimeout, clearTimeout, Date }) {
  const sent = [];
  const injected = [];
  const pinged = [];
  let receiverMissing = false;
  let contentVersion = Infinity;
  let injectError = null;
  let requests = 0;
  const missing = id => new Error(`Invalid tab ID: ${id}`);
  const browser = {
    tabs: {
      onCreated: event(), onUpdated: event(), onRemoved: event(),
      query: async () => Object.values(tabs).filter(tab => tab.active).map(tab => ({ ...tab })),
      get: async id => { if (!tabs[id]) throw missing(id); return { ...tabs[id] }; },
      update: async id => { if (!tabs[id]) throw missing(id); return { ...tabs[id] }; },
      remove: async id => { if (!tabs[id]) throw missing(id); delete tabs[id]; browser.tabs.onRemoved.fire(id); },
      reload: async id => { if (!tabs[id]) throw missing(id); },
      sendMessage: async (id, msg) => {
        if (msg.action === 'ping') {
          pinged.push(id);
          if (injectError || receiverMissing) throw new Error('Could not establish connection. Receiving end does not exist.');
          return { ok: true, version: contentVersion };
        }
        // The user-activity probe only reads; sent records commands.
        if (msg.action === 'userActivity') return { active: false };
        sent.push({ tabId: id, ...msg });
        return content(id, msg);
      },
      executeScript: async id => {
        injected.push(id);
        if (injectError) throw new Error(injectError);
        receiverMissing = false;
      },
    },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
    webRequest: { onHeadersReceived: event(), onBeforeRequest: event() },
  };
  const context = vm.createContext({
    browser, URL, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, Date: clock.Date,
    setInterval, clearInterval,
    console: { log() {}, error() {} }, WebSocket: class { close() {} },
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  return {
    sent, injected, pinged,
    removeReceiver: () => { receiverMissing = true; },
    setContentVersion: version => { contentVersion = version; },
    statusWaiters: () => vm.runInContext('mainFrameStatusWaiters.size', context),
    failInjection: message => { injectError = message; },
    // A main-frame request starts; response() records its status.
    request: (tabId, url) => browser.webRequest.onBeforeRequest.fire({ tabId, requestId: ++requests, url, type: 'main_frame' }),
    response: (tabId, url, statusCode) =>
      browser.webRequest.onHeadersReceived.fire({ tabId, requestId: requests, url, statusCode, type: 'main_frame' }),
    tabUpdated: (tabId, changeInfo) => browser.tabs.onUpdated.fire(tabId, changeInfo, { ...tabs[tabId] }),
    send: command => context.handleCommand({ includeSnapshot: false, ...command })
      .then(result => JSON.parse(JSON.stringify(result))),
  };
}

const page = (id, extra = {}) => ({ id, url: `https://example.com/${id}`, title: `Tab ${id}`,
  status: 'complete', active: false, ...extra });

async function testBackgroundErrors() {
  const missingElement = { error: 'Element not found', code: 'not_found', retryable: true, hint: 'snapshot' };
  const staleRef = { error: 'stale ref, call browser_snapshot again', code: 'stale_ref', retryable: true,
    hint: 'snapshot', state: { reason: 'other_document', ref: 's1e0' } };
  let reply = () => ({ ok: true });
  const tabs = { 1: page(1, { active: true }), 2: page(2), 3: page(3, { url: 'about:preferences' }) };
  const h = background(tabs, (id, msg) => reply(id, msg));

  // The target tab is verified before anything reaches a page.
  assert.deepEqual(await h.send({ action: 'click', tabId: 9, selector: '#go' }), {
    error: 'Tab 9 no longer exists', code: 'tab_closed', retryable: true,
    hint: 'The tab no longer exists; list tabs with browser_tabs and target an open tab.', state: { tabId: 9 },
  });
  const restricted = await h.send({ action: 'fill', tabId: 3, selector: '#q', value: 'x' });
  assert.equal(restricted.error, 'Cannot access browser internal page: about:preferences');
  assert.equal(restricted.code, 'restricted_page');
  assert.deepEqual(h.sent, [], 'closed and restricted tabs receive no command');
  for (const action of ['navigate', 'switchTab', 'closeTab']) {
    const closed = await h.send({ action, tabId: 9, url: 'https://example.com/next' });
    assert.deepEqual([closed.error, closed.code, closed.state], ['Invalid tab ID: 9', 'tab_closed', { tabId: 9 }], action);
  }

  // Existing v40 content must be reinjected for the integrated protocol, current v42 need not.
  h.setContentVersion(40);
  assert.deepEqual(await h.send({ action: 'click', tabId: 2, selector: '#go' }), { ok: true });
  assert.deepEqual(h.injected, [2]);
  h.setContentVersion(42);
  assert.deepEqual(await h.send({ action: 'click', tabId: 2, selector: '#go' }), { ok: true });
  assert.deepEqual(h.injected, [2]);

  // Element errors gain the tab's current state.
  reply = () => missingElement;
  assert.deepEqual(await h.send({ action: 'click', tabId: 2, selector: '#go' }), {
    ...missingElement, state: { tabId: 2, url: 'https://example.com/2', status: 'complete' },
  });
  reply = () => staleRef;
  assert.deepEqual((await h.send({ action: 'click', tabId: 2, selector: 's1e0' })).state,
    { reason: 'other_document', ref: 's1e0', tabId: 2, url: 'https://example.com/2', status: 'complete' });

  // Delayed content: a target missing while the tab loads is a navigation race.
  tabs[2].status = 'loading';
  const replacedRef = { ...staleRef, state: { reason: 'snapshot_replaced', ref: 's1e0', currentRef: 's2e0' } };
  for (const [error, cause] of [[missingElement, 'not_found'], [staleRef, 'stale_ref'], [replacedRef, 'stale_ref']]) {
    reply = () => error;
    const racing = await h.send({ action: 'click', tabId: 2, selector: '#go' });
    assert.equal(racing.error, error.error, 'error text is unchanged');
    assert.equal(racing.code, 'navigating');
    assert.equal(racing.retryable, true);
    assert.match(racing.hint, /browser_wait_for/);
    assert.equal(racing.state.cause, cause);
    assert.equal(racing.state.status, 'loading');
    assert.equal(racing.state.currentRef, undefined, 'the loading document replaces a suggested ref');
  }

  // The page unloaded while handling the command: its outcome is unknown.
  reply = () => { throw new Error('Message manager disconnected'); };
  const unloaded = await h.send({ action: 'submitForm', tabId: 2, selector: '#form' });
  assert.deepEqual([unloaded.error, unloaded.code, unloaded.retryable],
    ['Content script error: Message manager disconnected', 'navigating', false]);
  tabs[2].status = 'complete';
  reply = () => undefined;
  assert.deepEqual(Object.values(await h.send({ action: 'click', tabId: 2, selector: '#go' })).slice(0, 3),
    ['No response from content script', 'content_unavailable', false]);
  reply = () => { throw new Error('Could not establish connection. Receiving end does not exist.'); };
  assert.equal((await h.send({ action: 'click', tabId: 2, selector: '#go' })).retryable, true,
    'a message without a receiver never reached the page');

  // Tab unloads and closes.
  reply = id => { delete tabs[id]; throw new Error('Tab closed'); };
  const closing = await h.send({ action: 'click', tabId: 2, selector: '#go' });
  assert.deepEqual([closing.code, closing.state], ['tab_closed', { tabId: 2 }]);
  tabs[4] = page(4, { discarded: true });
  h.failInjection('Missing host permission for the tab');
  const discarded = await h.send({ action: 'click', tabId: 4, selector: '#go' });
  assert.equal(discarded.error, 'Cannot inject content script: Missing host permission for the tab');
  assert.deepEqual([discarded.code, discarded.retryable, discarded.state.discarded], ['tab_unloaded', true, true]);
  assert.match(discarded.hint, /browser_wake_tab/);
}

// Issue #169: a successful readiness ping cannot guarantee the next message has a receiver.
async function testMissingReceiverRecovery() {
  const missing = 'Could not establish connection. Receiving end does not exist.';
  const tabs = { 1: page(1, { active: true }), 2: page(2) };
  const clock = fakeClock();
  let calls = 0, effects = 0;
  const h = background(tabs, () => {
    if (++calls === 1) {
      h.removeReceiver();
      tabs[1].active = false;
      tabs[2].active = true;
      throw new Error(missing);
    }
    effects++;
    return { ok: true };
  }, clock);
  const command = { action: 'click', selector: '#go', includeSnapshot: false };
  const recovered = h.send(command);
  await clock.tick(150);
  assert.deepEqual(await recovered, { ok: true });
  assert.deepEqual(h.pinged, [1, 1], 'readiness is checked again on the original tab');
  assert.deepEqual(h.injected, [1], 'the vanished receiver is reinjected');
  assert.deepEqual(h.sent, [command, command].map(msg => ({ tabId: 1, ...msg })),
    'the action and target are unchanged despite a focus change');
  assert.equal(effects, 1, 'only the delivered action has a side effect');
  assert.equal(clock.timers.size, 0);

  const bounded = background({ 1: page(1) }, () => { throw new Error(missing); });
  const unavailable = await bounded.send({ action: 'click', tabId: 1, selector: '#go' });
  assert.deepEqual([unavailable.code, unavailable.retryable], ['content_unavailable', true]);
  assert.equal(bounded.sent.length, 2, 'a second missing receiver ends recovery');
  assert.deepEqual(bounded.pinged, [1, 1]);

  // A closed port, an empty response or a page error may follow dispatch: never repeat them,
  // whether on the first attempt or after recovering from a missing receiver.
  for (const recoverFirst of [false, true]) {
    for (const reply of [
      () => { throw new Error('The message port closed before a response was received.'); },
      () => { throw new Error('Message manager disconnected'); },
      () => { throw new Error('Other failure: ' + missing); },
      () => undefined,
      () => ({ error: 'Element not found', code: 'not_found', retryable: true }),
    ]) {
      const h = background({ 1: page(1) }, () => {
        if (recoverFirst && h.sent.length === 1) throw new Error(missing);
        return reply();
      });
      const result = await h.send({ action: 'click', tabId: 1, selector: '#go' });
      assert.equal(h.sent.length, recoverFirst ? 2 : 1, 'no replay after possible dispatch');
      assert.equal(result.retryable, result.code === 'not_found');
    }
  }

  for (const change of ['closed', 'restricted', 'unloaded']) {
    const tabs = { 1: page(1) };
    const h = background(tabs, () => {
      if (change === 'closed') delete tabs[1];
      if (change === 'restricted') tabs[1].url = 'about:config';
      if (change === 'unloaded') {
        tabs[1].discarded = true;
        h.failInjection('Missing host permission for the tab');
      }
      throw new Error(missing);
    });
    const result = await h.send({ action: 'click', tabId: 1, selector: '#go' });
    assert.equal(result.code, { closed: 'tab_closed', restricted: 'restricted_page', unloaded: 'tab_unloaded' }[change]);
    assert.equal(h.sent.length, 1, 'recovery validates the tab and injection before resending');
  }
}

async function testEmptyErrorPage() {
  const tabs = { 1: page(1, { active: true }), 2: page(2, { url: 'http://127.0.0.1:8000/nf-empty', title: '' }),
    3: page(3, { url: 'https://example.com/blocked', title: 'Blocked' }), 4: page(4, { url: 'file:///tmp/x.html' }) };
  const clock = fakeClock();
  const h = background(tabs, () => ({ ok: true }), clock);
  // Without a recorded status, a failed injection waits for one; let that wait time out.
  const sendUnknown = async command => {
    const pending = h.send(command);
    await clock.tick(1000);
    return pending;
  };
  h.failInjection('Missing host permission for the tab');
  h.response(2, 'http://127.0.0.1:8000/nf-empty', 404);
  h.response(3, 'https://example.com/elsewhere', 500);

  // An empty 404: the browser shows its own document, so reads fail the same way until navigation.
  for (const action of ['getSnapshot', 'getPageInfo', 'getAccessibilityTree', 'click']) {
    const result = await h.send({ action, tabId: 2, selector: '#go' });
    assert.equal(result.code, 'error_page', action);
    assert.equal(result.retryable, false, 'retrying cannot succeed');
    assert.equal(result.error, 'The tab shows an HTTP 404 error response with no scriptable document. '
      + 'Cannot inject content script: Missing host permission for the tab');
    assert.deepEqual(result.state, { tabId: 2, url: 'http://127.0.0.1:8000/nf-empty', status: 'complete',
      title: '', httpStatus: 404 });
    assert.doesNotMatch(result.hint, /browser_snapshot/, 'a snapshot would fail the same way');
  }
  assert.deepEqual(h.sent, [], 'nothing reached the page');

  // Page text of an error response is empty, not an error; a selector cannot match.
  const empty = { text: '', empty: true, httpStatus: 404, tabId: 2, url: 'http://127.0.0.1:8000/nf-empty', title: '' };
  assert.deepEqual(await h.send({ action: 'getPageText', tabId: 2 }), empty);
  assert.deepEqual(await h.send({ action: 'getPageTextFromTab', tabId: 2 }), empty);
  assert.equal((await h.send({ action: 'getPageText', tabId: 2, selector: 'main' })).code, 'error_page');

  // Without an observed error status for this URL, the code stays but no status is claimed.
  for (const action of ['getPageText', 'getSnapshot']) {
    const unknown = await sendUnknown({ action, tabId: 3 });
    assert.deepEqual([unknown.code, unknown.retryable, unknown.state.httpStatus, unknown.state.title],
      ['error_page', false, undefined, 'Blocked'], action);
    assert.match(unknown.error, /^The tab shows a page with no scriptable document\. Cannot inject/);
  }

  // Only a loaded http(s) page is an error page.
  assert.equal((await h.send({ action: 'getSnapshot', tabId: 4 })).code, 'content_unavailable');
  tabs[2].status = 'loading';
  assert.equal((await h.send({ action: 'getSnapshot', tabId: 2 })).code, 'navigating');
  tabs[2].status = 'complete';

  // Statuses are per tab and forgotten when the tab closes.
  await h.send({ action: 'closeTab', tabId: 2, userTabApproved: true });
  tabs[2] = page(2, { url: 'http://127.0.0.1:8000/nf-empty' });
  assert.equal((await sendUnknown({ action: 'getSnapshot', tabId: 2 })).state.httpStatus, undefined);
  assert.equal(h.statusWaiters(), 0);
  assert.equal(clock.timers.size, 0);
}

// Issue #99: Firefox can report a tab complete before it delivers the response's webRequest
// events, so a failed injection waits a bounded time for the status of the tab's URL.
async function testErrorPageWaitsForLateStatus() {
  const url = 'http://127.0.0.1:8000/nf-empty';
  const tabs = { 1: page(1, { active: true }), 2: page(2, { url, title: '' }) };
  const clock = fakeClock();
  const h = background(tabs, () => ({ ok: true }), clock);
  h.failInjection('Missing host permission for the tab');
  const settled = promise => {
    const state = { done: false };
    promise.then(value => Object.assign(state, { done: true, value }));
    return state;
  };

  // The status arrives during the wait: the read resolves at once with it.
  const late = settled(h.send({ action: 'getPageText', tabId: 2 }));
  await clock.tick(10);
  assert.equal(late.done, false, 'waits for the status');
  h.response(2, url, 404);
  await clock.tick(0);
  assert.deepEqual(late.value, { text: '', empty: true, httpStatus: 404, tabId: 2, url, title: '' });
  assert.deepEqual([h.statusWaiters(), clock.timers.size], [0, 0], 'the waiter and its timer are gone');

  // A recorded status answers without waiting.
  const known = settled(h.send({ action: 'getSnapshot', tabId: 2 }));
  await clock.tick(0);
  assert.deepEqual([known.value.code, known.value.state.httpStatus], ['error_page', 404]);

  // A response for another URL of the tab does not end the wait; the timeout keeps today's error_page.
  tabs[2].url = 'http://127.0.0.1:8000/other';
  const mismatched = settled(h.send({ action: 'getPageText', tabId: 2 }));
  await clock.tick(10);
  h.response(2, 'http://127.0.0.1:8000/elsewhere', 500);
  await clock.tick(989);
  assert.equal(mismatched.done, false, 'still waiting just before the timeout');
  await clock.tick(1);
  assert.deepEqual([mismatched.value.code, mismatched.value.retryable, mismatched.value.state.httpStatus],
    ['error_page', false, undefined]);
  assert.match(mismatched.value.error, /^The tab shows a page with no scriptable document\./);
  assert.deepEqual([h.statusWaiters(), clock.timers.size], [0, 0]);

  // The tab closes during the wait: it reports tab_closed without waiting for the timeout.
  const closing = settled(h.send({ action: 'getPageText', tabId: 2 }));
  await clock.tick(10);
  await h.send({ action: 'closeTab', tabId: 2, userTabApproved: true });
  await clock.tick(0);
  assert.deepEqual([closing.value.code, closing.value.state], ['tab_closed', { tabId: 2 }]);
  assert.deepEqual([h.statusWaiters(), clock.timers.size], [0, 0]);
}

// Issue #95: a woken or reloaded tab shows its old document until the reload replaces it. Within
// RELOAD_SETTLE_MS a refused injection is navigating unless a response since then is an error;
// after it, a known status is final (PDF/JSON viewers answer 200), but a 2xx that only arrives
// while the status is awaited belongs to a document still being replaced.
async function testErrorPageClassification() {
  const url = 'http://127.0.0.1:8000/doc';
  const tabs = { 1: page(1, { active: true }), 2: page(2, { url, title: 'Doc' }) };
  const clock = fakeClock();
  const h = background(tabs, () => ({ ok: true }), clock);
  h.failInjection('Missing host permission for the tab');
  // Reads tab 2; `during` runs while a status wait is pending, which then resolves or times out.
  const read = async (during = () => {}) => {
    let result;
    h.send({ action: 'getSnapshot', tabId: 2 }).then(r => { result = r; });
    await clock.tick(10);
    during();
    await clock.tick(990);
    assert.ok(result, 'the read settles within the status wait');
    return [result.code, result.retryable, result.state.httpStatus];
  };
  const errorPage = status => ['error_page', false, status];
  const navigating = status => ['navigating', true, status];

  // Not reloaded: a known status is final.
  h.request(2, url);
  h.response(2, url, 200);
  assert.deepEqual(await read(), errorPage(200), 'known 2xx');
  h.response(2, url, 404);
  assert.deepEqual(await read(), errorPage(404), 'known 404');
  // A status arriving during the wait: a 2xx is a document still being replaced, an error is final.
  h.request(2, url);
  assert.deepEqual(await read(() => h.response(2, url, 200)), navigating(200), 'arriving 2xx');
  h.request(2, url);
  assert.deepEqual(await read(() => h.response(2, url, 404)), errorPage(404), 'arriving 404');
  // A new main-frame request replaces the last status; with none arriving, no status is claimed.
  h.response(2, url, 500);
  h.request(2, url);
  assert.deepEqual(await read(), errorPage(undefined), 'replaced by a new request');

  // A reload clears the previous document's status: its 200 must not make the old document final.
  h.request(2, url);
  h.response(2, url, 200);
  assert.deepEqual((await h.send({ action: 'reloadTabBrowser', tabId: 2 })).ok, true);
  assert.deepEqual(await read(), navigating(undefined), 'reloaded, no response yet');
  h.request(2, url);
  assert.deepEqual(await read(() => h.response(2, url, 200)), navigating(200), 'reloaded, arriving 2xx');
  assert.deepEqual(await read(), navigating(200), 'reloaded, known 2xx');
  h.request(2, url);
  assert.deepEqual(await read(() => h.response(2, url, 404)), errorPage(404), 'reloaded, arriving 404');
  assert.deepEqual(await read(), errorPage(404), 'reloaded, known 404');
  // The reload stops counting after RELOAD_SETTLE_MS, so the viewer's 200 becomes final.
  await h.send({ action: 'reloadTabBrowser', tabId: 2 });
  h.request(2, url);
  h.response(2, url, 200);
  assert.deepEqual(await read(), navigating(200));
  await clock.tick(4000);
  assert.deepEqual(await read(), errorPage(200), 'after the settle window');

  // A status recorded before a wake by the browser is not fresh, even if nothing cleared it.
  h.request(2, url);
  h.response(2, url, 404);
  await clock.tick(1);
  h.tabUpdated(2, { discarded: false });
  assert.deepEqual(await read(), navigating(undefined), 'woken by the browser, stale 404');
  // A discard clears the status.
  await clock.tick(5000);
  h.tabUpdated(2, { discarded: true });
  assert.deepEqual(await read(), errorPage(undefined), 'discarded, status cleared');
  assert.deepEqual([h.statusWaiters(), clock.timers.size], [0, 0]);
}

async function testFocusChangeKeepsTarget() {
  const tabs = { 1: page(1, { active: true }), 2: page(2) };
  const h = background(tabs, (id, msg) => {
    if (msg.action === 'click') {
      // The user switches tabs while the click runs.
      tabs[1].active = false;
      tabs[2].active = true;
      return { ok: true };
    }
    return { snapshot: `tab ${id}` };
  });
  const result = await h.send({ action: 'click', selector: '#go', includeSnapshot: true });
  assert.equal(result.snapshot, 'tab 1');
  assert.deepEqual(h.sent.map(msg => [msg.action, msg.tabId]), [['click', 1], ['getSnapshot', 1]]);
}

(async () => {
  await testContentErrors();
  await testNotRenderedWarnings();
  await testBackgroundErrors();
  await testMissingReceiverRecovery();
  await testEmptyErrorPage();
  await testErrorPageWaitsForLateStatus();
  await testErrorPageClassification();
  await testFocusChangeKeepsTarget();
  console.log('action_errors: all tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
