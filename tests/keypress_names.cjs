// Run with: node tests/keypress_names.cjs (no browser or npm dependencies).
// #173: key names become matching key/code/legacy fields without changing text insertion.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class KeyboardEvent extends Event {
  constructor(type, init) {
    super(type, init);
    for (const name of ['key', 'code', 'keyCode', 'which', 'ctrlKey', 'shiftKey', 'altKey', 'metaKey']) {
      this[name] = init[name];
    }
  }
}

class Element extends EventTarget {
  constructor(tagName) {
    super();
    Object.assign(this, { tagName, events: [] });
    for (const type of ['keydown', 'keypress', 'keyup', 'input']) {
      this.addEventListener(type, event => this.events.push(event));
    }
  }
}
class HTMLInputElement extends Element {
  constructor() { super('INPUT'); this._value = ''; }
  get value() { return this._value; }
  set value(value) { this._value = value; }
}
class HTMLTextAreaElement extends HTMLInputElement {
  constructor() { super(); this.tagName = 'TEXTAREA'; }
  get value() { return super.value; }
  set value(value) { super.value = value; }
}

const input = new HTMLInputElement();
const textarea = new HTMLTextAreaElement();
const editor = Object.assign(new Element('DIV'), { isContentEditable: true });
const commands = [];
const document = {
  body: new Element('BODY'), activeElement: input,
  execCommand: (...args) => commands.push(args),
};
let listener;
const context = vm.createContext({
  console, crypto: webcrypto, Event, KeyboardEvent, HTMLInputElement, HTMLTextAreaElement,
  document, window: {},
  browser: { runtime: {
    onMessage: { addListener: fn => { listener = fn; } },
    sendMessage: () => Promise.resolve(null),
  } },
});
vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
const send = msg => new Promise(resolve => listener({ action: 'keypress', ...msg }, {},
  result => resolve(JSON.parse(JSON.stringify(result)))));

(async () => {
  for (const [name, key, code, keyCode] of [
    ['Space', ' ', 'Space', 32], ['Spacebar', ' ', 'Space', 32], [' ', ' ', 'Space', 32],
    ['Esc', 'Escape', 'Escape', 27], ['Escape', 'Escape', 'Escape', 27],
    ['Return', 'Enter', 'Enter', 13], ['Enter', 'Enter', 'Enter', 13],
    ['Up', 'ArrowUp', 'ArrowUp', 38], ['Down', 'ArrowDown', 'ArrowDown', 40],
    ['Left', 'ArrowLeft', 'ArrowLeft', 37], ['Right', 'ArrowRight', 'ArrowRight', 39],
    ['ArrowUp', 'ArrowUp', 'ArrowUp', 38], ['ArrowDown', 'ArrowDown', 'ArrowDown', 40],
    ['ArrowLeft', 'ArrowLeft', 'ArrowLeft', 37], ['ArrowRight', 'ArrowRight', 'ArrowRight', 39],
    ['Tab', 'Tab', 'Tab', 9], ['Backspace', 'Backspace', 'Backspace', 8],
    ['Delete', 'Delete', 'Delete', 46], ['Home', 'Home', 'Home', 36], ['End', 'End', 'End', 35],
    ['PageUp', 'PageUp', 'PageUp', 33], ['PageDown', 'PageDown', 'PageDown', 34],
    ['Insert', 'Insert', 'Insert', 45], ['Shift', 'Shift', 'ShiftLeft', 16],
    ['Control', 'Control', 'ControlLeft', 17], ['Alt', 'Alt', 'AltLeft', 18], ['Meta', 'Meta', 'MetaLeft', 91],
    ['a', 'a', 'KeyA', 65], ['Z', 'Z', 'KeyZ', 90], ['0', '0', 'Digit0', 48], ['9', '9', 'Digit9', 57],
    ['?', '?', '', 0], ['한', '한', '', 0], ['UnknownKey', 'UnknownKey', '', 0],
    ['constructor', 'constructor', '', 0],
  ]) {
    input.events = [];
    const result = await send({ key: name });
    assert.deepEqual(result, { ok: true, key: name, target: 'input' }, 'keep the response key as supplied');
    assert.deepEqual(input.events.map(event => event.type), ['keydown', 'keyup']);
    for (const event of input.events) {
      assert.deepEqual([event.key, event.code, event.keyCode, event.which], [key, code, keyCode, keyCode], name);
      assert.ok(event.bubbles && event.cancelable && event.composed);
    }
    assert.equal(input.value, '', 'key alone does not insert text');
  }

  input.events = [];
  await send({ key: 'a', modifiers: { ctrl: true, shift: true, alt: true, meta: true } });
  for (const event of input.events) {
    assert.deepEqual([event.key, event.code, event.ctrlKey, event.shiftKey, event.altKey, event.metaKey],
      ['a', 'KeyA', true, true, true, true], 'modifiers do not rewrite the supplied character');
  }

  for (const target of [input, textarea]) {
    document.activeElement = target;
    target.events = [];
    target.value = 'existing';
    assert.equal((await send({ key: 'Space', text: ' appended' })).ok, true);
    assert.equal(target.value, 'existing appended');
    assert.deepEqual(target.events.map(event => event.type), ['keydown', 'keypress', 'input', 'keyup']);
    for (const event of target.events.filter(event => event.type !== 'input')) {
      assert.deepEqual([event.key, event.code, event.keyCode, event.which], [' ', 'Space', 32, 32]);
    }
  }
  document.activeElement = editor;
  assert.equal((await send({ key: 'Return', text: 'at the caret' })).ok, true);
  assert.deepEqual(commands, [['insertText', false, 'at the caret']], 'keep contenteditable insertion via execCommand');
  assert.deepEqual(editor.events.map(event => event.type), ['keydown', 'keypress', 'keyup']);
  assert.ok(editor.events.every(event => event.key === 'Enter' && event.keyCode === 13 && event.which === 13));

  console.log('Keypress name checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
