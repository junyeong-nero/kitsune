// Run with: node tests/editor_textbox.cjs (no browser or npm dependencies).
// A contenteditable editor is one textbox (#256) that fill writes to (#255): the Papago-shaped
// source editor, a host > div > span + br, inside a presentation wrapper.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class InputEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    Object.assign(this, { inputType: init.inputType, data: init.data });
  }
}

// Mutation records since the start, for MutationObserver.takeRecords.
const mutations = [];

class Text {
  constructor(text) { Object.assign(this, { nodeType: 3, textContent: text }); }
}

// isContentEditable is inherited, as in a browser: a host's descendants report true.
class Element extends EventTarget {
  // nodes: strings (text nodes) and Elements, in document order.
  constructor(tagName, attrs = {}, nodes = [], props = {}) {
    super();
    Object.assign(this, { tagName, attrs, nodeType: 1, isConnected: true, focused: 0, events: [] }, props);
    this.childNodes = [];
    nodes.forEach(node => this.append(node));
    for (const type of ['beforeinput', 'input', 'change']) {
      this.addEventListener(type, event => this.events.push(event.type));
    }
  }
  append(node) {
    if (typeof node === 'string') node = new Text(node);
    node.parentNode = this;
    if (node.nodeType === 1) node.parentElement = this;
    this.childNodes.push(node);
    mutations.push({ type: 'childList', target: this, addedNodes: [node] });
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  get innerText() { return this.textContent; }
  get contentEditable() { return this.attrs.contenteditable ?? 'inherit'; }
  get isContentEditable() {
    const value = this.attrs.contenteditable;
    if (['true', '', 'plaintext-only'].includes(value)) return true;
    if (value === 'false') return false;
    return !!this.parentElement?.isContentEditable;
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  // Tag names and [contenteditable="…"], the selectors fill and type use.
  matches(selector) {
    return String(selector).split(',').map(part => part.trim()).some(part => {
      const editable = /^\[contenteditable="(.*)"\]$/.exec(part);
      return editable ? this.attrs.contenteditable === editable[1] : part.toUpperCase() === this.tagName;
    });
  }
  querySelector(selector) { return this.descendants().find(el => el.matches(selector)) || null; }
  querySelectorAll(selector) { return this.descendants().filter(el => selector === '*' || el.matches(selector)); }
  contains(node) {
    for (let cur = node; cur; cur = cur.parentNode) if (cur === this) return true;
    return false;
  }
  closest() { return null; }
  focus(options) { this.focused++; this.focusOptions = options; document.activeElement = this; }
  blur() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);

// Ranges and the selection keep only their boundary points.
class Range {
  selectNodeContents(node) {
    Object.assign(this, { startContainer: node, startOffset: 0, endContainer: node, endOffset: node.childNodes.length });
  }
  collapse(toStart) {
    if (toStart) Object.assign(this, { endContainer: this.startContainer, endOffset: this.startOffset });
    else Object.assign(this, { startContainer: this.endContainer, startOffset: this.endOffset });
  }
  cloneRange() { return Object.assign(new Range(), this); }
  // Only whole-content ranges are replaced here, which is what fill selects.
  deleteContents() { this.startContainer.childNodes = []; }
  insertNode(node) { this.startContainer.append(node); }
  setStartAfter(node) { Object.assign(this, { startContainer: node.parentNode,
    startOffset: node.parentNode.childNodes.indexOf(node) + 1 }); }
}
const selection = {
  ranges: [],
  get rangeCount() { return this.ranges.length; },
  getRangeAt(i) { return this.ranges[i]; },
  removeAllRanges() { this.ranges = []; },
  addRange(range) { this.ranges.push(range); },
};
const point = range => range && [range.startContainer, range.startOffset, range.endContainer, range.endOffset];

const line = h('DIV', {}, h('SPAN', { id: 'previous' }, '이전 문장'), h('BR', { id: 'br' }));
const host = h('DIV', { id: 'source', contenteditable: 'true' }, line);
const wrapper = h('DIV', { id: 'wrapper', role: 'presentation', 'aria-label': 'Type the text to translate' }, host);
const output = h('DIV', { id: 'output' }, '');
const name = new Element('INPUT', { id: 'name', 'aria-label': 'Name' }, [], { value: '', type: 'text' });
const slider = h('DIV', { id: 'slider', role: 'slider', 'aria-label': 'Volume', tabindex: '0' });
// A chip inside an editor is a control in its own right.
const chip = h('SPAN', { id: 'chip', role: 'button', contenteditable: 'false' }, '@kitsune');
const notes = h('DIV', { id: 'notes', contenteditable: 'true', 'aria-label': 'Notes' }, h('P', {}, 'Meet ', chip));
const body = h('BODY', {}, h('MAIN', {}, wrapper, output, name, slider, notes));
const byId = Object.fromEntries(body.descendants().filter(el => el.attrs.id).map(el => [`#${el.attrs.id}`, el]));

let execCommandWorks = true;
const commands = [];
class MutationObserver {
  observe() { this.seen = mutations.length; }
  takeRecords() {
    const records = mutations.slice(this.seen);
    this.seen = mutations.length;
    return records;
  }
  disconnect() {}
}
const document = {
  body, title: 'papago', activeElement: null,
  getElementById: id => byId[`#${id}`] || null,
  querySelector: selector => byId[selector] || null,
  querySelectorAll: selector => selector === '*' ? body.descendants() : byId[selector] ? [byId[selector]] : [],
  createRange: () => new Range(),
  createTextNode: text => new Text(text),
  // Records each command with the selection it applied to, and edits like Firefox: beforeinput
  // at the editing host, which the page can cancel, and true even when it does (#289).
  execCommand(command, ui, value) {
    commands.push({ command, value, at: point(selection.ranges[0]) });
    if (!execCommandWorks) return false;
    const range = selection.ranges[0];
    const inputType = command === 'insertText' ? 'insertText' : 'deleteContentBackward';
    const editHost = range.startContainer;
    if (!editHost.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType,
      data: command === 'insertText' ? value : null }))) return true;
    range.deleteContents();
    if (command === 'insertText') range.insertNode(new Text(value));
    else mutations.push({ type: 'childList', target: editHost, addedNodes: [] });
    editHost.dispatchEvent(new InputEvent('input', { bubbles: true, inputType }));
    return true;
  },
};
let listener;
const context = vm.createContext({
  console, URL, crypto: webcrypto, CSS: { escape: x => x }, setTimeout, Event, InputEvent, MutationObserver, document,
  HTMLInputElement: class {}, HTMLTextAreaElement: class {},
  KeyboardEvent: class extends Event {},
  location: { href: 'https://papago.test/' },
  window: { getSelection: () => selection,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
  browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
    sendMessage: () => Promise.resolve(null) } },
});
vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
// Results come from the vm realm, so compare plain copies.
const send = msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
const reset = () => {
  selection.ranges = [];
  commands.length = 0;
  for (const el of body.descendants()) Object.assign(el, { focused: 0, events: [] });
};
const restoreSource = () => { host.childNodes = []; host.append(line); };
const refOf = (snapshot, el, label) => {
  const match = snapshot.match(new RegExp(`${label} \\[ref=(s\\d+e\\d+)\\]`));
  assert.ok(match, `${label} has a ref in:\n${snapshot}`);
  return match[1];
};

(async () => {
  // 1. The snapshot lists each editor once, as its editing host: the line div, the span and
  //    the br inherit isContentEditable but are not textboxes, and get no refs (#256).
  const { snapshot } = await send({ action: 'getSnapshot' });
  const textboxes = snapshot.split('\n').filter(row => row.includes('- textbox'));
  assert.equal(textboxes.length, 3, `source editor, Name and Notes:\n${snapshot}`);
  // An editor's text is its value, as a field's is (#385).
  assert.match(snapshot, /- textbox "이전 문장" \[ref=s\d+e\d+\] \[value="이전 문장"\]/);
  assert.match(snapshot, /- textbox "Name" \[ref=s\d+e\d+\] \[value=""\]/);
  assert.match(snapshot, /- textbox "Notes" \[ref=s\d+e\d+\] \[value="Meet @kitsune"\]/);
  assert.match(snapshot, /- slider "Volume" \[ref=s\d+e\d+\]/);
  // A descendant that is a control in its own right is still listed.
  assert.match(snapshot, /- button "@kitsune" \[ref=s\d+e\d+\]/);
  const sourceRef = refOf(snapshot, host, 'textbox "이전 문장"');
  assert.equal((await send({ action: 'focusElement', selector: sourceRef })).ok, true);
  assert.ok(document.activeElement === host, 'the textbox ref is the editing host');

  // 2. fill through the editor's textbox ref replaces its content as typing over a full
  //    selection does (was not_fillable, #255).
  reset();
  const translated = '하카타역으로 가는 지하철은 어디에서 타나요?';
  assert.deepEqual(await send({ action: 'fill', selector: sourceRef, value: translated }),
    { ok: true, pierced: false, tag: 'div', contentEditable: true });
  assert.equal(host.focused, 1);
  assert.deepEqual({ ...host.focusOptions }, { preventScroll: true });
  assert.deepEqual(commands, [{ command: 'insertText', value: translated, at: [host, 0, host, 1] }]);
  assert.equal(host.textContent, translated);

  // 3. A stale descendant ref or selector (the br) and a wrapper around the editor fill the host.
  for (const selector of ['#br', '#wrapper']) {
    reset();
    restoreSource();
    assert.deepEqual(await send({ action: 'fill', selector, value: 'X' }),
      { ok: true, pierced: true, tag: 'div', contentEditable: true }, selector);
    assert.deepEqual(commands, [{ command: 'insertText', value: 'X', at: [host, 0, host, 1] }], selector);
    assert.equal(host.textContent, 'X', selector);
    assert.equal(host.focused, 1, selector);
  }

  // 4. An empty value clears the editor.
  reset();
  restoreSource();
  assert.equal((await send({ action: 'fill', selector: sourceRef, value: '' })).ok, true);
  assert.deepEqual(commands, [{ command: 'delete', value: undefined, at: [host, 0, host, 1] }]);
  assert.equal(host.textContent, '');

  // 5. Without execCommand the text replaces the content with beforeinput and input, and a
  //    page that cancels beforeinput keeps its content.
  reset();
  restoreSource();
  execCommandWorks = false;
  assert.equal((await send({ action: 'fill', selector: sourceRef, value: 'Fallback' })).ok, true);
  assert.equal(host.textContent, 'Fallback');
  assert.deepEqual(host.events, ['beforeinput', 'input']);
  reset();
  restoreSource();
  const refuse = event => event.preventDefault();
  host.addEventListener('beforeinput', refuse);
  assert.equal((await send({ action: 'fill', selector: sourceRef, value: 'No' })).ok, true);
  host.removeEventListener('beforeinput', refuse);
  assert.equal(host.textContent, '이전 문장');
  assert.deepEqual(host.events, ['beforeinput']);
  execCommandWorks = true;

  // 6. The editor's own beforeinput cancellation is reported, for a replacement and for
  //    clearing, and the edit is not applied another way (#289).
  for (const value of ['Replacement', '']) {
    reset();
    restoreSource();
    host.addEventListener('beforeinput', refuse);
    assert.deepEqual(await send({ action: 'fill', selector: sourceRef, value }),
      { ok: true, pierced: false, tag: 'div', contentEditable: true, prevented: true }, value);
    host.removeEventListener('beforeinput', refuse);
    assert.equal(host.textContent, '이전 문장', value);
    assert.deepEqual(host.events, ['beforeinput'], value);
    assert.equal(commands.length, 1, value);
  }

  // 7. Inputs still fill through their native setter.
  reset();
  assert.deepEqual(await send({ action: 'fill', selector: '#name', value: 'Kitsune' }),
    { ok: true, pierced: false, tag: 'input' });
  assert.equal(name.value, 'Kitsune');
  assert.deepEqual(name.events, ['input', 'change']);

  // 8. A target that is neither field nor editor is not fillable, and the hint names no
  //    parameter the core browser_type lacks (mode=). A slider without aria-valuenow cannot
  //    be stepped by fill, so its hint names the keyboard instead (#311).
  reset();
  const refused = await send({ action: 'fill', selector: '#slider', value: '50' });
  assert.equal(refused.code, 'not_fillable');
  assert.deepEqual(refused.state, { tag: 'div', reason: 'no_field', role: 'slider' });
  assert.match(refused.error, /input, textarea, select or editor/);
  assert.doesNotMatch(refused.hint, /mode=/);
  assert.match(refused.hint, /without a numeric aria-valuenow for fill to step toward: click it to focus it, /);
  assert.match(refused.hint, /browser_press_key; repeat/);
  const plain = await send({ action: 'fill', selector: '#output', value: '50' });
  assert.equal(plain.code, 'not_fillable');
  assert.deepEqual(plain.state, { tag: 'div', reason: 'no_field' });
  assert.match(plain.hint, /contenteditable editor is filled through its textbox ref/);

  // 9. A long editor's value is cut at 160 characters, like a field's, and marked as cut (#385).
  reset();
  const long = '가'.repeat(150) + ' 다음 문장입니다. 끝';
  assert.equal((await send({ action: 'fill', selector: sourceRef, value: long })).ok, true);
  const longRead = (await send({ action: 'getSnapshot' })).snapshot;
  assert.ok(longRead.includes(`[ref=${sourceRef}] [value=${JSON.stringify(long.slice(0, 160) + '\u2026')}]`), longRead);

  // 10. A delta is not unchanged when the action edited an editor past the value it shows, or the
  //     page added an element the delta does not list (#385); a transient element does not count.
  reset();
  const edited = await send({ action: 'fill', selector: sourceRef, value: long + ' 추가', deltaBaseline: true });
  assert.equal(edited.ok, true);
  const editDelta = await send({ action: 'getSnapshot', delta: true });
  assert.equal(editDelta.delta, true);
  assert.equal(editDelta.unchanged, undefined, editDelta.snapshot);
  const quiet = async (reaction = () => {}) => {
    assert.equal((await send({ action: 'focusElement', selector: sourceRef, deltaBaseline: true })).ok, true);
    reaction();
    return send({ action: 'getSnapshot', delta: true });
  };
  assert.equal((await quiet()).unchanged, true, 'focusing changes nothing');
  const spinner = h('DIV', { id: 'spinner' });
  const added = await quiet(() => output.append(spinner));
  assert.doesNotMatch(added.snapshot, /spinner/);
  assert.equal(added.unchanged, undefined, 'an added element is a change');
  const transient = await quiet(() => {
    const flash = h('DIV', { id: 'flash' });
    output.append(flash);
    output.childNodes = output.childNodes.filter(node => node !== flash);
    flash.isConnected = false;
  });
  assert.equal(transient.unchanged, true, 'an element added and removed again is not');

  console.log('Editor textbox and fill checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
