// Run with: node tests/form_fill.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element extends EventTarget {
  constructor(tagName, attrs = {}) {
    super();
    Object.assign(this, { tagName, attrs, isConnected: true, events: [] });
    for (const type of ['input', 'change']) this.addEventListener(type, event => this.events.push(event.type));
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  focus() {}
}

// Mirrors the DOM: assigning a value with no matching option clears the selection.
class SelectElement extends Element {
  constructor(values) {
    super('SELECT');
    this.options = values.map(value => ({ value, text: value[0].toUpperCase() + value.slice(1) }));
    this.selectedIndex = 0;
  }
  get value() { return this.options[this.selectedIndex]?.value ?? ''; }
  set value(value) { this.selectedIndex = this.options.findIndex(opt => opt.value === String(value)); }
}

// Mirrors the DOM: checking a radio unchecks the rest of its name group.
class RadioElement extends Element {
  constructor(name, group) {
    super('INPUT', { name });
    Object.assign(this, { type: 'radio', group, _checked: false });
    group.push(this);
  }
  get checked() { return this._checked; }
  set checked(value) {
    if (value) for (const radio of this.group) radio._checked = false;
    this._checked = !!value;
  }
}

async function testFormFill() {
  const color = new SelectElement(['red', 'blue']);
  const name = Object.assign(new Element('INPUT'), { type: 'text', value: '' });
  const label = { innerText: 'Color', querySelector: () => color };
  const agree = Object.assign(new Element('INPUT'), { type: 'checkbox', checked: false });
  const sizes = [];
  const small = new RadioElement('size', sizes);
  const medium = new RadioElement('size', sizes);
  const elements = { '#name': name, '#color': color, '#agree': agree, '#small': small, '#medium': medium };
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, CSS: { escape: x => x },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: {
      body: {},
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => selector === 'label' ? [label] : [],
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  // Results come from the vm realm, so compare plain copies.
  const send = msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));

  const valid = await send({ action: 'formFill', fields: { Color: 'blue' } });
  assert.deepEqual(valid.results.Color, { ok: true, tag: 'select', value: 'blue' });
  assert.equal(color.selectedIndex, 1);
  assert.deepEqual(color.events, ['change', 'input'], 'same events as selectOption');

  color.events = [];
  const mixed = await send({ action: 'formFill', fields: { Color: 'nonexistent', '#name': 'Ada' } });
  assert.deepEqual(mixed.results.Color, { error: 'No matching option for value: nonexistent' });
  assert.equal(color.value, 'blue', 'previous selection is preserved');
  assert.equal(color.selectedIndex, 1);
  assert.deepEqual(color.events, [], 'no change/input signal without a match');
  assert.deepEqual(mixed.results['#name'], { ok: true, tag: 'input' });
  assert.equal(name.value, 'Ada', 'other fields in the same call still fill');

  const control = await send({ action: 'selectOption', selector: '#color', value: 'nonexistent' });
  assert.equal(control.error, mixed.results.Color.error, 'matches selectOption error');
  assert.equal(color.value, 'blue');

  for (const [value, expected] of [[true, true], ['false', false], ['ON', true], [' off ', false],
    ['Yes', true], ['no', false], ['1', true], [0, false], [1, true], [false, false]]) {
    agree.events = [];
    const result = await send({ action: 'formFill', fields: { '#agree': value } });
    assert.deepEqual(result.results['#agree'], { ok: true, tag: 'input', checked: expected }, `checkbox ${JSON.stringify(value)}`);
    assert.equal(agree.checked, expected);
    assert.deepEqual(agree.events, ['change']);
  }

  for (const value of ['maybe', '', 'null', 2, null, {}]) {
    agree.events = [];
    const result = await send({ action: 'formFill', fields: { '#agree': value, '#name': 'Grace' } });
    assert.match(result.results['#agree'].error, /^Invalid checkbox value: .* \(expected true\/false/, `rejects ${JSON.stringify(value)}`);
    assert.equal(agree.checked, false, 'invalid value leaves the checkbox unchanged');
    assert.deepEqual(agree.events, [], 'no change signal for an invalid value');
    assert.deepEqual(result.results['#name'], { ok: true, tag: 'input' }, 'partial failure still fills other fields');
  }

  const radio = await send({ action: 'formFill', fields: { '#small': 'true' } });
  assert.deepEqual(radio.results['#small'], { ok: true, tag: 'input', checked: true });
  assert.deepEqual([small.checked, medium.checked], [true, false]);

  small.events = [];
  const uncheck = await send({ action: 'formFill', fields: { '#small': false } });
  assert.deepEqual(uncheck.results['#small'], { error: 'Cannot uncheck a selected radio; set another option in its group to true' });
  assert.deepEqual([small.checked, medium.checked], [true, false], 'radio group keeps a selection');
  assert.deepEqual(small.events, []);

  medium.events = [];
  const unselected = await send({ action: 'formFill', fields: { '#medium': 'off' } });
  assert.deepEqual(unselected.results['#medium'], { ok: true, tag: 'input', checked: false });
  assert.deepEqual(medium.events, [], 'false on an unselected radio is a no-op');

  const swap = await send({ action: 'formFill', fields: { '#medium': 'yes', '#small': 'false', '#agree': 'sure' } });
  assert.deepEqual(swap.results['#medium'], { ok: true, tag: 'input', checked: true });
  assert.deepEqual(swap.results['#small'], { ok: true, tag: 'input', checked: false }, 'already deselected by its group');
  assert.match(swap.results['#agree'].error, /^Invalid checkbox value: "sure"/);
  assert.deepEqual([small.checked, medium.checked], [false, true]);

  const badRadio = await send({ action: 'formFill', fields: { '#small': 'checked' } });
  assert.match(badRadio.results['#small'].error, /^Invalid radio value: "checked"/);
  assert.deepEqual([small.checked, medium.checked], [false, true]);
}

testFormFill().then(() => console.log('Form fill checks passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
