// Run with: node tests/playwright_actions.cjs (no browser or npm dependencies).
// Content-script support for Playwright MCP's core tool arguments: click button/modifiers,
// several select values, evaluate on an element, snapshot boxes and data drops.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    for (const key of ['clientX', 'clientY', 'detail', 'button', 'buttons', 'ctrlKey', 'shiftKey', 'altKey', 'metaKey']) {
      this[key] = init[key];
    }
  }
}

class DragEvent extends MouseEvent {
  constructor(type, init = {}) { super(type, init); this.dataTransfer = init.dataTransfer; }
}

class Element extends EventTarget {
  constructor(tagName, id, props = {}) {
    super();
    Object.assign(this, { tagName, id, textContent: id, isConnected: true, dispatched: [], ...props });
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'auxclick', 'contextmenu',
      'dblclick', 'input', 'change', 'dragenter', 'dragover', 'drop']) {
      this.addEventListener(type, event => this.dispatched.push(event));
    }
  }
  get types() { return this.dispatched.map(event => event.type); }
  matches() { return false; }
  getAttribute() { return null; }
  hasAttribute() { return false; }
  closest() { return null; }
  querySelector() { return null; }
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 10.4, y: 20.6, width: 100, height: 30 }; }
  click() { this.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); }
}

class DataTransfer {
  constructor() { this.data = {}; this.items = { add: () => {} }; this.files = []; }
  setData(type, value) { this.data[type] = value; }
}

(async () => {
  const button = new Element('BUTTON', 'go');
  const options = ['free', 'pro', 'team'].map(value => ({ value, text: value.toUpperCase(), selected: false }));
  const multi = new Element('SELECT', 'plans', { multiple: true, options, value: '' });
  const single = new Element('SELECT', 'plan', { multiple: false, value: 'free',
    options: options.map(option => ({ ...option })) });
  const zone = new Element('DIV', 'zone');
  const elements = { '#go': button, '#plans': multi, '#plan': single, '#zone': zone };
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, PointerEvent: MouseEvent, DragEvent, DataTransfer,
    CSS: { escape: x => x }, navigator: { platform: 'MacIntel' }, setTimeout,
    window: { PointerEvent: MouseEvent, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: {
      body: {},
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
    },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } }, sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  // Results come from the vm realm; compare them as plain JSON.
  const plain = value => JSON.parse(JSON.stringify(value));
  const send = msg => new Promise(resolve => listener(msg, {}, resolve)).then(plain);

  // Right and middle buttons fire contextmenu/auxclick in place of click, with the button held.
  for (const [button_, event, index, held] of [['right', 'contextmenu', 2, 2], ['middle', 'auxclick', 1, 4]]) {
    button.dispatched = [];
    const result = await send({ action: 'click', selector: '#go', button: button_ });
    assert.equal(result.ok, true);
    assert.equal(result.button, button_);
    assert.deepEqual(button.types, ['pointerdown', 'mousedown', 'pointerup', 'mouseup', event]);
    assert.equal(button.dispatched[0].button, index);
    assert.equal(button.dispatched[0].buttons, held);
  }
  assert.match((await send({ action: 'click', selector: '#go', button: 'side' })).error, /left, middle or right/);

  // Modifiers set the event flags; ControlOrMeta is Meta on macOS.
  button.dispatched = [];
  await send({ action: 'click', selector: '#go', modifiers: ['Shift', 'ControlOrMeta'] });
  const click = button.dispatched.find(event => event.type === 'click');
  assert.deepEqual([click.shiftKey, click.metaKey, click.ctrlKey, click.altKey], [true, true, false, false]);
  button.dispatched = [];
  await send({ action: 'doubleClick', selector: '#go', modifiers: ['Alt'] });
  assert.equal(button.dispatched.find(event => event.type === 'dblclick').altKey, true);

  // Several values select each option by value, else by its text.
  let result = await send({ action: 'selectOption', selector: '#plans', values: ['pro', 'TEAM'] });
  assert.deepEqual(result.values, ['pro', 'team']);
  assert.deepEqual(multi.options.map(option => option.selected), [false, true, true]);
  assert.deepEqual(multi.types.slice(-2), ['change', 'input']);
  result = await send({ action: 'selectOption', selector: '#plan', values: ['PRO'] });
  assert.equal(single.value, 'pro');
  assert.match((await send({ action: 'selectOption', selector: '#plan', values: ['free', 'pro'] })).error,
    /takes one value/);
  assert.match((await send({ action: 'selectOption', selector: '#plans', values: ['gold'] })).error,
    /No matching option/);
  // A single value without values keeps the old behavior.
  assert.equal((await send({ action: 'selectOption', selector: '#plan', value: 'team' })).value, 'team');

  // browser_evaluate: the target is __kitsuneElement and a promise result is awaited.
  result = await send({ action: 'executeJS', selector: '#go',
    code: "Promise.resolve(((el) => el.id)(__kitsuneElement)).then(value => JSON.stringify(value))" });
  assert.deepEqual(result, { result: '"go"' });
  assert.equal((await send({ action: 'executeJS', code: 'Promise.reject(new Error("nope"))' })).error, 'nope');
  assert.equal((await send({ action: 'executeJS', code: '1 + 1' })).result, '2');
  assert.equal((await send({ action: 'executeJS', selector: '#missing', code: '1' })).code, 'not_found');

  // browser_drop: data alone, with no file chunks, drops on the target.
  zone.dispatched = [];
  result = await send({ action: 'uploadCommit', uploadId: 'u1', selector: '#zone', files: [], mode: 'drop',
    data: { 'text/plain': 'hello' } });
  assert.equal(result.ok, true);
  assert.deepEqual(zone.types, ['dragenter', 'dragover', 'drop']);
  assert.deepEqual(plain(zone.dispatched[2].dataTransfer.data), { 'text/plain': 'hello' });
  assert.match((await send({ action: 'uploadCommit', uploadId: 'u2', selector: '#zone',
    files: [{ name: 'a', start: 0, end: 1 }], mode: 'drop' })).error, /chunks never arrived/);

  console.log('playwright actions ok');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
