// Run with: node tests/key_defaults.cjs (no browser or npm dependencies).
// #267: a key applies its default edit in an input, textarea or editor, unless the page
// cancels it, and the result says what changed. #257: Enter in a text input submits its
// form as a browser's implicit submission does. Checked on the fields' actual state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class KeyboardEvent extends Event {
  constructor(type, init) {
    super(type, init);
    for (const name of ['key', 'code', 'ctrlKey', 'shiftKey', 'altKey', 'metaKey']) this[name] = init[name];
  }
}
class InputEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    Object.assign(this, { inputType: init.inputType, data: init.data });
  }
}

// Events as "type" or "type:inputType", with what the page saw at each.
class Node extends EventTarget {
  constructor(tagName, props = {}) {
    super();
    Object.assign(this, { tagName, isConnected: true, focused: 0, events: [], attrs: {} }, props);
    for (const type of ['keydown', 'keypress', 'beforeinput', 'input', 'keyup']) {
      this.addEventListener(type, event => this.events.push(event.inputType ? `${type}:${event.inputType}` : type));
    }
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  matches() { return false; }
  closest() { return null; }
  focus(options) { this.focused++; this.focusOptions = options; document.activeElement = this; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}

// An input or textarea with the selection API (setSelectionRange, setRangeText).
class Field extends Node {
  constructor(tagName, value = '', props = {}) {
    super(tagName, { type: tagName === 'INPUT' ? 'text' : 'textarea', value, maxLength: -1, readOnly: false,
      form: null, ...props });
    if (this.selectionStart !== null) this.setSelectionRange(value.length, value.length);
  }
  setSelectionRange(start, end, direction = 'none') {
    Object.assign(this, { selectionStart: start, selectionEnd: end, selectionDirection: direction });
  }
  setRangeText(text, start, end, mode) {
    assert.equal(mode, 'end');
    this.value = this.value.slice(0, start) + text + this.value.slice(end);
    this.setSelectionRange(start + text.length, start + text.length);
  }
}

// A form owns controls (their form property); tree is every control in its document, in
// tree order, including ones another form owns (#288).
class Form {
  constructor(controls, tree = controls) {
    Object.assign(this, { controls, tree, submits: 0 });
    for (const control of controls) control.form = this;
  }
  getRootNode() {
    return { querySelectorAll: selector => {
      const tags = selector.split(',').map(tag => tag.trim().toUpperCase());
      return this.tree.filter(control => tags.includes(control.tagName));
    } };
  }
  requestSubmit() { this.submits++; }
}

const button = (props = {}) => Object.assign(new Node('BUTTON', { type: 'submit', clicks: 0, ...props }),
  { click() { this.clicks++; } });

// The editor's selection keeps its points; modify moves the focus by one step per call, and
// execCommand records what ran and whether it changed the editor (for MutationObserver).
const editor = new Node('DIV', { isContentEditable: true, contentEditable: 'true', parentElement: null,
  contains: node => node === editor });
const outside = new Node('P');
const selection = {
  anchorNode: editor, anchorOffset: 3, focusNode: editor, focusOffset: 3, rangeCount: 1, modified: [],
  get isCollapsed() { return this.anchorNode === this.focusNode && this.anchorOffset === this.focusOffset; },
  getRangeAt() {
    const [start, end] = [this.anchorOffset, this.focusOffset].sort((a, b) => a - b);
    return { startContainer: this.anchorNode, endContainer: this.focusNode, startOffset: start, endOffset: end,
      cloneRange() { return this; } };
  },
  removeAllRanges() { this.rangeCount = 0; },
  addRange(range) {
    Object.assign(this, { rangeCount: 1, anchorNode: range.startContainer, anchorOffset: range.startOffset,
      focusNode: range.endContainer, focusOffset: range.endOffset });
  },
  setBaseAndExtent(anchorNode, anchorOffset, focusNode, focusOffset) {
    Object.assign(this, { anchorNode, anchorOffset, focusNode, focusOffset });
  },
  modify(alter, direction, unit) {
    this.modified.push([alter, direction, unit]);
    if (this.leaves) this.focusNode = outside;
    this.focusOffset = Math.max(0, Math.min(6, this.focusOffset + (direction === 'backward' ? -1 : 1)));
    if (alter === 'move') this.anchorOffset = this.focusOffset;
  },
  collapseToStart() { this.focusOffset = this.anchorOffset = Math.min(this.anchorOffset, this.focusOffset); },
  collapseToEnd() { this.focusOffset = this.anchorOffset = Math.max(this.anchorOffset, this.focusOffset); },
};
class Range {
  selectNodeContents(node) { Object.assign(this, { startContainer: node, startOffset: 0, endContainer: node, endOffset: 6 }); }
  collapse(toStart) {
    if (toStart) this.endOffset = this.startOffset;
    else this.startOffset = this.endOffset;
  }
}
const commands = [];
let mutations = 0;
let changes = true;
class MutationObserver {
  observe() { this.seen = mutations; }
  takeRecords() { return mutations > this.seen ? [{}] : []; }
  disconnect() {}
}

const COMMAND_INPUT_TYPES = { delete: 'deleteContentBackward', forwardDelete: 'deleteContentForward' };
let document;
// The content script on a platform: navigator.platform picks macOS's or the others' modifiers.
function load(platform) {
  document = {
    body: new Node('BODY'), activeElement: null,
    createRange: () => new Range(),
    // As in Firefox: beforeinput at the editor, which the page can cancel, then the edit and
    // input; true even when the page cancelled it (#289).
    execCommand(command, ui, value) {
      commands.push(value === undefined ? command : [command, value]);
      const inputType = COMMAND_INPUT_TYPES[command] || command;
      if (!editor.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType }))) return true;
      if (changes) {
        mutations++;
        editor.dispatchEvent(new InputEvent('input', { bubbles: true, inputType }));
      }
      return true;
    },
  };
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, KeyboardEvent, InputEvent, MutationObserver, Intl, document,
    navigator: { platform },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getSelection: () => selection },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  // Keys go to the focused element, as browser_press_key sends them.
  return (target, key, modifiers) => {
    document.activeElement = target;
    target.events = [];
    commands.length = 0;
    return new Promise(resolve => listener({ action: 'keypress', key, ...(modifiers ? { modifiers } : {}) }, {},
      result => resolve(JSON.parse(JSON.stringify(result)))));
  };
}
const state = field => [field.value, field.selectionStart, field.selectionEnd];

(async () => {
  let press = load('Linux x86_64');

  // 1. The issue's diagnostic: abcdef with the caret at the end, each key from a fresh field.
  const fresh = () => new Field('INPUT', 'abcdef');
  let input = fresh();
  assert.deepEqual(await press(input, 'Backspace'),
    { ok: true, key: 'Backspace', target: 'input', effect: 'deleteContentBackward' });
  assert.deepEqual(state(input), ['abcde', 5, 5]);
  assert.deepEqual(input.events, ['keydown', 'beforeinput:deleteContentBackward', 'input:deleteContentBackward', 'keyup']);
  input = fresh();
  assert.equal((await press(input, 'ArrowLeft')).effect, 'moveCaret');
  assert.deepEqual(state(input), ['abcdef', 5, 5]);
  input = fresh();
  assert.deepEqual(await press(input, 'ArrowLeft', { shift: true }),
    { ok: true, key: 'Shift+ArrowLeft', target: 'input', effect: 'extendSelection' });
  assert.deepEqual([...state(input), input.selectionDirection], ['abcdef', 5, 6, 'backward'], 'f is selected');
  input = fresh();
  assert.equal((await press(input, 'Space')).effect, 'insertText');
  assert.deepEqual(state(input), ['abcdef ', 7, 7]);
  assert.deepEqual(input.events, ['keydown', 'keypress', 'beforeinput:insertText', 'input:insertText', 'keyup']);

  // Negative controls: End at the end, and Enter in an input outside a form, change nothing.
  input = fresh();
  assert.deepEqual(await press(input, 'End'), { ok: true, key: 'End', target: 'input', effect: null });
  assert.deepEqual(await press(input, 'Enter'), { ok: true, key: 'Enter', target: 'input', effect: null });
  assert.deepEqual(state(input), ['abcdef', 6, 6]);

  // 2. Characters, Shift, selections and boundaries.
  input = fresh();
  await press(input, 'x');
  await press(input, 'y', { shift: true });
  await press(input, '한');
  assert.deepEqual(state(input), ['abcdefxY한', 9, 9]);
  input = fresh();
  await press(input, 'ArrowLeft', { shift: true });
  await press(input, 'ArrowLeft', { shift: true });
  assert.deepEqual(state(input), ['abcdef', 4, 6]);
  await press(input, 'ArrowRight', { shift: true });
  assert.deepEqual([...state(input), input.selectionDirection], ['abcdef', 5, 6, 'backward']);
  await press(input, 'x');
  assert.deepEqual(state(input), ['abcdex', 6, 6], 'typing replaces the selection');
  input.setSelectionRange(1, 3);
  await press(input, 'ArrowLeft');
  assert.deepEqual(state(input), ['abcdex', 1, 1], 'an arrow collapses a selection to its start');
  input.setSelectionRange(1, 3);
  await press(input, 'ArrowRight');
  assert.deepEqual(state(input), ['abcdex', 3, 3], 'or to its end');
  input.setSelectionRange(1, 3);
  assert.equal((await press(input, 'Backspace')).effect, 'deleteContentBackward');
  assert.deepEqual(state(input), ['adex', 1, 1], 'Backspace removes the selection');
  await press(input, 'Home');
  assert.deepEqual(await press(input, 'Backspace'), { ok: true, key: 'Backspace', target: 'input', effect: null });
  assert.equal((await press(input, 'Delete')).effect, 'deleteContentForward');
  assert.deepEqual(state(input), ['dex', 0, 0]);
  assert.equal((await press(input, 'a', { ctrl: true })).effect, 'selectAll');
  assert.deepEqual(state(input), ['dex', 0, 3]);

  // Words with Control off macOS; graphemes stay whole.
  input = new Field('INPUT', 'hello big world');
  assert.equal((await press(input, 'ArrowLeft', { ctrl: true })).effect, 'moveCaret');
  assert.equal(input.selectionStart, 10);
  await press(input, 'End');
  assert.deepEqual(await press(input, 'Backspace', { ctrl: true }),
    { ok: true, key: 'Control+Backspace', target: 'input', effect: 'deleteWordBackward' });
  assert.deepEqual(state(input), ['hello big ', 10, 10]);
  input = new Field('INPUT', 'ok 👍🏽');
  await press(input, 'Backspace');
  assert.deepEqual(state(input), ['ok ', 3, 3], 'an emoji with a skin tone is one character');

  // Read-only fields move the caret but do not change; maxlength caps typing.
  input = new Field('INPUT', 'abc', { readOnly: true });
  assert.equal((await press(input, 'Backspace')).effect, null);
  assert.equal((await press(input, 'ArrowLeft')).effect, 'moveCaret');
  assert.deepEqual(state(input), ['abc', 2, 2]);
  input = new Field('INPUT', 'abc', { maxLength: 3 });
  assert.equal((await press(input, 'x')).effect, null);
  assert.equal(input.value, 'abc');

  // An email input has no selection: keys edit at its end and do not move a caret.
  input = new Field('INPUT', 'a@b', { type: 'email', selectionStart: null, selectionEnd: null });
  await press(input, 'c');
  assert.equal(input.value, 'a@bc');
  await press(input, 'Backspace');
  assert.equal(input.value, 'a@b');
  assert.equal((await press(input, 'ArrowLeft')).effect, null);

  // 3. The page's handlers come first: a cancelled keydown, keypress or beforeinput edits nothing.
  for (const type of ['keydown', 'keypress', 'beforeinput']) {
    input = fresh();
    input.addEventListener(type, event => event.preventDefault());
    const result = await press(input, type === 'keypress' ? 'x' : 'Backspace');
    assert.deepEqual([result.effect, result.prevented], [null, true], type);
    assert.deepEqual(state(input), ['abcdef', 6, 6], type);
    assert.equal(input.events.at(-1), 'keyup', 'keyup still follows');
    assert.ok(!input.events.some(event => event.startsWith('input')), type);
  }

  // 4. A textarea: Enter adds a line; Home, Up and Down follow its lines; Control+End its end.
  const textarea = new Field('TEXTAREA', 'ab\ncd');
  assert.equal((await press(textarea, 'Home')).effect, 'moveCaret');
  assert.equal(textarea.selectionStart, 3);
  await press(textarea, 'End');
  await press(textarea, 'ArrowUp');
  assert.equal(textarea.selectionStart, 2);
  await press(textarea, 'ArrowUp');
  assert.equal(textarea.selectionStart, 0, 'Up on the first line goes to the start');
  await press(textarea, 'ArrowDown');
  assert.equal(textarea.selectionStart, 3);
  await press(textarea, 'End', { ctrl: true });
  assert.equal((await press(textarea, 'Enter')).effect, 'insertLineBreak');
  assert.deepEqual(state(textarea), ['ab\ncd\n', 6, 6]);

  // 5. Enter in a text input submits its form (#257): through the first submit control, or
  //    requestSubmit when the form has none and no other text input; a disabled control or a
  //    second text input blocks it.
  let submit = button();
  let field = new Field('INPUT', 'query');
  let form = new Form([field, submit]);
  assert.deepEqual(await press(field, 'Enter'), { ok: true, key: 'Enter', target: 'input', effect: 'implicitSubmit' });
  assert.deepEqual([submit.clicks, form.submits], [1, 0]);
  assert.deepEqual(field.events, ['keydown', 'keypress', 'keyup']);
  submit.disabled = true;
  assert.equal((await press(field, 'Enter')).effect, null);
  assert.equal(submit.clicks, 1);
  field = new Field('INPUT', 'query');
  form = new Form([field]);
  assert.equal((await press(field, 'Enter')).effect, 'implicitSubmit');
  assert.equal(form.submits, 1);
  field = new Field('INPUT', 'query');
  form = new Form([field, new Field('INPUT', '', { type: 'password' })]);
  assert.equal((await press(field, 'Enter')).effect, null);
  assert.equal(form.submits, 0);
  // Ownership, not containment (#288): another form's button among the form's descendants
  // is not its default button, a button or text input associated from outside is, and the
  // default button is the first in tree order.
  field = new Field('INPUT', 'query');
  let foreign = button();
  form = new Form([field], [field, foreign]);
  new Form([foreign], [field, foreign]);
  assert.equal((await press(field, 'Enter')).effect, 'implicitSubmit');
  assert.deepEqual([foreign.clicks, form.submits], [0, 1]);
  field = new Field('INPUT', 'query');
  const external = button();
  const later = button();
  form = new Form([field, external, later], [foreign, field, external, later]);
  assert.equal((await press(field, 'Enter')).effect, 'implicitSubmit');
  assert.deepEqual([foreign.clicks, external.clicks, later.clicks, form.submits], [0, 1, 0, 0]);
  field = new Field('INPUT', 'query');
  form = new Form([field, new Field('INPUT', '')]);
  assert.equal((await press(field, 'Enter')).effect, null, 'an associated text input blocks it');
  assert.equal(form.submits, 0);
  field = new Field('INPUT', 'query');
  const image = new Node('INPUT', { type: 'image', clicks: 0, click() { this.clicks++; } });
  form = new Form([field, image]);
  assert.equal((await press(field, 'Enter')).effect, 'implicitSubmit');
  assert.deepEqual([image.clicks, form.submits], [1, 0], 'an image input is a submit control');
  // A page that handles Enter itself and cancels it is not submitted as well.
  submit = button();
  field = new Field('INPUT', 'query');
  new Form([field, submit]);
  field.addEventListener('keydown', event => { if (event.key === 'Enter') event.preventDefault(); });
  assert.equal((await press(field, 'Enter')).prevented, true);
  assert.equal(submit.clicks, 0);

  // 6. Defaults Kitsune does not apply are named, not implied.
  input = fresh();
  assert.match((await press(input, 'Tab')).note, /does not move focus/);
  assert.match((await press(input, 'c', { ctrl: true })).note, /clipboard or undo/);
  assert.match((await press(document.body, 'PageDown')).note, /does not scroll/);
  const go = button({ closest: () => go });
  assert.match((await press(go, 'Enter')).note, /click it instead/);
  assert.equal(go.clicks, 0);
  assert.deepEqual(state(input), ['abcdef', 6, 6]);

  // 7. An editor edits through editing commands at its selection, and its caret moves with
  //    Selection.modify without leaving it.
  assert.deepEqual(await press(editor, 'Backspace'),
    { ok: true, key: 'Backspace', target: 'div', effect: 'deleteContentBackward' });
  assert.deepEqual(commands, ['delete']);
  assert.equal((await press(editor, 'Delete')).effect, 'deleteContentForward');
  assert.deepEqual(commands, ['forwardDelete']);
  assert.equal((await press(editor, 'Enter')).effect, 'insertParagraph');
  assert.deepEqual(commands, ['insertParagraph']);
  assert.equal((await press(editor, 'Enter', { shift: true })).effect, 'insertLineBreak');
  assert.deepEqual(commands, ['insertLineBreak']);
  assert.equal((await press(editor, 'x')).effect, 'insertText');
  assert.deepEqual(commands, [['insertText', 'x']]);
  assert.deepEqual(editor.events, ['keydown', 'keypress', 'beforeinput:insertText', 'input:insertText', 'keyup']);
  selection.modified = [];
  assert.equal((await press(editor, 'Backspace', { ctrl: true })).effect, 'deleteWordBackward');
  assert.deepEqual([selection.modified, commands], [[['extend', 'backward', 'word']], ['delete']]);
  // A command that changes nothing (Backspace at the start) has no effect, and was not
  // cancelled; an edit the page cancels in beforeinput is reported as prevented (#289).
  changes = false;
  assert.deepEqual(await press(editor, 'Backspace'), { ok: true, key: 'Backspace', target: 'div', effect: null });
  assert.deepEqual(await press(editor, 'x'), { ok: true, key: 'x', target: 'div', effect: null });
  changes = true;
  const refuse = event => event.preventDefault();
  editor.addEventListener('beforeinput', refuse);
  for (const key of ['x', 'Backspace', 'Enter']) {
    const before = mutations;
    assert.deepEqual(await press(editor, key), { ok: true, key, target: 'div', effect: null, prevented: true }, key);
    assert.equal(mutations, before, key);
    assert.equal(commands.length, 1, `${key} is not applied another way`);
    assert.ok(!editor.events.some(event => event.startsWith('input')), key);
  }
  editor.removeEventListener('beforeinput', refuse);

  selection.setBaseAndExtent(editor, 3, editor, 3);
  selection.modified = [];
  assert.equal((await press(editor, 'ArrowLeft')).effect, 'moveCaret');
  assert.equal((await press(editor, 'ArrowRight', { shift: true })).effect, 'extendSelection');
  assert.deepEqual(selection.modified, [['move', 'backward', 'character'], ['extend', 'forward', 'character']]);
  assert.deepEqual([selection.anchorOffset, selection.focusOffset], [2, 3]);
  await press(editor, 'ArrowLeft');
  assert.deepEqual([selection.anchorOffset, selection.focusOffset], [2, 2], 'collapses the selection to its start');
  await press(editor, 'End', { ctrl: true });
  assert.deepEqual([selection.anchorOffset, selection.focusOffset], [6, 6]);
  assert.equal((await press(editor, 'a', { ctrl: true })).effect, 'selectAll');
  assert.deepEqual([selection.anchorOffset, selection.focusOffset], [0, 6]);
  selection.setBaseAndExtent(editor, 3, editor, 3);
  selection.leaves = true;
  assert.equal((await press(editor, 'ArrowUp')).effect, null, 'a caret key does not leave the editor');
  assert.deepEqual([selection.focusNode, selection.focusOffset], [editor, 3]);
  selection.leaves = false;

  // 8. macOS: Alt moves by words, Meta to the line boundary, and Meta+A selects all.
  press = load('MacIntel');
  input = new Field('INPUT', 'hello big world');
  assert.equal((await press(input, 'Backspace', { alt: true })).effect, 'deleteWordBackward');
  assert.equal(input.value, 'hello big ');
  assert.equal((await press(input, 'ArrowLeft', { meta: true })).effect, 'moveCaret');
  assert.equal(input.selectionStart, 0);
  await press(input, 'End');
  assert.deepEqual(await press(input, 'Backspace', { meta: true }),
    { ok: true, key: 'Meta+Backspace', target: 'input', effect: 'deleteSoftLineBackward' });
  assert.equal(input.value, '');
  input = fresh();
  assert.equal((await press(input, 'a', { meta: true })).effect, 'selectAll');
  assert.equal((await press(input, 'a', { ctrl: true })).effect, null);

  console.log('Key default checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
