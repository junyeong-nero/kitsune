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
  assert.equal(mixed.error, undefined, 'a partial fill is not a failed call');

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
  assert.equal(uncheck.error, 'No field was filled', 'a call that wrote nothing fails as a whole');

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
  assert.equal(badRadio.error, 'No field was filled');
  assert.equal((await send({ action: 'formFill', fields: { '#missing': 'x' } })).error, 'No field was filled');
  assert.deepEqual([small.checked, medium.checked], [false, true]);
}

// #460: settleState tells the background whether the action keeps the settle floor.
async function testSettleFloor() {
  const field = (attrs = {}) => Object.assign(new Element('INPUT', attrs), { type: 'text', value: '' });
  const plan = new SelectElement(['free', 'pro']);
  const agree = Object.assign(new Element('INPUT'), { type: 'checkbox', checked: false });
  const elements = { '#name': field(), '#none': field({ 'aria-autocomplete': 'none' }),
    '#city': field({ role: 'combobox' }), '#tags': field({ 'aria-autocomplete': 'list' }),
    '#color': field({ list: 'colors' }), '#plan': plan, '#agree': agree };
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, CSS: { escape: x => x },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: { body: {}, querySelector: selector => elements[selector] || null, querySelectorAll: () => [] },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
  // ok=false: the action may fail in this DOM, which has no keyboard events; its watch still started.
  const floorAfter = async (msg, settle = { deltaBaseline: true }, ok = true) => {
    const result = await send({ ...msg, ...settle });
    if (ok) assert.equal(result.ok, true, JSON.stringify(msg));
    return (await send({ action: 'settleState' })).floor;
  };

  // Plain field writes: fill without slowly, a native select, a check.
  assert.equal(await floorAfter({ action: 'fill', selector: '#name', value: 'Ada' }), false);
  assert.equal(await floorAfter({ action: 'fill', selector: '#none', value: 'Ada' }), false, 'aria-autocomplete=none');
  assert.equal(await floorAfter({ action: 'selectOption', selector: '#plan', value: 'pro' }), false);
  assert.equal(await floorAfter({ action: 'checkBox', selector: '#agree', checked: true }), false);
  // Fields whose reaction is likely debounced, typing and keys keep it.
  for (const selector of ['#city', '#tags', '#color']) {
    assert.equal(await floorAfter({ action: 'fill', selector, value: 'Al' }), true, selector);
  }
  assert.equal(await floorAfter({ action: 'type', selector: '#name', text: 'Al' }, undefined, false), true, 'typing slowly');
  assert.equal(await floorAfter({ action: 'keypress', selector: '#name', key: 'a' }, undefined, false), true, 'a key');

  // browser_fill_form: the first field records the baseline, the last one settles. A field that
  // needs the floor keeps it for the whole call; the next call's baseline starts over.
  assert.equal((await send({ action: 'fill', selector: '#city', value: 'Seoul', deltaBaseline: true })).ok, true);
  assert.equal((await send({ action: 'fill', selector: '#name', value: 'Ada' })).ok, true);
  assert.equal(await floorAfter({ action: 'checkBox', selector: '#agree', checked: false }, { settleWatch: true }), true,
    'mixed fields');
  assert.equal((await send({ action: 'fill', selector: '#name', value: 'Grace', deltaBaseline: true })).ok, true);
  assert.equal(await floorAfter({ action: 'selectOption', selector: '#plan', value: 'free' }, { settleWatch: true }),
    false, 'plain fields');
}

// Mirrors the DOM: a single-line input's value sanitization strips line breaks.
class TextInputElement extends Element {
  constructor() {
    super('INPUT');
    Object.assign(this, { type: 'text', _value: '' });
  }
  get value() { return this._value; }
  set value(value) { this._value = String(value).replace(/[\r\n]/g, ''); }
}

// #484: line breaks typed or filled into a single-line input become spaces, as Playwright MCP
// stores them; a textarea keeps them.
async function testLineBreaks() {
  const input = new TextInputElement();
  const area = Object.assign(new Element('TEXTAREA'), { value: '' });
  const elements = { '#input': input, '#area': area };
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, CSS: { escape: x => x },
    KeyboardEvent: class extends Event {},
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: { body: {}, querySelector: selector => elements[selector] || null, querySelectorAll: () => [] },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
  const reset = () => { input.value = ''; area.value = ''; };

  for (const [name, write] of [
    ['browser_type slowly', (selector, text) => send({ action: 'type', selector, text, clear: true })],
    ['browser_type fill', (selector, value) => send({ action: 'fill', selector, value })],
    ['browser_fill_form', (selector, value) => send({ action: 'formFill', fields: { [selector]: value } })],
  ]) {
    reset();
    const typed = await write('#input', 'a\nb');
    assert.equal(typed.error, undefined, `${name}: ${JSON.stringify(typed)}`);
    assert.equal(input.value, 'a b', `${name} replaces a line break in an input with a space`);
    reset();
    await write('#input', 'a\r\nb\rc\n\nd');
    assert.equal(input.value, 'a b c  d', `${name} replaces each \\r\\n, \\r or \\n with one space`);
    reset();
    await write('#area', 'a\nb');
    assert.equal(area.value, 'a\nb', `${name} keeps a line break in a textarea`);
  }

  reset();
  input.value = 'x';
  await send({ action: 'type', selector: '#input', text: '\ny', clear: false });
  assert.equal(input.value, 'x y', 'appending to an input replaces the line break too');
}

// #539: native value setters must not bypass the page's editing restrictions.
async function testRestrictedFields() {
  const field = (tagName, props = {}) => Object.assign(new Element(tagName), {
    type: 'text', value: 'before', focused: 0,
    focus() { this.focused++; },
    matches(selector) { return selector === ':disabled' && (this.disabled || this.inDisabledFieldset); },
  }, props);
  const elements = {
    '#readonly': field('INPUT', { readOnly: true }),
    '#area': field('TEXTAREA', { readOnly: true }),
    '#disabled': field('INPUT', { disabled: true }),
    '#fieldset': field('INPUT', { inDisabledFieldset: true }),
    '#editable': field('INPUT'),
    '#ignored': field('INPUT', { type: 'range', readOnly: true }),
  };
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, CSS: { escape: x => x },
    KeyboardEvent: class extends Event {},
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: { body: {}, querySelector: selector => elements[selector] || null, querySelectorAll: () => [] },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
  const writes = [
    selector => send({ action: 'fill', selector, value: 'after' }),
    selector => send({ action: 'type', selector, text: 'after', clear: true }),
    selector => send({ action: 'setEditableContent', selector, value: 'after', format: 'text', clear: true }),
    async selector => (await send({ action: 'formFill', fields: { [selector]: 'after' } })).results[selector],
  ];
  for (const selector of ['#readonly', '#area', '#disabled', '#fieldset']) {
    const el = elements[selector];
    for (const write of writes) {
      const result = await write(selector);
      assert.equal(result.code, 'not_editable', `${selector}: ${JSON.stringify(result)}`);
      assert.equal(result.retryable, false);
      assert.equal(result.state.reason, el.readOnly ? 'readonly' : 'disabled');
      assert.equal(el.value, 'before');
      assert.equal(el.focused, 0);
      assert.deepEqual(el.events, []);
    }
  }
  const mixed = await send({ action: 'formFill', fields: { '#readonly': 'wrong', '#editable': 'Ada' } });
  assert.equal(mixed.error, undefined, 'keep per-field partial success semantics');
  assert.equal(mixed.results['#readonly'].code, 'not_editable');
  assert.equal(elements['#editable'].value, 'Ada');
  assert.deepEqual(elements['#editable'].events, ['input', 'change']);
  assert.equal((await send({ action: 'fill', selector: '#ignored', value: '5' })).ok, true,
    'readonly does not apply to range inputs');
}

testFormFill().then(testSettleFloor).then(testLineBreaks).then(testRestrictedFields).then(() => console.log('Form fill checks passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
