// Run with: node tests/closed_shadow_fill.cjs (no browser or npm dependencies).
// browser_type (fill, type and editable modes) reaches a field inside a closed shadow
// root through Firefox's openOrClosedShadowRoot. A field it cannot reach fails as
// not_fillable, including through the page-context fallback, never as "unknown".
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

function matches(el, selector) {
  return selector.split(',').map(part => part.trim()).some(part =>
    part === '[contenteditable="true"]' ? el.contentEditable === 'true' : el.tagName === part.toUpperCase());
}

class Node {
  constructor(children = []) { this.children = children; }
  get descendants() { return this.children.flatMap(child => [child, ...child.descendants]); }
  querySelector(selector) { return this.descendants.find(el => matches(el, selector)) || null; }
  querySelectorAll(selector) { return this.descendants.filter(el => matches(el, selector)); }
}

class ShadowRoot extends Node {}

class Element extends Node {
  constructor(tagName, { id = '', children = [], open = null, closed = null } = {}) {
    super(children);
    Object.assign(this, { tagName, id, isConnected: true, attrs: {}, events: [] });
    // Page code sees only open roots; Firefox content scripts see closed ones too.
    this.shadowRoot = open;
    this.openOrClosedShadowRoot = open || closed;
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  matches(selector) { return matches(this, selector); }
  closest() { return null; }
  focus() { this.focused = true; }
  dispatchEvent(event) { this.events.push(event.type); return true; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
  getClientRects() { return [this.getBoundingClientRect()]; }
}

// Native value accessors throw on other objects, as the browser's do.
function nativeField(name) {
  const Field = { [name]: class extends Element {} }[name];
  Object.defineProperty(Field.prototype, 'value', {
    get() { return this.currentValue ?? ''; },
    set(value) {
      if (!(this instanceof Field)) {
        throw new TypeError(`'set value' called on an object that does not implement interface ${name}.`);
      }
      this.currentValue = String(value);
    },
  });
  return Field;
}
const HTMLInputElement = nativeField('HTMLInputElement');
const HTMLTextAreaElement = nativeField('HTMLTextAreaElement');
const input = id => new HTMLInputElement('INPUT', { id });

// Each call is a separate document with its own content script and page world.
function page(body, { pageScripts = true, pageError = null } = {}) {
  const document = {
    body,
    querySelector: selector => body.descendants.find(el => `#${el.id}` === selector) || null,
    querySelectorAll: () => [],
  };
  // Injected <script> elements run in the page world, where an exception is reported
  // to the page's console and never reaches the content script.
  const pageWindow = {};
  const pageWorld = vm.createContext({
    window: pageWindow, document, HTMLInputElement, HTMLTextAreaElement,
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
  });
  const scripts = [];
  document.documentElement = {
    appendChild(script) {
      if (!pageScripts) return;
      if (script.textContent.includes('__claudeFill_')) scripts.push(script.textContent);
      if (pageError) pageWorld.document = { querySelector() { throw new Error(pageError); } };
      try { vm.runInContext(script.textContent, pageWorld); } catch {}
    },
  };
  document.createElement = () => ({ remove() {} });

  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout, document,
    HTMLInputElement, HTMLTextAreaElement,
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    InputEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    KeyboardEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    window: { wrappedJSObject: pageWindow, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve))
    .then(result => JSON.parse(JSON.stringify(result)));
  return { send, scripts };
}

function fixture() {
  const closedInput = input('closed-inner');
  const openInput = input('open-inner');
  // An input two closed roots deep: the host's own root holds another closed host.
  const nestedInput = input('nested-inner');
  const inner = new Element('X-INNER-FIELD', { closed: new ShadowRoot([nestedInput]) });
  const hosts = {
    closed: new Element('X-CODE-INPUT', { id: 'closed', closed: new ShadowRoot([closedInput]) }),
    open: new Element('X-SEARCH-INPUT', { id: 'open', open: new ShadowRoot([openInput]) }),
    nested: new Element('X-NESTED-INPUT', { id: 'nested', closed: new ShadowRoot([inner]) }),
    badge: new Element('X-BADGE', { id: 'badge', closed: new ShadowRoot([new Element('SPAN')]) }),
    plain: new Element('DIV', { id: 'plain' }),
  };
  const body = new Element('BODY', { children: Object.values(hosts) });
  return { body, hosts, closedInput, openInput, nestedInput };
}

function assertNotFillable(result, label) {
  assert.equal(result.code, 'not_fillable', `${label}: ${JSON.stringify(result)}`);
  assert.equal(result.retryable, true, label);
  assert.match(result.error, /^Element is not a fillable field/, label);
  assert.match(result.hint, /shadow root/, label);
  assert.notEqual(result.error, 'unknown', label);
}

(async () => {
  // Filling and typing reach the input inside a closed root, as they do for open roots.
  {
    const { body, closedInput, openInput } = fixture();
    const { send, scripts } = page(body);
    assert.deepEqual(await send({ action: 'fill', selector: '#closed', value: 'KITSUNE_FAKE_FILL_000' }),
      { ok: true, pierced: true, tag: 'input' });
    assert.equal(closedInput.value, 'KITSUNE_FAKE_FILL_000');
    assert.deepEqual(closedInput.events, ['input', 'change']);
    assert.equal(closedInput.focused, true);
    assert.deepEqual(await send({ action: 'fill', selector: '#open', value: 'kittens' }),
      { ok: true, pierced: true, tag: 'input' });
    assert.equal(openInput.value, 'kittens');
    assert.deepEqual(scripts, [], 'no page script is needed for a reachable field');

    closedInput.events = [];
    assert.deepEqual(await send({ action: 'type', selector: '#closed', text: 'KITSUNE_FAKE_TYPE_000', clear: true }),
      { ok: true });
    assert.equal(closedInput.value, 'KITSUNE_FAKE_TYPE_000');
    assert.deepEqual(closedInput.events, ['input', 'change', 'keydown', 'keyup']);
    assert.deepEqual(await send({ action: 'type', selector: '#closed', text: '1' }), { ok: true });
    assert.equal(closedInput.value, 'KITSUNE_FAKE_TYPE_0001', 'typing without clear appends');

    const editable = await send({ action: 'setEditableContent', selector: '#closed', value: 'KITSUNE_FAKE_EDIT_000' });
    assert.deepEqual(editable, { ok: true, tag: 'input', length: 21 });
    assert.equal(closedInput.value, 'KITSUNE_FAKE_EDIT_000');
  }

  // A field the content script cannot reach fails as not_fillable, and nothing changes.
  {
    const { body, hosts, nestedInput } = fixture();
    const { send, scripts } = page(body);
    for (const selector of ['#nested', '#badge', '#plain']) {
      const fill = await send({ action: 'fill', selector, value: 'KITSUNE_FAKE_NESTED_000' });
      assertNotFillable(fill, `fill ${selector}`);
      // The page-context fallback ran, could not reach a field either, and said so.
      assert.deepEqual(fill.state, { tag: hosts[selector.slice(1)].tagName.toLowerCase(), pageContext: 'no_field' });
      assertNotFillable(await send({ action: 'type', selector, text: 'KITSUNE_FAKE_NESTED_000', clear: true }),
        `type ${selector}`);
    }
    assert.equal(scripts.length, 3, 'only fill falls back to the page context');
    assert.equal(nestedInput.value, '');
    assert.equal(Object.values(hosts).some(host => host.focused || 'currentValue' in host), false,
      'nothing was focused or written');
    assert.equal((await send({ action: 'fill', selector: '#missing', value: 'x' })).code, 'not_found');
  }

  // A page script that never runs (e.g. blocked) or fails in the page is reported, not "unknown".
  for (const [options, pageContext] of [[{ pageScripts: false }, 'not_run'], [{ pageError: 'page broke' }, 'error']]) {
    const { body } = fixture();
    const { send } = page(body, options);
    const fill = await send({ action: 'fill', selector: '#nested', value: 'KITSUNE_FAKE_NESTED_000' });
    assertNotFillable(fill, pageContext);
    assert.equal(fill.state.pageContext, pageContext);
  }

  console.log('Closed shadow fill checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
