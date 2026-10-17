// Run with: node tests/click_events.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    Object.assign(this, { clientX: init.clientX, clientY: init.clientY, detail: init.detail || 0 });
  }
}

class Element extends EventTarget {
  constructor(tagName, id, type) {
    super();
    Object.assign(this, { tagName, id, type, textContent: id, isConnected: true,
      checked: false, nativeClicks: 0, events: [] });
    for (const type of ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'dblclick', 'input', 'change']) {
      this.addEventListener(type, event => this.events.push(event.type));
    }
  }
  getAttribute() { return null; }
  hasAttribute() { return false; }
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
  click() {
    this.nativeClicks++;
    // Model native activation as well as the click listener invocation.
    if (this.type === 'checkbox') this.checked = !this.checked;
    this.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
  }
}

async function testClickEvents(pointerEvents) {
  const button = new Element('BUTTON', 'go');
  const checkbox = new Element('INPUT', 'toggle', 'checkbox');
  const child = new Element('SPAN', 'child');
  child.parentElement = button;
  const form = new Element('FORM', 'form');
  form.querySelector = () => button;
  const elements = { '#go': button, '#toggle': checkbox, '#form': form };
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, PointerEvent: MouseEvent,
    CSS: { escape: x => x },
    window: { PointerEvent: pointerEvents ? MouseEvent : undefined,
      getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: {
      body: {},
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
      elementFromPoint: () => child,
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve));
  const ref = (await send({ action: 'find', query: '#go' })).results[0].ref;
  const clickSequence = ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
  for (const target of [{ selector: '#go' }, { selector: ref }, { coords: { x: 20, y: 10 } }]) {
    button.events = [];
    const nativeClicks = button.nativeClicks;
    assert.equal((await send({ action: 'click', ...target })).ok, true);
    assert.equal(button.events.filter(type => type === 'click').length, 1, 'one click handler invocation per action');
    assert.equal(button.nativeClicks, nativeClicks + 1, 'retain native activation');
    assert.deepEqual(button.events, clickSequence);
  }

  for (const checked of [true, false]) {
    checkbox.events = [];
    assert.equal((await send({ action: 'click', selector: '#toggle' })).ok, true);
    assert.equal(checkbox.checked, checked, 'one native checkbox toggle per click');
    assert.equal(checkbox.events.filter(type => type === 'click').length, 1);
  }

  button.events = [];
  const nativeClicks = button.nativeClicks;
  assert.equal((await send({ action: 'doubleClick', selector: '#go' })).ok, true);
  assert.deepEqual(button.events, ['mousedown', 'mouseup', 'click', 'mousedown', 'mouseup', 'click', 'dblclick']);
  assert.equal(button.nativeClicks, nativeClicks, 'doubleClick does not add native clicks to its synthetic clicks');

  for (const checked of [true, false]) {
    checkbox.events = [];
    const nativeClicks = checkbox.nativeClicks;
    const result = await send({ action: 'checkBox', selector: '#toggle', checked });
    assert.equal(result.ok, true);
    assert.equal(result.checked, checked);
    assert.equal(checkbox.checked, checked);
    assert.deepEqual(checkbox.events, ['input', 'change']);
    assert.equal(checkbox.nativeClicks, nativeClicks, 'checkBox sets state without extra clicks');
  }

  button.events = [];
  assert.equal((await send({ action: 'submitForm', selector: '#form' })).ok, true);
  assert.deepEqual(button.events, ['click'], 'submit button activates once');
  assert.equal(button.nativeClicks, nativeClicks + 1);
}

(async () => {
  await testClickEvents(true);
  await testClickEvents(false);
  console.log('Click event counts and native activation checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
