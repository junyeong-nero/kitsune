// Run with: node tests/click_caret.cjs (no browser or npm dependencies).
// #159: a click focuses its target and puts the caret where it landed; typing into an editor
// inserts at the caret, or at the end of the target when the caret is elsewhere.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    Object.assign(this, { clientX: init.clientX, clientY: init.clientY });
  }
}

class InputEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    Object.assign(this, { inputType: init.inputType, data: init.data });
  }
}

class Text {
  constructor(text, parent) { Object.assign(this, { nodeType: 3, textContent: text, parentNode: parent }); }
  get length() { return this.textContent.length; }
}

class Element extends EventTarget {
  constructor(tagName, id, { editable = false, host = false, parent = null } = {}) {
    super();
    Object.assign(this, { tagName, id, isConnected: true, isContentEditable: editable,
      contentEditable: host ? 'true' : 'inherit', parentElement: parent, parentNode: parent,
      childNodes: [], firstElementChild: null, focused: 0, events: [] });
    if (parent) {
      parent.childNodes.push(this);
      parent.firstElementChild ||= this;
    }
    for (const type of ['mousedown', 'click', 'beforeinput', 'input']) {
      this.addEventListener(type, event => this.events.push(event.type));
    }
  }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  get length() { return this.childNodes.length; }
  text(value) { this.childNodes.push(new Text(value, this)); return this; }
  contains(node) {
    for (let cur = node; cur; cur = cur.parentNode) if (cur === this) return true;
    return false;
  }
  getAttribute() { return null; }
  hasAttribute() { return false; }
  matches() { return false; }
  closest(selector) {
    for (let el = this; el; el = el.parentElement) if (this.focusable?.includes(el)) return el;
    return null;
  }
  focus(options) { this.focused++; this.focusOptions = options; document.activeElement = this; }
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
  click() { this.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); }
}

// Ranges and the selection keep only their boundary points.
class Range {
  setStart(node, offset) { Object.assign(this, { startContainer: node, startOffset: offset,
    endContainer: node, endOffset: offset }); }
  selectNodeContents(node) { Object.assign(this, { startContainer: node, startOffset: 0,
    endContainer: node, endOffset: node.length }); }
  collapse(toStart) {
    if (toStart) Object.assign(this, { endContainer: this.startContainer, endOffset: this.startOffset });
    else Object.assign(this, { startContainer: this.endContainer, startOffset: this.endOffset });
  }
  cloneRange() { return Object.assign(new Range(), this); }
  deleteContents() { this.deleted = true; }
  insertNode(node) { inserted.push({ node, at: this.cloneRange() }); }
  setStartAfter(node) { this.startContainer = node; this.startOffset = node.length; }
  get collapsed() { return this.startContainer === this.endContainer && this.startOffset === this.endOffset; }
}

const selection = {
  ranges: [],
  get rangeCount() { return this.ranges.length; },
  getRangeAt(i) { return this.ranges[i]; },
  removeAllRanges() { this.ranges = []; },
  addRange(range) { this.ranges.push(range); },
};
const point = range => range && [range.startContainer, range.startOffset, range.endContainer, range.endOffset];

const editor = new Element('DIV', 'editor', { editable: true, host: true });
const first = new Element('P', 'first', { editable: true, parent: editor }).text('first paragraph');
const last = new Element('P', 'last', { editable: true, parent: editor }).text('last paragraph');
const outside = new Element('P', 'outside').text('not editable');
const button = new Element('BUTTON', 'save');
const icon = new Element('SPAN', 'icon', { parent: button });
icon.focusable = button.focusable = [button];
const elements = { '#editor': editor, '#first': first, '#last': last, '#outside': outside, '#icon': icon };

let inserted = [];
let execCommandWorks = true;
const commands = [];
let caretPoint = null;  // what caretPositionFromPoint answers
const document = {
  body: {},
  activeElement: null,
  querySelector: selector => elements[selector] || null,
  querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
  elementFromPoint: () => first,
  createTextNode: text => new Text(text, null),
  createRange: () => new Range(),
  caretPositionFromPoint: () => caretPoint,
  execCommand(command, ui, value) {
    commands.push({ command, value, at: point(selection.ranges[0]) });
    return execCommandWorks;
  },
};
let listener;
const context = vm.createContext({
  console, crypto: webcrypto, Event, MouseEvent, InputEvent, PointerEvent: MouseEvent, document,
  CSS: { escape: x => x },
  window: { PointerEvent: MouseEvent, getSelection: () => selection,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
  browser: { runtime: {
    onMessage: { addListener: fn => { listener = fn; } },
    sendMessage: () => Promise.resolve(null),
  } },
});
vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
// Results are copied out of the vm realm for deepEqual.
const send = msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
const reset = () => {
  selection.ranges = [];
  inserted = [];
  commands.length = 0;
  for (const el of [editor, first, last, outside, button, icon]) Object.assign(el, { focused: 0, events: [] });
};

(async () => {
  // A ref or selector click in an editor focuses the editing host with the caret at the end of
  // the clicked paragraph, without scrolling, before the click handlers run.
  reset();
  let focusedAtClick = null;
  const onClick = () => { focusedAtClick = document.activeElement; };
  last.addEventListener('click', onClick);
  assert.equal((await send({ action: 'click', selector: '#last' })).ok, true);
  last.removeEventListener('click', onClick);
  assert.equal(editor.focused, 1);
  assert.ok(focusedAtClick === editor, 'focus moves on mousedown, as for a real click');
  assert.deepEqual({ ...editor.focusOptions }, { preventScroll: true });
  assert.deepEqual(point(selection.ranges[0]), [last, 1, last, 1], 'caret at the end of the clicked paragraph');

  // A click at coordinates puts the caret at the click point.
  reset();
  caretPoint = { offsetNode: first.childNodes[0], offset: 5 };
  assert.equal((await send({ action: 'click', coords: { x: 30, y: 10 } })).ok, true);
  assert.equal(editor.focused, 1);
  assert.deepEqual(point(selection.ranges[0]), [first.childNodes[0], 5, first.childNodes[0], 5]);

  // A click point outside the clicked element falls back to its end.
  reset();
  caretPoint = { offsetNode: outside.childNodes[0], offset: 2 };
  assert.equal((await send({ action: 'click', coords: { x: 30, y: 10 } })).ok, true);
  assert.deepEqual(point(selection.ranges[0]), [first, 1, first, 1]);

  // A cancelled mousedown keeps focus and the caret where they were.
  reset();
  const cancel = event => event.preventDefault();
  last.addEventListener('mousedown', cancel);
  assert.equal((await send({ action: 'click', selector: '#last' })).ok, true);
  last.removeEventListener('mousedown', cancel);
  assert.equal(editor.focused, 0);
  assert.equal(selection.rangeCount, 0);

  // A click on a control's descendant focuses the control; plain text focuses nothing.
  reset();
  assert.equal((await send({ action: 'click', selector: '#icon' })).ok, true);
  assert.equal(button.focused, 1);
  assert.equal(icon.focused, 0);
  assert.equal((await send({ action: 'click', selector: '#outside' })).ok, true);
  assert.equal(outside.focused, 0);
  assert.equal(selection.rangeCount, 0);

  // type inserts at the caret when it is inside the target...
  reset();
  const caret = new Range();
  caret.setStart(first.childNodes[0], 3);
  selection.addRange(caret);
  assert.deepEqual(await send({ action: 'type', selector: '#editor', text: 'X' }), { ok: true });
  assert.equal(editor.focused, 1);
  assert.deepEqual(commands, [{ command: 'insertText', value: 'X', at: [first.childNodes[0], 3, first.childNodes[0], 3] }]);

  // ...and at the end of the target otherwise, also into a paragraph inside the editor (was not_fillable).
  reset();
  caret.setStart(first.childNodes[0], 3);
  selection.addRange(caret);
  assert.deepEqual(await send({ action: 'type', selector: '#last', text: ' Appended.' }), { ok: true });
  assert.deepEqual(commands, [{ command: 'insertText', value: ' Appended.', at: [last, 1, last, 1] }]);
  reset();
  assert.deepEqual(await send({ action: 'type', selector: '#editor', text: 'Y' }), { ok: true });
  assert.deepEqual(commands, [{ command: 'insertText', value: 'Y', at: [editor, 2, editor, 2] }]);

  // clear replaces all of the target's content.
  reset();
  selection.addRange(caret);
  assert.deepEqual(await send({ action: 'type', selector: '#editor', text: 'Z', clear: true }), { ok: true });
  assert.deepEqual(commands, [{ command: 'insertText', value: 'Z', at: [editor, 0, editor, 2] }]);

  // Without execCommand, the text is inserted at the caret with beforeinput and input.
  reset();
  execCommandWorks = false;
  assert.deepEqual(await send({ action: 'type', selector: '#last', text: ' Fallback.' }), { ok: true });
  assert.equal(inserted.length, 1);
  assert.equal(inserted[0].node.textContent, ' Fallback.');
  assert.deepEqual(point(inserted[0].at), [last, 1, last, 1]);
  assert.deepEqual(last.events, ['beforeinput', 'input']);
  assert.deepEqual(point(selection.ranges[0]), [inserted[0].node, 10, inserted[0].node, 10], 'caret after the text');

  // A page that cancels beforeinput keeps its content.
  reset();
  const refuse = event => event.preventDefault();
  last.addEventListener('beforeinput', refuse);
  assert.deepEqual(await send({ action: 'type', selector: '#last', text: ' No.' }), { ok: true });
  last.removeEventListener('beforeinput', refuse);
  assert.deepEqual(inserted, []);
  assert.deepEqual(last.events, ['beforeinput']);
  execCommandWorks = true;

  // setEditableContent appends at the end too, not where focus() put the caret.
  reset();
  editor.focus = function (options) {
    Element.prototype.focus.call(this, options);
    const start = new Range();
    start.setStart(first.childNodes[0], 0);
    selection.removeAllRanges();
    selection.addRange(start);
  };
  const edited = await send({ action: 'setEditableContent', selector: '#editor', value: ' End.', clear: false });
  assert.equal(edited.ok, true);
  assert.deepEqual(commands, [{ command: 'insertText', value: ' End.', at: [editor, 2, editor, 2] }]);

  console.log('Click focus and editor caret checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
