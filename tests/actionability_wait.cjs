// Run with: node tests/actionability_wait.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {}
class KeyboardEvent extends Event {}

class Element extends EventTarget {
  constructor(tagName, id, props = {}) {
    super();
    const { style, ...rest } = props;
    Object.assign(this, { tagName, id, textContent: id, isConnected: true, hidden: false, rects: 1,
      value: '', style: { display: 'block', visibility: 'visible', ...style }, events: [] }, rest);
    for (const type of ['mousedown', 'mouseover', 'click', 'dblclick', 'keydown', 'input', 'change',
      'dragstart', 'drop']) {
      this.addEventListener(type, event => this.events.push(event.type));
    }
  }
  getAttribute() { return null; }
  hasAttribute() { return false; }
  closest() { return null; }
  focus() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
  getClientRects() { return Array.from({ length: this.rects }, () => this.getBoundingClientRect()); }
  click() { this.dispatchEvent(new MouseEvent('click', { bubbles: true })); }
}

const flush = () => new Promise(resolve => setImmediate(resolve));

// A document whose elements the test adds, renders and removes on a fake clock.
function page() {
  let now = 0, nextId = 0, listener;
  const timers = new Map();
  const elements = {};
  const body = new Element('BODY', 'body');
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, PointerEvent: MouseEvent, KeyboardEvent,
    DragEvent: MouseEvent, DataTransfer: class {},
    // Delivers on the microtask queue, so a pointer drag settles within one flush.
    MessageChannel: class {
      constructor() {
        this.port1 = { close() {} };
        this.port2 = { postMessage: () => queueMicrotask(() => this.port1.onmessage()) };
      }
    },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {}, CSS: { escape: x => x },
    Date: class extends Date { static now() { return now; } },
    setTimeout: (fn, delay) => { timers.set(++nextId, { fn, at: now + delay }); return nextId; },
    clearTimeout: id => timers.delete(id),
    window: { getComputedStyle: el => el.style },
    document: {
      body, activeElement: body,
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
      elementFromPoint: () => elements['#go'] || null,
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  return {
    body, elements, timers,
    add: (selector, el) => { elements[selector] = el; return el; },
    remove: selector => { elements[selector].isConnected = false; delete elements[selector]; },
    // Tracks settlement so a hang fails an assertion instead of exiting silently.
    start(msg) {
      const pending = { settled: false };
      pending.result = new Promise(resolve => listener(msg, {}, resolve))
        .then(result => { pending.settled = true; return JSON.parse(JSON.stringify(result)); });
      return pending;
    },
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

async function testImmediateModeIsUnchanged() {
  const p = page();
  const missing = p.start({ action: 'click', selector: '#later' });
  await p.tick(0);
  assert.equal(missing.settled, true, 'no wait without wait=true');
  assert.deepEqual(await missing.result, { error: 'Element not found', code: 'not_found', retryable: true,
    hint: 'No element matches; take a new browser_snapshot, or browser_wait_for the element if the page is still updating.' });
  assert.equal(p.timers.size, 0, 'immediate mode schedules nothing');

  // A hidden target is still clicked at once, with the #65 warning.
  const hidden = p.add('#hidden', new Element('BUTTON', 'hidden', { style: { display: 'none' } }));
  const warned = p.start({ action: 'click', selector: '#hidden' });
  await p.tick(0);
  const result = await warned.result;
  assert.equal(result.ok, true);
  assert.equal(result.warning.code, 'hidden_or_covered');
  assert.deepEqual(hidden.events, ['mousedown', 'click']);
}

// The same delayed content and dynamic form without wait: each action resolves its target once.
async function testImmediateModeOnDelayedContentAndDynamicForms() {
  const p = page();
  const actions = [
    [{ action: 'click', selector: '#go' }, 'Element not found'],
    [{ action: 'doubleClick', selector: '#go' }, 'Element not found'],
    [{ action: 'hover', selector: '#go' }, 'Element not found'],
    [{ action: 'type', selector: '#name', text: 'Ada' }, 'Element not found'],
    [{ action: 'fill', selector: '#email', value: 'a@b.c' }, 'Element not found'],
    [{ action: 'selectOption', selector: '#color', value: 'blue' }, 'Element not found'],
    [{ action: 'checkBox', selector: '#agree', checked: true }, 'Element not found'],
    [{ action: 'keypress', selector: '#name', key: 'Enter' }, 'No target'],
    [{ action: 'drag', from: '#from', to: '#to' }, 'from element not found'],
  ];
  for (const [msg, error] of actions) {
    // A timeout without wait is ignored.
    const pending = p.start({ ...msg, timeout: 5000 });
    await p.tick(0);
    assert.equal(pending.settled, true, `${msg.action} does not wait`);
    const result = await pending.result;
    assert.equal(result.error, error, msg.action);
    assert.equal(result.code, 'not_found');
    assert.equal(result.state, undefined, 'no wait state in immediate mode');
  }
  assert.equal(p.timers.size, 0, 'immediate mode schedules nothing');

  // The content arrives with its pointer target still hidden and the native controls
  // display:none behind custom widgets. Pointer actions dispatch anyway with the warning;
  // value actions need no rendered target and get none.
  const go = p.add('#go', new Element('BUTTON', 'go', { style: { display: 'none' } }));
  const name = p.add('#name', new Element('INPUT', 'name', { type: 'text' }));
  const email = p.add('#email', new Element('INPUT', 'email', { type: 'email' }));
  const color = p.add('#color', new Element('SELECT', 'color', { style: { display: 'none' },
    options: [{ value: 'red', text: 'Red' }, { value: 'blue', text: 'Blue' }] }));
  const agree = p.add('#agree', new Element('INPUT', 'agree', { type: 'checkbox', checked: false,
    style: { display: 'none' } }));
  const from = p.add('#from', new Element('DIV', 'from'));
  const to = p.add('#to', new Element('DIV', 'to'));
  const results = {};
  for (const [msg] of actions) {
    const pending = p.start(msg);
    await p.tick(0);
    assert.equal(pending.settled, true, `${msg.action} does not wait`);
    results[msg.action] = await pending.result;
  }
  for (const action of ['click', 'doubleClick', 'hover']) {
    assert.equal(results[action].ok, true, action);
    assert.equal(results[action].warning.code, 'hidden_or_covered', action);
  }
  assert.ok(['mousedown', 'click', 'dblclick', 'mouseover'].every(type => go.events.includes(type)));
  assert.deepEqual(results.type, { ok: true });
  assert.equal(name.value, 'Ada');
  assert.deepEqual(results.fill, { ok: true, pierced: false, tag: 'input' });
  assert.equal(email.value, 'a@b.c');
  assert.deepEqual(results.selectOption, { ok: true, value: 'blue', text: 'Blue' });
  assert.equal(color.value, 'blue');
  assert.deepEqual(results.checkBox, { ok: true, checked: true });
  assert.equal(agree.checked, true);
  assert.deepEqual(results.keypress, { ok: true, key: 'Enter', target: 'input' });
  assert.deepEqual(results.drag, { ok: true, from: '#from', to: '#to', via: 'pointer' });
  assert.ok(from.events.includes('dragstart') && to.events.includes('drop'));
  assert.equal(p.timers.size, 0, 'immediate mode schedules nothing');
}

async function testDelayedContent() {
  const p = page();
  const clicking = p.start({ action: 'click', selector: '#go', wait: true, timeout: 5000 });
  await p.tick(1000);
  assert.equal(clicking.settled, false, 'waits for the target to exist');
  const go = p.add('#go', new Element('BUTTON', 'go', { style: { display: 'none' } }));
  await p.tick(500);
  assert.equal(clicking.settled, false, 'pointer actions wait for the target to render');
  assert.deepEqual(go.events, [], 'nothing is dispatched while waiting');
  go.style.display = 'block';
  await p.tick(100);
  assert.equal(clicking.settled, true);
  const result = await clicking.result;
  assert.equal(result.ok, true);
  assert.equal(result.warning, undefined, 'a rendered target has no warning');
  assert.deepEqual(go.events, ['mousedown', 'click'], 'clicked once');

  // A target inside a display:none ancestor has no client rects until the ancestor shows.
  const menu = p.add('#item', new Element('A', 'item', { rects: 0 }));
  const hovering = p.start({ action: 'hover', selector: '#item', wait: true });
  await p.tick(300);
  assert.equal(hovering.settled, false);
  menu.rects = 1;
  await p.tick(100);
  assert.equal((await hovering.result).ok, true);
  assert.ok(menu.events.includes('mouseover'));

  // Already actionable: dispatches on the first check with no timer.
  const dbl = p.start({ action: 'doubleClick', selector: '#go', wait: true });
  await p.tick(0);
  assert.equal(dbl.settled, true);
  assert.ok(go.events.includes('dblclick'));
  assert.equal(p.timers.size, 0, 'no timer leaked');
}

async function testDynamicForms() {
  const p = page();
  const typing = p.start({ action: 'type', selector: '#name', text: 'Ada', wait: true });
  const filling = p.start({ action: 'fill', selector: '#email', value: 'a@b.c', wait: true });
  const selecting = p.start({ action: 'selectOption', selector: '#color', value: 'blue', wait: true });
  const checking = p.start({ action: 'checkBox', selector: '#agree', checked: true, wait: true });
  const pressing = p.start({ action: 'keypress', selector: '#name', key: 'Enter', wait: true });
  await p.tick(700);
  for (const pending of [typing, filling, selecting, checking, pressing]) assert.equal(pending.settled, false);

  // The form renders; its native checkbox and select stay display:none behind custom widgets.
  const name = p.add('#name', new Element('INPUT', 'name', { type: 'text' }));
  const email = p.add('#email', new Element('INPUT', 'email', { type: 'email' }));
  const color = p.add('#color', new Element('SELECT', 'color', { style: { display: 'none' },
    options: [{ value: 'red', text: 'Red' }, { value: 'blue', text: 'Blue' }] }));
  const agree = p.add('#agree', new Element('INPUT', 'agree', { type: 'checkbox', checked: false,
    style: { display: 'none' } }));
  await p.tick(100);
  assert.deepEqual(await typing.result, { ok: true });
  assert.equal(name.value, 'Ada');
  assert.deepEqual(await filling.result, { ok: true, pierced: false, tag: 'input' });
  assert.equal(email.value, 'a@b.c');
  assert.deepEqual(await selecting.result, { ok: true, value: 'blue', text: 'Blue' });
  assert.equal(color.value, 'blue', 'a hidden native select need not render');
  assert.deepEqual(await checking.result, { ok: true, checked: true });
  assert.equal(agree.checked, true, 'a hidden native checkbox need not render');
  assert.deepEqual(await pressing.result, { ok: true, key: 'Enter', target: 'input' });

  // Drag waits for both ends.
  const from = p.add('#from', new Element('DIV', 'from'));
  const dragging = p.start({ action: 'drag', from: '#from', to: '#to', wait: true });
  await p.tick(400);
  assert.equal(dragging.settled, false);
  assert.deepEqual(from.events, []);
  const to = p.add('#to', new Element('DIV', 'to'));
  await p.tick(100);
  assert.deepEqual(await dragging.result, { ok: true, from: '#from', to: '#to', via: 'pointer' });
  assert.ok(from.events.includes('dragstart') && to.events.includes('drop'));
  assert.equal(p.timers.size, 0, 'no timer leaked');
}

async function testTimeouts() {
  const p = page();
  const notFound = async (msg, error) => {
    const pending = p.start({ ...msg, wait: true, timeout: 2000 });
    await p.tick(1999);
    assert.equal(pending.settled, false, msg.action);
    await p.tick(1);
    assert.equal(pending.settled, true, `${msg.action} stops at the bound`);
    const result = await pending.result;
    assert.equal(result.error, error, 'error text matches immediate mode');
    assert.equal(result.code, 'not_found');
    assert.equal(result.retryable, true, 'nothing was dispatched');
    assert.match(result.hint, /browser_snapshot/);
    assert.deepEqual(result.state, { timeoutMs: 2000, elapsed: 2000 });
  };
  await notFound({ action: 'click', selector: '#missing' }, 'Element not found');
  await notFound({ action: 'fill', selector: '#missing', value: 'x' }, 'Element not found');
  await notFound({ action: 'keypress', selector: '#missing', key: 'a' }, 'No target');
  await notFound({ action: 'drag', from: '#missing', to: '#missing' }, 'from element not found');
  const from = p.add('#from', new Element('DIV', 'from'));
  await notFound({ action: 'drag', from: '#from', to: '#missing' }, 'to element not found');
  assert.deepEqual(from.events, [], 'drag never started');

  // Exists but never renders: a distinct code, never `timeout` (that means "sent, outcome unknown").
  const hidden = p.add('#hidden', new Element('BUTTON', 'hidden', { style: { visibility: 'hidden' } }));
  for (const action of ['click', 'doubleClick', 'hover']) {
    const pending = p.start({ action, selector: '#hidden', wait: true, timeout: 1000 });
    await p.tick(1000);
    const result = await pending.result;
    assert.equal(result.code, 'not_actionable', action);
    assert.equal(result.error, 'Timed out after 1000ms waiting for the target to render');
    assert.equal(result.retryable, true);
    assert.match(result.hint, /label or visible control/);
    assert.deepEqual(result.state, { reason: 'not_rendered', timeoutMs: 1000, elapsed: 1000 });
  }
  assert.deepEqual(hidden.events, [], 'nothing reaches a target that never rendered');

  // A target that leaves the DOM mid-wait is reported missing at the deadline.
  const leaving = p.add('#leaving', new Element('BUTTON', 'leaving', { style: { display: 'none' } }));
  const pending = p.start({ action: 'click', selector: '#leaving', wait: true, timeout: 500 });
  await p.tick(200);
  p.remove('#leaving');
  await p.tick(300);
  assert.equal((await pending.result).code, 'not_found');
  assert.deepEqual(leaving.events, []);

  // timeout 0 checks once.
  const once = p.start({ action: 'click', selector: '#missing', wait: true, timeout: 0 });
  await p.tick(0);
  assert.equal((await once.result).code, 'not_found');
  for (const timeout of [-1, 'soon']) {
    const invalid = p.start({ action: 'click', selector: '#go', wait: true, timeout });
    await p.tick(0);
    assert.deepEqual(await invalid.result, { error: 'timeout must be non-negative milliseconds' });
  }
  assert.equal(p.timers.size, 0, 'no timer leaked');
}

async function testRefsAndTargetlessActions() {
  const p = page();
  const go = p.add('#go', new Element('BUTTON', 'go'));
  const ref = (await p.start({ action: 'find', query: '#go' }).result).results[0].ref;
  go.style.display = 'none';

  // A ref whose element is removed cannot come back, so the wait ends at once.
  const clicking = p.start({ action: 'click', selector: ref, wait: true, timeout: 5000 });
  await p.tick(300);
  assert.equal(clicking.settled, false, 'waits for the ref target to render');
  go.isConnected = false;
  await p.tick(100);
  assert.equal(clicking.settled, true, 'a removed ref fails before the deadline');
  const stale = await clicking.result;
  assert.equal(stale.code, 'stale_ref');
  assert.equal(stale.state.reason, 'element_removed');
  assert.deepEqual(go.events, []);

  const unknown = p.start({ action: 'hover', selector: ref.replace(/e\d+$/, 'e999'), wait: true });
  await p.tick(0);
  assert.equal((await unknown.result).state.reason, 'not_in_snapshot');

  // Coordinates and the active element have no target to wait for.
  const target = p.add('#go', new Element('BUTTON', 'go'));
  const atPoint = p.start({ action: 'click', coords: { x: 1, y: 1 }, wait: true });
  const active = p.start({ action: 'keypress', key: 'Escape', wait: true });
  await p.tick(0);
  assert.equal((await atPoint.result).ok, true);
  assert.deepEqual(target.events, ['mousedown', 'click']);
  assert.deepEqual(await active.result, { ok: true, key: 'Escape', target: 'body' });
  assert.deepEqual(p.body.events, ['keydown']);

  // Other actions ignore wait.
  const submitting = p.start({ action: 'submitForm', selector: '#missing', wait: true });
  await p.tick(0);
  assert.equal(submitting.settled, true);
  assert.equal((await submitting.result).code, 'not_found');
  assert.equal(p.timers.size, 0, 'no timer leaked');
}

// A 0 ms waitForElement is a single check that needs no page timer (#202): background tabs
// throttle timers, so a check that waited for one could end long after its deadline.
async function testZeroTimeoutElementWaitIsASingleCheck() {
  const p = page();
  const missing = p.start({ action: 'waitForElement', selector: '#menu', timeout: 0 });
  await p.tick(0);
  assert.equal(missing.settled, true, 'a single check settles without a timer');
  const result = await missing.result;
  assert.equal(result.found, false);
  assert.equal(result.ok, false);
  assert.equal(p.timers.size, 0, 'a single check schedules nothing');

  p.add('#menu', new Element('DIV', 'menu'));
  const present = p.start({ action: 'waitForElement', selector: '#menu', timeout: 0 });
  await p.tick(0);
  assert.equal((await present.result).found, true);

  // Without a timeout the wait still defaults to 10 s.
  const later = p.start({ action: 'waitForElement', selector: '#later' });
  await p.tick(9900);
  assert.equal(later.settled, false);
  await p.tick(200);
  assert.equal((await later.result).found, false);
}

(async () => {
  await testZeroTimeoutElementWaitIsASingleCheck();
  await testImmediateModeIsUnchanged();
  await testImmediateModeOnDelayedContentAndDynamicForms();
  await testDelayedContent();
  await testDynamicForms();
  await testTimeouts();
  await testRefsAndTargetlessActions();
  console.log('actionability_wait: all tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
