// Run with: node tests/aria_slider.cjs (no browser or npm dependencies).
// #311: fill sets an ARIA slider or spinbutton without a native input by pressing arrow keys
// until aria-valuenow is the value, and fails with value_not_reached when it is not reached;
// keypress takes a bounded repeat; not_fillable on a slider names the keyboard alternative.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class KeyboardEvent extends Event {
  constructor(type, init) {
    super(type, init);
    for (const name of ['key', 'code', 'keyCode', 'ctrlKey', 'shiftKey', 'altKey', 'metaKey']) this[name] = init[name];
  }
}

const document = { body: null, activeElement: null };

class Element extends EventTarget {
  constructor(tagName, attrs = {}) {
    super();
    Object.assign(this, { tagName, attrs, isConnected: true, keys: [], focused: 0 });
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  hasAttribute(key) { return key in this.attrs; }
  matches() { return false; }
  closest() { return null; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  focus() { this.focused++; document.activeElement = this; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 200, height: 20 }; }
}

const ARROWS = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 };

// A slider widget as frameworks build one: keydown computes the next value from the rendered
// state and renders it in a scheduler task (a MessageChannel message, as React's scheduler
// posts), so keys pressed without a task between them all start from the same value. handles
// maps keys to a step direction.
function slider({ role = 'slider', min = 0, max = 100, step = 1, value = 50, handles = ARROWS, ends = true,
  disabled = false, attrs = {} } = {}) {
  const el = new Element('DIV', { role, tabindex: '0', 'aria-valuemin': String(min),
    'aria-valuemax': String(max), 'aria-valuenow': String(value), ...attrs });
  let rendered = value;
  el.addEventListener('keydown', event => {
    el.keys.push(event.key);
    if (disabled) return;
    let next;
    if (ends && event.key === 'Home') next = min;
    else if (ends && event.key === 'End') next = max;
    else if (event.key in handles) next = Math.min(max, Math.max(min, rendered + handles[event.key] * step));
    else return;
    event.preventDefault();
    const channel = new MessageChannel();
    channel.port1.onmessage = () => { channel.port1.close(); rendered = next; el.setAttribute('aria-valuenow', next); };
    channel.port2.postMessage(null);
  });
  return el;
}

// clock, if given, is the page's Date (its now() for fill's time limit).
function page(elements, clock = Date) {
  let listener;
  document.body = new Element('BODY');
  document.activeElement = document.body;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, KeyboardEvent, MessageChannel, CSS: { escape: x => x }, Date: clock,
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: Object.assign(document, {
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
    }),
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  // Results come from the vm realm, so compare plain copies.
  return msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
}

// After the widget's render of a key pressed just before.
const nextTask = () => new Promise(resolve => {
  const channel = new MessageChannel();
  channel.port1.onmessage = () => { channel.port1.close(); resolve(); };
  channel.port2.postMessage(null);
});

async function fill(widget, value, clock) {
  const send = page({ '#slider': widget }, clock);
  return send({ action: 'fill', selector: '#slider', value });
}

async function testFillReachesTheValue() {
  const widget = slider();
  assert.deepEqual(await fill(widget, '15'), { ok: true, tag: 'div', role: 'slider', value: 15, target: 15,
    min: 0, max: 100, keys: 35 });
  assert.equal(widget.attrs['aria-valuenow'], '15');
  assert.equal(widget.focused, 1, 'focused first, as a user would');
  assert.deepEqual(new Set(widget.keys), new Set(['ArrowLeft']));

  // 0.1 + 0.2 renders as 0.30000000000000004, which counts as 0.3.
  const tenths = slider({ min: 0, max: 1, step: 0.1, value: 0.1 });
  assert.deepEqual([(await fill(tenths, '0.3')).value, tenths.keys], [0.30000000000000004, ['ArrowRight', 'ArrowRight']]);

  const up = slider({ min: 0, max: 1, step: 0.25, value: 0.5 });
  assert.equal((await fill(up, 0.75)).value, 0.75);
  assert.deepEqual(up.keys, ['ArrowRight']);

  // The ends are one key away.
  const low = slider();
  assert.equal((await fill(low, '0')).value, 0);
  assert.deepEqual(low.keys, ['Home']);
  const high = slider();
  assert.equal((await fill(high, '100')).value, 100);
  assert.deepEqual(high.keys, ['End']);
  // A widget without Home/End still gets there by arrows.
  const noEnds = slider({ value: 97, ends: false });
  assert.equal((await fill(noEnds, '100')).value, 100);
  assert.deepEqual(noEnds.keys, ['End', 'ArrowRight', 'ArrowRight', 'ArrowRight']);

  const unchanged = slider();
  assert.deepEqual(await fill(unchanged, '50'), { ok: true, tag: 'div', role: 'slider', value: 50, target: 50,
    min: 0, max: 100, keys: 0 });
}

async function testKeyPairs() {
  // A spinbutton (and a vertical slider) steps with Up/Down.
  const spin = slider({ role: 'spinbutton', value: 3, min: 1, max: 10, handles: { ArrowUp: 1, ArrowDown: -1 } });
  assert.equal((await fill(spin, '6')).value, 6);
  assert.deepEqual(spin.keys, ['ArrowUp', 'ArrowUp', 'ArrowUp']);
  const vertical = slider({ attrs: { 'aria-orientation': 'vertical' } });
  await fill(vertical, '48');
  assert.deepEqual(vertical.keys, ['ArrowDown', 'ArrowDown']);

  // A horizontal widget that handles only Up/Down: Right does nothing, so Up is tried.
  const upDown = slider({ handles: { ArrowUp: 1, ArrowDown: -1 } });
  const result = await fill(upDown, '53');
  assert.equal(result.value, 53);
  assert.deepEqual(upDown.keys, ['ArrowRight', 'ArrowUp', 'ArrowUp', 'ArrowUp']);

  // A right-to-left slider: Right decreases, so the other pair takes over from where it moved.
  const rtl = slider({ handles: { ArrowRight: -1, ArrowLeft: 1, ArrowUp: 1, ArrowDown: -1 } });
  assert.equal((await fill(rtl, '52')).value, 52);
  assert.deepEqual(rtl.keys, ['ArrowRight', 'ArrowUp', 'ArrowUp', 'ArrowUp']);
}

async function testValueNotReached() {
  // Steps of 10 skip 15: it stops where it stepped past.
  const coarse = slider({ step: 10 });
  const past = await fill(coarse, '15');
  assert.equal(past.code, 'value_not_reached');
  assert.equal(past.retryable, false, 'keys were pressed');
  assert.equal(past.error, 'The slider stopped at 10, not 15');
  assert.deepEqual(past.state, { role: 'slider', value: 10, target: 15, min: 0, max: 100, keys: 4,
    reason: 'stepped_past' });
  assert.match(past.hint, /steps skip the value/);

  // A widget that ignores the keys: both pairs are tried, then it stops.
  const stuck = slider({ disabled: true });
  const ignored = await fill(stuck, '15');
  assert.equal(ignored.code, 'value_not_reached');
  assert.deepEqual(ignored.state, { role: 'slider', value: 50, target: 15, min: 0, max: 100, keys: 2,
    reason: 'stuck' });
  assert.deepEqual(stuck.keys, ['ArrowLeft', 'ArrowDown']);
  assert.match(ignored.hint, /stopped moving/);

  // A value outside aria-valuemin/max fails before any key, so it may be retried.
  for (const target of ['150', '-1']) {
    const end = slider({ value: 95 });
    const beyond = await fill(end, target);
    assert.equal(beyond.code, 'value_not_reached');
    assert.equal(beyond.retryable, true, 'nothing was pressed');
    assert.equal(beyond.error, `${target} is outside the slider's range, 0 to 100`);
    assert.deepEqual(beyond.state, { role: 'slider', value: 95, target: Number(target), min: 0, max: 100, keys: 0,
      reason: 'out_of_range' });
    assert.match(beyond.hint, /^Nothing was pressed/);
    assert.deepEqual([end.keys, end.focused], [[], 0]);
  }

  // Right moves it away and Up/Down do nothing: it says so, not that it never moved.
  const away = slider({ handles: { ArrowRight: -1, ArrowLeft: 1 } });
  const movedAway = await fill(away, '52');
  assert.deepEqual([movedAway.state.value, movedAway.state.keys, movedAway.state.reason], [49, 2, 'moved_away']);
  assert.deepEqual(away.keys, ['ArrowRight', 'ArrowUp']);
  assert.match(movedAway.hint, /moved it away from the value/);

  // A long way stops at the key limit; filling again continues from there.
  const long = slider({ max: 5000, value: 0 });
  const limited = await fill(long, '4000');
  assert.deepEqual([limited.code, limited.state.value, limited.state.keys, limited.state.reason],
    ['value_not_reached', 1000, 1000, 'key_limit']);

  // A slow page stops at the time limit, before the bridge's request times out: each read of
  // the clock here is 100 ms later, so 20 s run out after about 200 keys.
  let ms = 0;
  const clock = class extends Date { static now() { return ms += 100; } };
  const slow = await fill(slider({ max: 5000, value: 0 }), '4000', clock);
  assert.deepEqual([slow.code, slow.state.reason], ['value_not_reached', 'time_limit']);
  assert.ok(slow.state.keys > 150 && slow.state.keys < 250, `${slow.state.keys} keys`);
  assert.match(slow.hint, /more than 20 s/);
}

async function testWhatFillRefuses() {
  const words = slider();
  const notNumber = await fill(words, 'medium');
  assert.equal(notNumber.error, 'An ARIA slider takes a number, not "medium"');
  assert.deepEqual(words.keys, []);

  // No aria-valuenow to step toward: not_fillable, and the hint names the keyboard.
  const unknown = slider();
  delete unknown.attrs['aria-valuenow'];
  const refused = await fill(unknown, '15');
  assert.equal(refused.code, 'not_fillable');
  assert.deepEqual(refused.state, { tag: 'div', reason: 'no_field', role: 'slider' });
  assert.match(refused.hint, /without a numeric aria-valuenow/);
  assert.match(refused.hint, /browser_press_key; repeat presses a key up to 100 times/);
  assert.deepEqual(unknown.keys, []);

  // browser_form_fill reads native fields only; its hint names fill and the keyboard.
  const form = page({ '#slider': slider() });
  const formFill = (await form({ action: 'formFill', fields: { '#slider': '15' } })).results['#slider'];
  assert.equal(formFill.code, 'not_fillable');
  assert.match(formFill.hint, /fill \(browser_fill_form type slider, browser_type without slowly or in fill mode\)/);
  assert.match(formFill.hint, /browser_press_key/);

  // A plain element keeps the usual hint.
  const plain = await page({ '#box': new Element('DIV') })({ action: 'fill', selector: '#box', value: '1' });
  assert.equal(plain.code, 'not_fillable');
  assert.match(plain.hint, /^Target the field/);
  assert.equal(plain.state.role, undefined);
}

async function testNativeRangeKeepsItsValueSetter() {
  const range = Object.assign(new Element('INPUT', { role: 'slider' }), { type: 'range', value: '50', events: [] });
  for (const type of ['input', 'change']) range.addEventListener(type, () => range.events.push(type));
  const result = await page({ '#range': range })({ action: 'fill', selector: '#range', value: '15' });
  assert.deepEqual(result, { ok: true, pierced: false, tag: 'input' });
  assert.equal(range.value, '15');
  assert.deepEqual(range.events, ['input', 'change']);
  assert.deepEqual(range.keys, []);

  // A role=slider wrapper around a native range fills the range, not by keys.
  const inner = Object.assign(new Element('INPUT'), { type: 'range', value: '50', events: [] });
  const wrapper = new Element('DIV', { role: 'slider', 'aria-valuenow': '50' });
  wrapper.querySelector = selector => selector === 'input, textarea, select' ? inner : null;
  const wrapped = await page({ '#wrapper': wrapper })({ action: 'fill', selector: '#wrapper', value: '15' });
  assert.deepEqual(wrapped, { ok: true, pierced: true, tag: 'input' });
  assert.equal(inner.value, '15');
  assert.deepEqual(wrapper.keys, []);
}

async function testKeypressRepeat() {
  const widget = slider();
  const send = page({ '#slider': widget });
  widget.focus();
  // A task between presses lets the widget render, so each press steps from the last value.
  const result = await send({ action: 'keypress', key: 'ArrowLeft', repeat: 7 });
  assert.deepEqual(result, { ok: true, key: 'ArrowLeft', target: 'div', effect: null, prevented: true, repeat: 7 });
  await nextTask();
  assert.equal(widget.attrs['aria-valuenow'], '43');
  assert.equal(widget.keys.length, 7);

  const single = await send({ action: 'keypress', selector: '#slider', key: 'ArrowRight', repeat: 1 });
  assert.equal(single.repeat, undefined);
  const most = await send({ action: 'keypress', selector: '#slider', key: 'ArrowRight', repeat: 100 });
  assert.equal(most.repeat, 100);
  assert.equal(widget.keys.length, 108);
  await nextTask();
  assert.equal(widget.attrs['aria-valuenow'], '100');

  for (const repeat of [0, 101, 2.5, '3', -1, true]) {
    const refused = await send({ action: 'keypress', selector: '#slider', key: 'ArrowLeft', repeat });
    assert.deepEqual(refused, { error: 'repeat must be an integer from 1 to 100' }, `repeat ${JSON.stringify(repeat)}`);
  }
  assert.equal(widget.keys.length, 108, 'nothing pressed for a refused repeat');

  const missing = await send({ action: 'keypress', selector: '#gone', key: 'ArrowLeft', repeat: 3 });
  assert.equal(missing.code, 'not_found');
  assert.equal(missing.pressed, undefined);
}

(async () => {
  await testFillReachesTheValue();
  await testKeyPairs();
  await testValueNotReached();
  await testWhatFillRefuses();
  await testNativeRangeKeepsItsValueSetter();
  await testKeypressRepeat();
})().then(() => console.log('ARIA slider checks passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
