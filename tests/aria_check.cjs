// Run with: node tests/aria_check.cjs (no browser or npm dependencies).
// #574: check sets an element with role checkbox, switch or radio by clicking it once when
// aria-checked is not the requested state, and verifies that it became that state; any other
// element fails with wrong_element_type. A native checkbox or radio is set as before.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    Object.assign(this, { clientX: init.clientX, clientY: init.clientY, button: init.button || 0 });
  }
}

const observers = new Set();
class MutationObserver {
  constructor(callback) { this.callback = callback; }
  observe(target, options) { Object.assign(this, { target, options }); observers.add(this); }
  takeRecords() { return []; }
  disconnect() { observers.delete(this); }
}
// The check's observers still connected (the click's popup watch has its own).
const watching = () => [...observers].filter(observer => observer.options?.attributeFilter?.[0] === 'aria-checked').length;

class Element extends EventTarget {
  constructor(tagName, attrs = {}, props = {}) {
    super();
    Object.assign(this, { tagName, attrs, isConnected: true, textContent: '', events: [], rendered: true }, props);
    for (const type of ['click', 'input', 'change']) this.addEventListener(type, event => this.events.push(event.type));
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  setAttribute(key, value) {
    this.attrs[key] = String(value);
    for (const observer of observers) {
      if (observer.target === this) queueMicrotask(() => observer.callback([{ type: 'attributes', target: this }]));
    }
  }
  hasAttribute(key) { return key in this.attrs; }
  matches(selector) { return selector === ':disabled' && !!this.disabled; }
  closest() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  focus() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 40, height: 20 }; }
  getClientRects() { return this.rendered ? [{}] : []; }
  get clicks() { return this.events.filter(type => type === 'click').length; }
}

const task = () => new Promise(resolve => setImmediate(resolve));

// How long the stub background takes to answer the content script's pause: the tests' stand-in
// for ARIA_CHECK_SETTLE_MS. null never answers, so a result proves the wait ended early.
function page(elements, { pauseMs = 60, matches = {} } = {}) {
  let listener;
  const pauses = [];
  const window = Object.assign(new EventTarget(), {
    PointerEvent: MouseEvent, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) });
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, PointerEvent: MouseEvent, MessageChannel, MutationObserver,
    setTimeout, clearTimeout, CSS: { escape: x => x }, navigator: { platform: 'Linux x86_64' },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window,
    document: {
      body: new Element('BODY'),
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => matches[selector] || [],
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: msg => {
        if (msg?.action !== 'pointerPause') return Promise.resolve(null);
        pauses.push(msg.ms);
        return pauseMs == null ? new Promise(() => {})
          : new Promise(resolve => setTimeout(resolve, pauseMs, { ok: true }));
      },
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
  // popupWatch is the click's note to the background, which takes the popup and drops the note.
  const check = async (selector, checked, extra = {}) => {
    const { popupWatch, ...result } = await send({ action: 'checkBox', selector, checked, ...extra });
    assert.equal(popupWatch, result.ok && result.clicked ? true : undefined, 'watched for a popup exactly when it clicked');
    return result;
  };
  return { send, window, pauses, check };
}

// A widget that publishes aria-checked when clicked: next(now) is the state a click steps to,
// publish schedules it ('sync', 'microtask', a number of ms, or 'never').
function widget(role, checked, { tag = 'BUTTON', publish = 'sync', next = now => now === 'true' ? 'false' : 'true',
  attrs = {} } = {}) {
  const el = new Element(tag, { role, ...(checked == null ? {} : { 'aria-checked': checked }), ...attrs },
    { textContent: 'Dark mode' });
  el.addEventListener('click', () => {
    const value = next(el.getAttribute('aria-checked') ?? 'false');
    const set = () => el.setAttribute('aria-checked', value);
    if (publish === 'sync') set();
    else if (publish === 'microtask') queueMicrotask(set);
    else if (publish !== 'never') setTimeout(set, publish);
  });
  return el;
}

async function testClicksOnceWhenTheStateDiffers() {
  for (const role of ['checkbox', 'switch', 'radio']) {
    const el = widget(role, 'false');
    const { check, pauses } = page({ '#t': el }, { pauseMs: null });
    assert.deepEqual(await check('#t', true),
      { ok: true, tag: 'button', text: 'Dark mode', role, checked: true, clicked: true }, role);
    assert.equal(el.clicks, 1);
    assert.equal(el.attrs['aria-checked'], 'true');
    // It is that state now: nothing is clicked.
    assert.deepEqual(await check('#t', true), { ok: true, tag: 'button', role, checked: true, clicked: false });
    assert.equal(el.clicks, 1);
    assert.deepEqual(pauses, [], 'a state published during the click is not waited for');
  }
  const on = widget('switch', 'true', { tag: 'DIV' });
  const { check } = page({ '#t': on });
  assert.deepEqual(await check('#t', false),
    { ok: true, tag: 'div', text: 'Dark mode', role: 'switch', checked: false, clicked: true });
  assert.equal(on.attrs['aria-checked'], 'false');

  // A missing aria-checked is unchecked, and a value is read ignoring case and spaces.
  const bare = widget('checkbox', null);
  const bareChecks = page({ '#t': bare });
  assert.equal((await bareChecks.check('#t', false)).clicked, false);
  assert.equal((await bareChecks.check('#t', true)).clicked, true);
  assert.equal(bare.clicks, 1);
  assert.equal((await page({ '#t': widget('checkbox', ' TRUE ') }).check('#t', true)).clicked, false);
  // The first role token counts, as for an ARIA slider.
  assert.equal((await page({ '#t': widget('Switch button', 'false') }).check('#t', true)).role, 'switch');
}

async function testWaitsForAStatePublishedAfterTheClick() {
  // A framework that renders in a microtask or after a request: the wait ends when it does.
  for (const publish of ['microtask', 20]) {
    const el = widget('checkbox', 'false', { publish });
    const { check, pauses } = page({ '#t': el }, { pauseMs: null });
    const result = await check('#t', true);
    assert.deepEqual([result.ok, result.checked, result.clicked], [true, true, true], String(publish));
    // A microtask has run by the time the click's result is in; a later state is waited for,
    // at most ARIA_CHECK_SETTLE_MS, timed by the background.
    assert.deepEqual(pauses, publish === 'microtask' ? [] : [1000]);
    assert.equal(el.clicks, 1);
    assert.equal(watching(), 0, 'the observer is disconnected');
  }
}

async function testStateNotReached() {
  const stuck = widget('checkbox', 'false', { publish: 'never', attrs: { 'aria-disabled': 'true' } });
  const { check, pauses } = page({ '#t': stuck });
  const result = await check('#t', true);
  assert.equal(result.error, 'The checkbox\'s aria-checked is "false" after the click, not "true"');
  assert.deepEqual([result.code, result.retryable], ['value_not_reached', false]);
  assert.deepEqual(result.state, { role: 'checkbox', checked: 'false', target: true, clicked: true, reason: 'unchanged' });
  assert.match(result.hint, /^The click was dispatched and aria-checked did not change\./);
  assert.equal(stuck.clicks, 1, 'never a second click');
  assert.deepEqual(pauses, [1000]);
  assert.equal(watching(), 0);

  // aria-checked="mixed" is neither state, so it is clicked. A tri-state control that steps
  // mixed -> true -> false reaches true at once and false with a second check.
  const cycle = { mixed: 'true', true: 'false', false: 'mixed' };
  const partial = widget('checkbox', 'mixed', { next: now => cycle[now] });
  const tri = page({ '#t': partial });
  const stepped = await tri.check('#t', false);
  assert.deepEqual([stepped.code, stepped.retryable], ['value_not_reached', false]);
  assert.deepEqual(stepped.state, { role: 'checkbox', checked: 'true', target: false, clicked: true, reason: 'other_state' });
  assert.match(stepped.hint, /check again to click it once more/);
  assert.equal(partial.clicks, 1);
  assert.deepEqual([(await tri.check('#t', false)).checked, partial.clicks], [false, 2]);
  const mixed = widget('checkbox', 'mixed', { next: now => cycle[now] });
  assert.deepEqual([(await page({ '#t': mixed }).check('#t', true)).checked, mixed.clicks], [true, 1]);

  // The page replaced the element, so its attribute says nothing about the control.
  const replaced = widget('switch', 'false', { publish: 'never' });
  replaced.addEventListener('click', () => { replaced.isConnected = false; });
  const gone = await page({ '#t': replaced }).check('#t', true);
  assert.deepEqual([gone.code, gone.state.reason, gone.retryable], ['value_not_reached', 'replaced', false]);
}

async function testRadiosAreNotUnchecked() {
  // As for a native radio, which no click unchecks: nothing is clicked.
  const selected = widget('radio', 'true');
  const { check } = page({ '#on': selected, '#off': widget('radio', 'false') });
  const result = await check('#on', false);
  assert.equal(result.error, 'Cannot uncheck a selected radio');
  assert.deepEqual([result.code, result.retryable], ['value_not_reached', true]);
  assert.deepEqual(result.state, { role: 'radio', checked: 'true', target: false, clicked: false, reason: 'radio' });
  assert.match(result.hint, /^Nothing was clicked/);
  assert.equal(selected.clicks, 0);
  assert.deepEqual(await check('#off', false), { ok: true, tag: 'button', role: 'radio', checked: false, clicked: false });
}

async function testADocumentTheClickUnloadsAnswersAsTheClick() {
  // A link styled as a checkbox filter, or a handler that sets location: beforeunload fires
  // while the click is handled or soon after, and the old document never publishes a state.
  for (const when of ['sync', 15]) {
    const el = widget('checkbox', 'false', { tag: 'A', publish: 'never' });
    const { check, window, pauses } = page({ '#t': el }, { pauseMs: null });
    const unload = () => window.dispatchEvent(new Event('beforeunload'));
    el.addEventListener('click', () => when === 'sync' ? unload() : setTimeout(unload, when));
    assert.deepEqual(await check('#t', true),
      { ok: true, tag: 'a', text: 'Dark mode', role: 'checkbox', clicked: true }, String(when));
    assert.deepEqual(pauses, when === 'sync' ? [] : [1000]);
    assert.equal(watching(), 0);
    // The listeners are gone: a later unload does not reach a finished check.
    let late = 0;
    const count = () => { late++; };
    window.addEventListener('beforeunload', count);
    unload();
    assert.equal(late, 1);
  }
  const hidden = widget('switch', 'false', { publish: 'never' });
  const { check, window } = page({ '#t': hidden }, { pauseMs: null });
  hidden.addEventListener('click', () => setTimeout(() => window.dispatchEvent(new Event('pagehide')), 5));
  assert.deepEqual([(await check('#t', true)).ok, hidden.clicks], [true, 1]);
}

async function testTheClicksResultFieldsPassThrough() {
  // A control a user could not click: the click's warning comes back with the check.
  const covered = widget('checkbox', 'false');
  covered.rendered = false;
  const result = await page({ '#t': covered }).check('#t', true);
  assert.deepEqual([result.ok, result.checked, result.clicked], [true, true, true]);
  assert.equal(result.warning.code, 'hidden_or_covered');
  // By ref, as a script that read the snapshot checks it.
  const el = widget('checkbox', 'false');
  const { send, check } = page({ '#t': el });
  const { ref } = await send({ action: 'waitForElement', selector: '#t', timeout: 0 });
  assert.deepEqual([(await check(ref, true)).checked, el.clicks], [true, 1]);
  // Like a click, it keeps the settle floor a plain native check drops.
  await send({ action: 'checkBox', selector: '#t', checked: false, deltaBaseline: true });
  assert.equal((await send({ action: 'settleState' })).floor, true);
}

async function testOnlyTheElementAClickWouldActOn() {
  // The selector's first match is disabled and its ARIA checkbox is not rendered: a click on
  // the selector acts on the first, which is what ask-before-act inspected.
  const first = new Element('BUTTON', {}, { disabled: true, textContent: 'Off' });
  const unrendered = widget('checkbox', 'false');
  unrendered.rendered = false;
  const { check } = page({ '.both': first }, { matches: { '.both': [first, unrendered] } });
  const result = await check('.both', true);
  assert.deepEqual([result.code, result.retryable, result.state.matches], ['ambiguous_target', false, 2]);
  assert.deepEqual(result.state.candidates.map(candidate => candidate.role), ['button', 'checkbox']);
  assert.equal(unrendered.clicks, 0);
  // Its ref names it alone.
  assert.equal((await check(result.state.candidates[1].ref, true)).checked, true);
  // first: true acts on the first match for a click and a check alike.
  assert.equal((await check('.both', true, { first: true })).code, 'wrong_element_type');
}

async function testOtherElementsAreRefusedWithTheirRole() {
  const elements = { '#div': new Element('DIV'), '#button': new Element('BUTTON'),
    '#menu': new Element('LI', { role: 'menuitemcheckbox', 'aria-checked': 'false' }),
    '#text': Object.assign(new Element('INPUT'), { type: 'text' }) };
  const { check } = page(elements);
  for (const [selector, state] of [['#div', { tag: 'div' }], ['#button', { tag: 'button', role: 'button' }],
    ['#menu', { tag: 'li', role: 'menuitemcheckbox' }], ['#text', { tag: 'input', role: 'textbox' }]]) {
    const result = await check(selector, true);
    assert.equal(result.error, 'Element is not a checkbox/radio', selector);
    assert.deepEqual([result.code, result.retryable], ['wrong_element_type', false]);
    assert.deepEqual(result.state, state);
    assert.match(result.hint, /Nothing was dispatched: click this element instead \(browser_click\)/);
    assert.deepEqual(elements[selector].events, []);
  }
  assert.equal((await check('#missing', true)).code, 'not_found');
}

async function testNativeInputsAreSetAsBefore() {
  const box = Object.assign(new Element('INPUT', { role: 'switch' }), { type: 'checkbox', checked: false });
  const radio = Object.assign(new Element('INPUT'), { type: 'radio', checked: true });
  const { check, pauses } = page({ '#box': box, '#radio': radio });
  assert.deepEqual(await check('#box', true), { ok: true, checked: true });
  assert.deepEqual(box.events, ['input', 'change'], 'the property is set; nothing is clicked');
  assert.deepEqual(await check('#box', false), { ok: true, checked: false });
  assert.deepEqual(await check('#radio', false), { ok: true, checked: false });
  assert.deepEqual(pauses, []);
}

testClicksOnceWhenTheStateDiffers().then(testWaitsForAStatePublishedAfterTheClick).then(testStateNotReached)
  .then(testRadiosAreNotUnchecked).then(testADocumentTheClickUnloadsAnswersAsTheClick)
  .then(testTheClicksResultFieldsPassThrough).then(testOnlyTheElementAClickWouldActOn)
  .then(testOtherElementsAreRefusedWithTheirRole).then(testNativeInputsAreSetAsBefore)
  .then(task).then(() => console.log('ARIA check checks passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
