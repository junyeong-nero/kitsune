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

  // An older snapshot of this document.
  const older = await refFor();
  await send({ action: 'getAccessibilityTree', depth: 6 });
  const replaced = await stale(older);
  assert.equal(replaced.state.reason, 'snapshot_replaced');
  assert.match(replaced.hint, /latest browser_snapshot/);

  // Dynamic forms: the element left the DOM after the snapshot.
  const current = await refFor();
  go.isConnected = false;
  go.events = [];
  assert.equal((await stale(current)).state.reason, 'element_removed');
  assert.equal((await stale(current, 'waitForElement')).state.reason, 'element_removed',
    'async handlers keep the error details');
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
  assert.equal((await stale('r0')).state.reason, 'snapshot_replaced');
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

function background(tabs, content) {
  const sent = [];
  const injected = [];
  let injectError = null;
  const missing = id => new Error(`Invalid tab ID: ${id}`);
  const browser = {
    tabs: {
      onCreated: event(), onUpdated: event(), onRemoved: event(),
      query: async () => Object.values(tabs).filter(tab => tab.active).map(tab => ({ ...tab })),
      get: async id => { if (!tabs[id]) throw missing(id); return { ...tabs[id] }; },
      update: async id => { if (!tabs[id]) throw missing(id); return { ...tabs[id] }; },
      remove: async id => { if (!tabs[id]) throw missing(id); delete tabs[id]; browser.tabs.onRemoved.fire(id); },
      sendMessage: async (id, msg) => {
        if (msg.action === 'ping') {
          if (injectError) throw new Error('Could not establish connection. Receiving end does not exist.');
          return { ok: true, version: Infinity };
        }
        sent.push({ tabId: id, ...msg });
        return content(id, msg);
      },
      executeScript: async id => { injected.push(id); if (injectError) throw new Error(injectError); },
    },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
    webRequest: { onHeadersReceived: event(), onBeforeRequest: event() },
  };
  const context = vm.createContext({
    browser, URL, setTimeout, clearTimeout, setInterval, clearInterval,
    console: { log() {}, error() {} }, WebSocket: class { close() {} },
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  return {
    sent, injected,
    failInjection: message => { injectError = message; },
    response: (tabId, url, statusCode) =>
      browser.webRequest.onHeadersReceived.fire({ tabId, url, statusCode, type: 'main_frame' }),
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

  // Successful results are unchanged.
  assert.deepEqual(await h.send({ action: 'click', tabId: 2, selector: '#go' }), { ok: true });

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
  for (const [error, cause] of [[missingElement, 'not_found'], [staleRef, 'stale_ref']]) {
    reply = () => error;
    const racing = await h.send({ action: 'click', tabId: 2, selector: '#go' });
    assert.equal(racing.error, error.error, 'error text is unchanged');
    assert.equal(racing.code, 'navigating');
    assert.equal(racing.retryable, true);
    assert.match(racing.hint, /browser_wait_for/);
    assert.equal(racing.state.cause, cause);
    assert.equal(racing.state.status, 'loading');
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

async function testEmptyErrorPage() {
  const tabs = { 1: page(1, { active: true }), 2: page(2, { url: 'http://127.0.0.1:8000/nf-empty', title: '' }),
    3: page(3, { url: 'https://example.com/blocked', title: 'Blocked' }), 4: page(4, { url: 'file:///tmp/x.html' }) };
  const h = background(tabs, () => ({ ok: true }));
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
    const unknown = await h.send({ action, tabId: 3 });
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
  await h.send({ action: 'closeTab', tabId: 2 });
  tabs[2] = page(2, { url: 'http://127.0.0.1:8000/nf-empty' });
  assert.equal((await h.send({ action: 'getSnapshot', tabId: 2 })).state.httpStatus, undefined);
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
  await testEmptyErrorPage();
  await testFocusChangeKeepsTarget();
  console.log('action_errors: all tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
