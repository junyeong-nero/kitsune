// Run with: node tests/closed_shadow_fill.cjs (no browser or npm dependencies).
// browser_type (fill, type and editable modes) and browser_form_fill reach a field inside
// open and closed shadow roots, nested up to SHADOW_FIELD_DEPTH roots deep, through
// Firefox's openOrClosedShadowRoot. A field they cannot reach fails as not_fillable with
// a reason, and no page script is injected.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

// SHADOW_FIELD_DEPTH in extension/content/content.js.
const SHADOW_FIELD_DEPTH = 8;

function matches(el, selector) {
  return selector.split(',').map(part => part.trim()).some(part =>
    part === '*' || (part === '[contenteditable="true"]' ? el.contentEditable === 'true'
      : el.tagName === part.toUpperCase()));
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
const shadow = nodes => new ShadowRoot(nodes);

// A host whose closed root holds `field` behind depth - 1 more closed hosts.
function closedChain(id, depth, field) {
  let node = field;
  for (let level = depth; level > 1; level--) node = new Element('X-LEVEL', { closed: shadow([node]) });
  return new Element('X-CHAIN', { id, closed: shadow([node]) });
}

// Each call is a separate document with its own content script.
function page(body) {
  const document = {
    body,
    querySelector: selector => body.descendants.find(el => `#${el.id}` === selector) || null,
    querySelectorAll: () => [],
  };
  // Scripts injected into the page world after the content script loaded.
  const scripts = [];
  document.documentElement = { appendChild(script) { scripts.push(script.textContent); } };
  document.createElement = () => ({ remove() {} });

  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout, document,
    HTMLInputElement, HTMLTextAreaElement,
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    InputEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    KeyboardEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    window: { wrappedJSObject: {}, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  scripts.length = 0;
  const send = msg => new Promise(resolve => listener(msg, {}, resolve))
    .then(result => JSON.parse(JSON.stringify(result)));
  return { send, scripts };
}

function fixture() {
  const fields = Object.fromEntries(['closed', 'open', 'nested', 'openInClosed', 'closedInOpen', 'deepest',
    'tooDeep'].map(name => [name, input(`${name}-inner`)]));
  const hosts = {
    closed: new Element('X-CODE-INPUT', { id: 'closed', closed: shadow([fields.closed]) }),
    open: new Element('X-SEARCH-INPUT', { id: 'open', open: shadow([fields.open]) }),
    // Two closed roots: the host's root holds another closed host with the input.
    nested: new Element('X-NESTED-INPUT', { id: 'nested',
      closed: shadow([new Element('X-INNER-FIELD', { closed: shadow([fields.nested]) })]) }),
    openInClosed: new Element('X-FORM-ROW', { id: 'openInClosed',
      closed: shadow([new Element('SPAN'), new Element('X-TEXT-FIELD', { open: shadow([fields.openInClosed]) })]) }),
    closedInOpen: new Element('X-FORM-ROW', { id: 'closedInOpen',
      open: shadow([new Element('X-TEXT-FIELD', { closed: shadow([fields.closedInOpen]) })]) }),
    deepest: closedChain('deepest', SHADOW_FIELD_DEPTH, fields.deepest),
    tooDeep: closedChain('tooDeep', SHADOW_FIELD_DEPTH + 1, fields.tooDeep),
    badge: new Element('X-BADGE', { id: 'badge',
      closed: shadow([new Element('X-ICON', { closed: shadow([new Element('SPAN')]) })]) }),
    plain: new Element('DIV', { id: 'plain' }),
  };
  const body = new Element('BODY', { children: Object.values(hosts) });
  return { body, hosts, fields };
}

function assertNotFillable(result, label, tag, reason) {
  assert.equal(result.code, 'not_fillable', `${label}: ${JSON.stringify(result)}`);
  assert.equal(result.retryable, true, label);
  assert.match(result.error, reason === 'shadow_depth'
    ? new RegExp(`^Element is not a fillable field: no .* within ${SHADOW_FIELD_DEPTH} nested shadow roots`)
    : /^Element is not a fillable field: no .* in it or in its open or closed shadow roots$/, label);
  assert.deepEqual(result.state, { tag, reason }, label);
}

(async () => {
  // Filling and typing reach the input inside a closed root, as they do for open roots.
  {
    const { body, fields } = fixture();
    const { send, scripts } = page(body);
    assert.deepEqual(await send({ action: 'fill', selector: '#closed', value: 'KITSUNE_FAKE_FILL_000' }),
      { ok: true, pierced: true, tag: 'input' });
    assert.equal(fields.closed.value, 'KITSUNE_FAKE_FILL_000');
    assert.deepEqual(fields.closed.events, ['input', 'change']);
    assert.equal(fields.closed.focused, true);
    assert.deepEqual(await send({ action: 'fill', selector: '#open', value: 'kittens' }),
      { ok: true, pierced: true, tag: 'input' });
    assert.equal(fields.open.value, 'kittens');

    fields.closed.events = [];
    assert.deepEqual(await send({ action: 'type', selector: '#closed', text: 'KITSUNE_FAKE_TYPE_000', clear: true }),
      { ok: true });
    assert.equal(fields.closed.value, 'KITSUNE_FAKE_TYPE_000');
    assert.deepEqual(fields.closed.events, ['input', 'change', 'keydown', 'keyup']);
    assert.deepEqual(await send({ action: 'type', selector: '#closed', text: '1' }), { ok: true });
    assert.equal(fields.closed.value, 'KITSUNE_FAKE_TYPE_0001', 'typing without clear appends');

    const editable = await send({ action: 'setEditableContent', selector: '#closed', value: 'KITSUNE_FAKE_EDIT_000' });
    assert.deepEqual(editable, { ok: true, tag: 'input', length: 21 });
    assert.equal(fields.closed.value, 'KITSUNE_FAKE_EDIT_000');
    assert.deepEqual(scripts, [], 'no page script is injected');
  }

  // fill, type and form_fill reach an input through nested open and closed shadow roots.
  for (const name of ['nested', 'openInClosed', 'closedInOpen', 'deepest']) {
    const { body, fields } = fixture();
    const { send, scripts } = page(body);
    const field = fields[name];
    const fill = `KITSUNE_FAKE_${name.toUpperCase()}_FILL_000`;
    assert.deepEqual(await send({ action: 'fill', selector: `#${name}`, value: fill }),
      { ok: true, pierced: true, tag: 'input' }, `fill #${name}`);
    assert.equal(field.value, fill, `fill #${name}`);
    assert.deepEqual(field.events, ['input', 'change']);
    assert.equal(field.focused, true);

    const typed = `KITSUNE_FAKE_${name.toUpperCase()}_TYPE_000`;
    assert.deepEqual(await send({ action: 'type', selector: `#${name}`, text: typed, clear: true }), { ok: true });
    assert.equal(field.value, typed, `type #${name}`);
    assert.deepEqual(await send({ action: 'type', selector: `#${name}`, text: '1' }), { ok: true });
    assert.equal(field.value, `${typed}1`, 'typing without clear appends');

    const formValue = `KITSUNE_FAKE_${name.toUpperCase()}_FORM_000`;
    assert.deepEqual(await send({ action: 'formFill', fields: { [`#${name}`]: formValue } }),
      { results: { [`#${name}`]: { ok: true, tag: 'input' } } }, `formFill #${name}`);
    assert.equal(field.value, formValue, `formFill #${name}`);
    assert.deepEqual(scripts, [], 'no page script is injected');
  }

  // A field that cannot be reached fails as not_fillable with its reason, and nothing changes.
  {
    const { body, hosts, fields } = fixture();
    const { send, scripts } = page(body);
    for (const [selector, reason] of [['#badge', 'no_field'], ['#plain', 'no_field'], ['#tooDeep', 'shadow_depth']]) {
      const tag = hosts[selector.slice(1)].tagName.toLowerCase();
      const value = 'KITSUNE_FAKE_UNREACHABLE_000';
      assertNotFillable(await send({ action: 'fill', selector, value }), `fill ${selector}`, tag, reason);
      assertNotFillable(await send({ action: 'type', selector, text: value, clear: true }),
        `type ${selector}`, tag, reason);
      const form = await send({ action: 'formFill', fields: { [selector]: value, '#missing': value } });
      assert.equal(form.error, 'No field was filled');
      assertNotFillable(form.results[selector], `formFill ${selector}`, tag, reason);
      assert.deepEqual(form.results['#missing'], { error: 'Not found' });
      assert.doesNotMatch(JSON.stringify(form), /KITSUNE_FAKE/, 'form_fill does not echo the value');
    }
    assert.equal(fields.tooDeep.value, '');
    const composed = node => [node, ...[...node.children, ...(node.openOrClosedShadowRoot?.children || [])]
      .flatMap(composed)];
    assert.equal(composed(body).some(el => el.focused || 'currentValue' in el), false,
      'nothing was focused or written');
    assert.deepEqual(scripts, [], 'no page-context fallback is injected');
    assert.equal((await send({ action: 'fill', selector: '#missing', value: 'x' })).code, 'not_found');
    assert.equal((await send({ action: 'type', selector: '#missing', text: 'x' })).code, 'not_found');
  }

  console.log('Nested and closed shadow fill checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
