// Run with: node tests/single_option_wrapper.cjs (no browser or npm dependencies).
// A jQuery UI autocomplete menu with one option (#483): the <ul tabindex="0"> takes the click
// and selects the .ui-menu-item on the event's path. The snapshot lists only the option, as it
// does when the menu has two options, so the ref an agent clicks selects it.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element {
  // nodes: strings (text nodes) and Elements, in document order.
  constructor(tagName, attrs = {}, nodes = []) {
    Object.assign(this, { tagName, attrs, nodeType: 1, isConnected: true, listeners: {} });
    this.childNodes = nodes.map(node => typeof node === 'string' ? { nodeType: 3, textContent: node } : node);
    this.children = this.childNodes.filter(node => node.nodeType === 1);
    this.children.forEach(child => { child.parentElement = this; });
  }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  get innerText() { return this.textContent; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  matches() { return false; }
  closest() { return null; }
  focus() {}
  scrollIntoView() {}
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  // Bubbles through the ancestors, as a browser does for a click.
  dispatchEvent(event) {
    event.target ??= this;
    for (let el = this; el; el = event.bubbles ? el.parentElement : null) {
      for (const fn of el.listeners[event.type] || []) fn(event);
    }
    return true;
  }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);

// jQuery UI 1.12 autocomplete markup, and its menu's delegated click handler.
function menu(labels, onSelect) {
  const ul = h('UL', { class: 'ui-menu', tabindex: '0' }, ...labels.map(label =>
    h('LI', { class: 'ui-menu-item' }, h('DIV', { class: 'ui-menu-item-wrapper', tabindex: '-1' }, label))));
  ul.addEventListener('click', event => {
    for (let el = event.target; el && el !== ul; el = el.parentElement) {
      if (el.attrs.class === 'ui-menu-item') return onSelect(el.textContent);
    }
  });
  return ul;
}

async function run(labels) {
  const selected = [];
  const body = h('BODY', {}, menu(labels, label => selected.push(label)));
  let listener;
  class MouseEvent { constructor(type, init = {}) { Object.assign(this, init, { type }); } }
  const context = vm.createContext({
    console, URL, crypto: webcrypto, CSS: { escape: x => x }, setTimeout, performance,
    Event: MouseEvent, MouseEvent, PointerEvent: MouseEvent, FocusEvent: MouseEvent,
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    location: { href: 'https://miniwob.test/book-flight' },
    navigator: { platform: 'Linux x86_64' },
    window: { PointerEvent: MouseEvent,
      getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
    document: { body, title: 'book-flight', getElementById: () => null,
      querySelector: () => null, querySelectorAll: () => [], elementFromPoint: () => null },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve))
    .then(result => JSON.parse(JSON.stringify(result)));
  const { snapshot } = await send({ action: 'getSnapshot' });
  const buttons = snapshot.split('\n').filter(line => /- button /.test(line));
  assert.deepEqual(buttons.map(line => line.trim().replace(/\[ref=[^\]]+\]/, '[ref]')),
    labels.map(label => `- button ${JSON.stringify(label)} [ref]`), snapshot);
  assert.ok(buttons.every(line => /^- button/.test(line)), 'options are not nested in a menu button');
  for (const [i, line] of buttons.entries()) {
    const ref = line.match(/\[ref=([^\]]+)\]/)[1];
    const result = await send({ action: 'click', selector: ref });
    assert.equal(result.ok, true);
    assert.equal(result.tag, 'div', 'the click lands on the option');
    assert.deepEqual(selected, labels.slice(0, i + 1));
  }
}

(async () => {
  await run(['Wenatchee, WA (EAT)']);
  await run(['Akron/Canton, OH (CAK)', 'Canton/Akron, OH (CAK)']);
  console.log('Single option wrapper checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
