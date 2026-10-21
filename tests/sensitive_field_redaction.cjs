// Run with: node tests/sensitive_field_redaction.cjs (no browser or npm dependencies).
// Masked non-password fields (inputs, textareas, editable regions), password-manager
// autofill and shadow DOM controls are redacted like type=password. All values are
// disposable fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const MARKER = 'KITSUNE_FAKE';
const fake = label => `${MARKER}_${label}_000`;

// Tag names, '*', '#id', '[attr]' and the autofill pseudo-classes; anything else
// (e.g. '[value^="K"]', 'label[for=…]') matches nothing.
function matchesSelector(el, selector) {
  return selector.split(',').map(part => part.trim()).some(part => {
    if (part === '*') return true;
    // Emulates a browser that does not implement this vendor alias.
    if (part === ':-webkit-autofill') throw new SyntaxError(`unsupported pseudo-class ${part}`);
    if (part === ':autofill' || part === ':-moz-autofill') return !!el.autofilled;
    if (part.startsWith('#')) return el.id === part.slice(1);
    const attribute = part.match(/^\[([\w-]+)\]$/);
    if (attribute) return el.hasAttribute(attribute[1]);
    return /^[\w-]+$/.test(part) && el.tagName === part.toUpperCase();
  });
}

// -webkit-text-security is inherited, like the computed style it emulates.
const isMaskedElement = el => !!el && (!!el.masked || isMaskedElement(el.parentElement));

class Node {
  get descendants() { return this.children.flatMap(child => [child, ...child.descendants]); }
  querySelectorAll(selector) { return this.descendants.filter(el => matchesSelector(el, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

class ShadowRoot extends Node {
  constructor(children) { super(); this.children = children; }
}

class Element extends Node {
  constructor(tagName, { text = '', attrs = {}, children = [], ...props } = {}) {
    super();
    Object.assign(this, { tagName, text, attrs, children, nodeType: 1, isConnected: true }, props);
    children.forEach(child => { child.parentElement = this; });
  }
  get childNodes() { return [{ nodeType: 3, textContent: this.text }, ...this.children]; }
  get textContent() { return this.text + this.children.map(child => child.textContent).join(''); }
  // Rendered text as Firefox reports it: a textarea's content is not rendered, and
  // masked text shows as bullets.
  get innerText() {
    if (this.tagName === 'TEXTAREA') return '';
    const text = isMaskedElement(this) ? '•'.repeat(this.text.length) : this.text;
    return text + this.children.map(child => child.innerText).join('');
  }
  replaceChildren() { this.text = ''; this.children = []; }
  getAttribute(key) { return this.attrs[key.toLowerCase()] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  get attributes() { return Object.entries(this.attrs).map(([name, value]) => ({ name, value })); }
  get innerHTML() { return this.text + this.children.map(child => child.outerHTML).join(''); }
  get outerHTML() {
    const tag = this.tagName.toLowerCase();
    const attrs = Object.entries(this.attrs).map(([name, value]) => ` ${name}="${value}"`).join('');
    return `<${tag}${attrs}>${this.innerHTML}</${tag}>`;
  }
  // Like the DOM: shadow roots, autofill state and computed styles are not cloned.
  cloneNode() {
    const { tagName, text, attrs, children, parentElement, shadowRoot, openOrClosedShadowRoot,
      autofilled, masked, isContentEditable, ...props } = this;
    return Object.assign(new this.constructor(tagName, { text, attrs: { ...attrs },
      children: children.map(child => child.cloneNode(true)) }), props);
  }
  matches(selector) { return matchesSelector(this, selector); }
  closest() { return null; }
  focus() { this.focused = true; }
  scrollIntoView() {}
  dispatchEvent() { return true; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}

// A form-associated custom element whose value property mirrors its inner input.
class CustomControl extends Element {
  get value() {
    const root = this.shadowRoot || this.openOrClosedShadowRoot;
    return root ? root.children[0].value : this.innerValue;
  }
}

const input = (id, props = {}, attrs = {}) => new Element('INPUT', {
  attrs: { 'aria-label': id, ...attrs }, id, name: id, type: 'text', value: '', ...props });
// A textarea's initial content is its text; typing changes only its value.
const textarea = (id, text, props = {}) => new Element('TEXTAREA', {
  attrs: { 'aria-label': id }, id, name: id, type: 'textarea', text, value: text, ...props });
// Descendants of an editing host are editable too.
const editable = (tagName, props = {}) => new Element(tagName, {
  attrs: { contenteditable: 'true' }, contentEditable: 'true', isContentEditable: true, ...props });

(async () => {
  // Non-sensitive controls keep their values.
  const email = input('email', { type: 'email', value: 'user@example.test' }, { value: 'user@example.test' });
  const zip = input('postalCode', { value: '12345' }, { inputmode: 'numeric' });
  const shipping = input('shipping', { value: 'ground' });
  const username = input('username', { value: 'fixture-user', autofilled: true });
  // Sensitive by autocomplete token (React mirrors two values into attributes).
  const sensitive = [
    input('field1', { value: fake('CARD') }, { autocomplete: 'cc-number', value: fake('CARD') }),
    input('field2', { type: 'tel', value: fake('CSC') }, { autocomplete: 'billing cc-csc' }),
    input('field3', { value: fake('OTP') }, { autocomplete: 'one-time-code', inputmode: 'numeric' }),
    input('field4', { value: fake('CURRENT') }, { autocomplete: 'current-password', value: fake('CURRENT') }),
    input('field5', { value: fake('NEW') }, { autocomplete: 'new-password' }),
    // Sensitive by name: numeric one-time code boxes and plain-text secret fields.
    input('code', { value: fake('CODE') }, { inputmode: 'numeric' }),
    input('userPassword', { value: fake('SHOWN') }),
    input('field6', { type: 'number', placeholder: 'Enter PIN', value: fake('PIN') }),
    input('cardCvv', { type: 'tel', value: fake('CVV') }),
    // Masked with CSS instead of type=password.
    input('field7', { value: fake('MASKED'), masked: true }),
  ];
  const [cardNumber] = sensitive;
  // Textareas: one masked (initial content and a typed value), one ordinary.
  const maskedNotes = textarea('recovery', fake('TEXTAREA_INITIAL'), { masked: true, value: fake('TEXTAREA') });
  const notes = textarea('notes', 'plain notes');
  const form = new Element('FORM', { children: [email, zip, shipping, username, ...sensitive, maskedNotes, notes] });

  // Shadow DOM controls: open, closed with Firefox's openOrClosedShadowRoot, and
  // a closed one that only advertises type=password on the host.
  const vaultInner = input('inner', { type: 'password', value: fake('VAULT') });
  const vault = new CustomControl('X-PASSWORD-INPUT', { id: 'vault',
    attrs: { role: 'textbox', 'aria-label': 'Vault key', value: fake('VAULT') },
    shadowRoot: new ShadowRoot([vaultInner]) });
  const closedOtp = new CustomControl('X-OTP-INPUT', { id: 'closed-otp',
    attrs: { role: 'textbox', 'aria-label': 'Closed code' }, shadowRoot: null,
    openOrClosedShadowRoot: new ShadowRoot([input('closed-inner',
      { value: fake('CLOSED') }, { autocomplete: 'one-time-code' })]) });
  const typedHost = new CustomControl('X-SECRET-FIELD', { id: 'typed-host',
    attrs: { role: 'textbox', 'aria-label': 'Typed host', type: 'password' }, innerValue: fake('TYPED') });
  // Firefox Xray wrappers hide the page-defined value property; the attribute remains.
  const xray = new Element('X-XRAY-INPUT', { id: 'xray', attrs: { value: fake('XRAY') },
    shadowRoot: new ShadowRoot([input('xray-inner', { type: 'password', value: fake('XRAY') })]) });
  const search = new CustomControl('X-SEARCH', { id: 'search',
    attrs: { role: 'searchbox', 'aria-label': 'Search', value: 'kittens' },
    shadowRoot: new ShadowRoot([input('q', { type: 'search', value: 'kittens' })]) });
  // Editing regions: one masked, with a nested paragraph that inherits the mask, and
  // one ordinary. Neither has an aria-label, so names fall back to their text.
  const secretNoteLine = new Element('P', { id: 'secret-note-line', text: fake('EDITABLE_LINE'),
    contentEditable: 'inherit', isContentEditable: true });
  const secretNote = editable('DIV', { id: 'secret-note', text: fake('EDITABLE'), masked: true,
    children: [secretNoteLine] });
  const draft = editable('DIV', { id: 'draft', text: 'draft text' });
  const widgets = new Element('DIV', { text: 'Widgets',
    children: [vault, closedOtp, typedHost, xray, search, secretNote, draft] });
  const body = new Element('BODY', { children: [form, widgets] });
  const fields = [...form.children];

  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout, location: { href: 'http://example.test/' },
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    KeyboardEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    MouseEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    window: { getComputedStyle: el => ({ display: 'block', visibility: 'visible',
      getPropertyValue: property => property === '-webkit-text-security' && isMaskedElement(el) ? 'disc' : '' }) },
    document: { body, title: 'Fixture',
      getElementById: id => body.querySelector(`#${id}`),
      querySelector: selector => [body, ...body.querySelectorAll(selector)].find(el => el.matches(selector)) || null,
      querySelectorAll: selector => [body, ...body.descendants].filter(el => el.matches(selector)),
    },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve));
  const assertNoLeak = (label, result) => {
    assert.ok(!JSON.stringify(result).includes(MARKER), `${label} leaks a sensitive value`);
  };

  // Writes go through the same paths as browser_type / browser_form_fill, and
  // their results and errors never echo the value.
  for (const msg of [
    { action: 'fill', selector: '#vault', value: fake('VAULT') },
    { action: 'formFill', fields: { '#field5': fake('NEW') } },
    { action: 'type', selector: '#field3', text: fake('OTP'), clear: true },
    { action: 'fill', selector: '#missing', value: fake('MISSING') },
    { action: 'selectOption', selector: '#field1', value: fake('CARD') },
    { action: 'checkBox', selector: '#field4', checked: true },
    { action: 'formFill', fields: { '#missing': fake('MISSING') } },
    { action: 'type', selector: '#recovery', text: fake('TEXTAREA'), clear: true },
    { action: 'click', selector: '#secret-note' },
    { action: 'describeTarget', selector: '#recovery', key: 'Enter' },
    { action: 'describeTarget', selector: '#secret-note-line' },
  ]) {
    const result = await send(msg);
    assertNoLeak(`${msg.action} result`, result);
    if (['click', 'describeTarget'].includes(msg.action)) assert.equal(result.ok, true, JSON.stringify(result));
  }
  assert.equal(vaultInner.value, fake('VAULT'), 'fill pierced the open shadow root');

  const snapshot = await send({ action: 'getSnapshot' });
  assertNoLeak('snapshot', snapshot);
  assert.match(snapshot.snapshot, /textbox "email" \[ref=s\d+e\d+\] \[value="user@example.test"\]/);
  assert.match(snapshot.snapshot, /textbox "postalCode" \[ref=s\d+e\d+\] \[value="12345"\]/);
  assert.match(snapshot.snapshot, /textbox "username" \[ref=s\d+e\d+\]$/m, 'autofilled values are redacted');
  assert.match(snapshot.snapshot, /textbox "Vault key" \[ref=s\d+e\d+\]$/m);
  assert.match(snapshot.snapshot, /searchbox "Search" \[ref=s\d+e\d+\] \[value="kittens"\]/);
  for (const el of [...sensitive, maskedNotes]) {
    assert.match(snapshot.snapshot, new RegExp(`(?:textbox|spinbutton) "${el.id}" \\[ref=s\\d+e\\d+\\]$`, 'm'), el.id);
  }
  assert.match(snapshot.snapshot, /textbox "notes" \[ref=s\d+e\d+\] \[value="plain notes"\]/);
  assert.match(snapshot.snapshot, /textbox "draft text" \[ref=s\d+e\d+\]$/m, 'an ordinary editing region keeps its name');
  assert.match(snapshot.snapshot, /textbox "" \[ref=s\d+e\d+\]$/m, 'a masked editing region has no text name');

  const textSnapshot = await send({ action: 'getSnapshot', text: true });
  assertNoLeak('text snapshot', textSnapshot);
  assert.match(textSnapshot.snapshot, /textbox "draft text"/);
  assert.match(textSnapshot.snapshot, /- text "Widgets"/);

  const forms = await send({ action: 'getFormFields' });
  assertNoLeak('forms', forms);
  const formField = id => forms.fields.find(field => field.id === id);
  for (const [el, value] of [[email, 'user@example.test'], [zip, '12345'], [shipping, 'ground']]) {
    assert.equal(formField(el.id).value, value, `${el.id} keeps its value`);
  }
  assert.equal(formField('notes').value, 'plain notes', 'an ordinary textarea keeps its value');
  for (const el of [username, ...sensitive, maskedNotes]) {
    const field = formField(el.id);
    assert.equal('value' in field, false, el.id);
    assert.deepEqual([field.redacted, field.hasValue], [true, true], el.id);
    assert.equal(field.selector, `#${el.id}`, `${el.id} stays addressable`);
  }
  assert.equal((await send({ action: 'focusElement', selector: formField('field7').ref })).ok, true);

  const dom = await send({ action: 'getAccessibilityTree', depth: 6 });
  assertNoLeak('dom', dom);
  const domNodes = [];
  (function collect(node) { domNodes.push(node); (node.children || []).forEach(collect); })(dom.tree);
  const domNode = id => domNodes.find(node => node.id === id);
  assert.equal(domNode('email').value, 'user@example.test');
  assert.equal(domNode('search').value, 'kittens');
  assert.deepEqual([domNode('notes').value, domNode('notes').text], ['plain notes', 'plain notes']);
  assert.equal(domNode('draft').text, 'draft text');
  for (const el of [username, ...sensitive, maskedNotes, secretNote, vault, closedOtp, typedHost]) {
    assert.deepEqual([domNode(el.id).redacted, domNode(el.id).hasValue], [true, true], el.id);
    assert.equal('text' in domNode(el.id), false, el.id);
  }
  assert.equal('text' in domNode('secret-note-line'), false, 'nested editable text inherits the mask');
  // hasValue follows the host's value property, which this host does not expose.
  assert.deepEqual([domNode('xray').redacted, domNode('xray').hasValue], [true, false]);

  const query = await send({ action: 'query', selector: 'input',
    fields: ['text', 'value', 'Value', 'attrs', 'html', 'id', 'autocomplete'] });
  assertNoLeak('query', query);
  const row = id => query.items.find(item => item.id === id);
  assert.equal(row('email').value, 'user@example.test');
  assert.equal(row('field1').autocomplete, 'cc-number');
  for (const el of sensitive) {
    assert.deepEqual([row(el.id).redacted, row(el.id).hasValue], [true, true], el.id);
    assert.equal('value' in row(el.id).attrs, false, el.id);
  }
  const hostQuery = await send({ action: 'query', selector: 'x-password-input, x-otp-input, x-secret-field, x-xray-input, x-search',
    fields: ['value', 'attrs', 'html'] });
  assertNoLeak('hostQuery', hostQuery);
  assert.deepEqual(Array.from(hostQuery.items, item => item.redacted), [true, true, true, true, undefined]);
  assert.equal(hostQuery.items[4].value, 'kittens');
  assert.equal(hostQuery.items[4].attrs.value, 'kittens');
  const textQuery = await send({ action: 'query', selector: 'textarea, [contenteditable], p',
    fields: ['id', 'text', 'value', 'html'] });
  assertNoLeak('textQuery', textQuery);
  const textRow = id => textQuery.items.find(item => item.id === id);
  for (const id of ['recovery', 'secret-note', 'secret-note-line']) {
    assert.deepEqual([textRow(id).redacted, textRow(id).hasValue, textRow(id).text, textRow(id).html],
      [true, true, '', ''], id);
  }
  assert.deepEqual([textRow('notes').value, textRow('notes').text, textRow('notes').html],
    ['plain notes', 'plain notes', 'plain notes']);
  assert.deepEqual([textRow('draft').redacted, textRow('draft').text, textRow('draft').html],
    [undefined, 'draft text', 'draft text']);

  // Markup drops sensitive value attributes, including reflected ones on shadow hosts.
  const html = await send({ action: 'getHTML', selector: 'body' });
  assertNoLeak('html', html);
  assert.match(html.html, /value="user@example.test"/);
  assert.match(html.html, /value="kittens"/);
  assert.match(html.html, /<textarea aria-label="recovery"><\/textarea>/, 'masked textarea content is dropped');
  assert.match(html.html, /<textarea aria-label="notes">plain notes<\/textarea>/);
  assert.match(html.html, /<div contenteditable="true"><\/div>/, 'masked editing region content is dropped');
  assert.match(html.html, /<div contenteditable="true">draft text<\/div>/);
  assert.equal(maskedNotes.textContent, fake('TEXTAREA_INITIAL'), 'redaction does not mutate the page');
  assert.equal(secretNote.children.length, 1);
  assert.equal(vault.getAttribute('value'), fake('VAULT'), 'redaction does not mutate the page');
  assert.equal(cardNumber.getAttribute('value'), fake('CARD'));
  assert.equal(xray.getAttribute('value'), fake('XRAY'));
  // A clone that does not mirror the page cannot be mapped back and drops every value attribute.
  const cloneNode = form.cloneNode;
  form.cloneNode = function() {
    const clone = cloneNode.call(this);
    clone.children.unshift(new Element('SPAN'));
    return clone;
  };
  const misaligned = await send({ action: 'getHTML', selector: 'form' });
  assertNoLeak('misaligned html', misaligned);
  assert.doesNotMatch(misaligned.html, /value=/);
  assert.doesNotMatch(misaligned.html, /plain notes/, 'every textarea is emptied when sensitivity cannot be mapped');
  delete form.cloneNode;
  const widgetsCloneNode = widgets.cloneNode;
  widgets.cloneNode = function() {
    const clone = widgetsCloneNode.call(this);
    clone.children.unshift(new Element('SPAN'));
    return clone;
  };
  const misalignedWidgets = await send({ action: 'getHTML', selector: 'div' });
  assertNoLeak('misaligned widgets html', misalignedWidgets);
  assert.doesNotMatch(misalignedWidgets.html, /draft text/, 'every editing region is emptied');
  delete widgets.cloneNode;

  for (const msg of [
    { action: 'getPageText' },
    { action: 'getPageText', selector: 'form' },
    { action: 'getMarkdown' },
    { action: 'explainSelector', selector: 'input' },
    { action: 'explainSelector', selector: 'textarea' },
    { action: 'explainSelector', selector: '[contenteditable]' },
    { action: 'waitForElement', selector: formField('field1').ref, timeout: 0 },
    { action: 'waitForElement', selector: '#recovery', timeout: 0 },
    { action: 'waitForElement', selector: '#secret-note', timeout: 0 },
    { action: 'getBounds', selector: '#vault' },
  ]) assertNoLeak(`${msg.action} ${msg.selector || ''}`, await send(msg));
  // Ordinary text stays readable; the browser renders masked text as bullets.
  const pageText = (await send({ action: 'getPageText' })).text;
  assert.match(pageText, /draft text/);
  assert.match(pageText, /•+/);
  const markdown = (await send({ action: 'getMarkdown' })).markdown;
  assert.match(markdown, /draft text/);
  assert.equal((await send({ action: 'explainSelector', selector: 'textarea' })).samples[1].text, 'plain notes');
  assert.equal((await send({ action: 'waitForElement', selector: '#draft', timeout: 0 })).text, 'draft text');

  // find cannot match against sensitive values, but still matches others.
  const probe = await send({ action: 'find', query: MARKER });
  assertNoLeak('find', probe);
  assert.ok(probe.results.every(result => result.score === 1), 'only the interactive bonus scores');
  const byValue = await send({ action: 'find', query: 'user@example.test' });
  assert.equal(byValue.results[0].selector, '#email');
  const byNotes = await send({ action: 'find', query: 'plain notes' });
  assert.equal(byNotes.results[0].selector, '#notes');
  assert.equal(byNotes.results[0].text, 'plain notes');
  const byDraft = await send({ action: 'find', query: 'draft text' });
  assert.equal(byDraft.results[0].selector, '#draft');

  // Value-attribute selectors are refused while any sensitive field or host has one.
  for (const msg of [
    { action: 'query', selector: 'input[value^="K"]' },
    { action: 'explainSelector', selector: 'x-password-input[value^="K"]' },
    { action: 'getHTML', selector: '[value^="K"]' },
  ]) {
    const res = await send(msg);
    assertNoLeak(`${msg.action} value selector`, res);
    assert.equal(res.error, 'value-attribute selectors are disabled while a sensitive field has a value attribute');
  }
  cardNumber.removeAttribute('value');
  sensitive[3].removeAttribute('value');
  assert.match((await send({ action: 'query', selector: '[value^="K"]' })).error, /value-attribute selectors/,
    'a shadow host reflecting a sensitive value still blocks');
  vault.removeAttribute('value');
  xray.removeAttribute('value');
  assert.equal((await send({ action: 'query', selector: '[value^="K"]' })).error, undefined);

  // Autofill redaction ends when the browser clears the autofill state.
  username.autofilled = false;
  assert.equal((await send({ action: 'getFormFields' })).fields.find(field => field.id === 'username').value,
    'fixture-user');

  console.log('Sensitive field redaction checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
