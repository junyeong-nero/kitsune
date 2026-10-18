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

async function testFormFill() {
  const color = new SelectElement(['red', 'blue']);
  const name = Object.assign(new Element('INPUT'), { type: 'text', value: '' });
  const label = { innerText: 'Color', querySelector: () => color };
  const elements = { '#name': name, '#color': color };
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
}

testFormFill().then(() => console.log('Form fill checks passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
