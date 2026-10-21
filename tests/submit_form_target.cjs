// Run with: node tests/submit_form_target.cjs (no browser or npm dependencies).
// #133: browser_submit_form without a target submits the form of the field Kitsune last
// filled or focused, refuses with ambiguous_target when several forms leave that open,
// and names the form it submitted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element extends EventTarget {
  constructor(tagName, attrs = {}, children = []) {
    super();
    Object.assign(this, { tagName, attrs, children, parentElement: null, connected: true, textContent: '' });
    for (const child of children) child.parentElement = this;
  }
  get isConnected() { return this.connected && (!this.parentElement || this.parentElement.isConnected); }
  get type() { return this.attrs.type || (this.tagName === 'BUTTON' ? 'submit' : 'text'); }
  get form() { return this.closest('form'); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  matches(selector) {
    return selector.split(',').map(s => s.trim()).some(s => {
      const [, tag = '', id, type] = /^([a-z]*)(?:#([\w-]+))?(?:\[type="?(\w+)"?\])?$/.exec(s) || [];
      return (!tag || this.tagName === tag.toUpperCase()) && (!id || this.attrs.id === id) &&
        (!type || this.attrs.type === type);
    });
  }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  querySelectorAll(selector) { return this.descendants().filter(el => el.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let el = this; el; el = el.parentElement) if (el.matches(selector)) return el;
    return null;
  }
  focus() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 20 }; }
}

class Button extends Element {
  constructor(text, attrs = {}) {
    super('BUTTON', attrs);
    this.textContent = text;
    this.clicks = 0;
  }
  click() { this.clicks++; }
}

class Form extends Element {
  constructor(attrs, children) {
    super('FORM', attrs, children);
    this.submits = 0;
  }
  submit() { this.submits++; }
}

// Two per-item delete forms above an add form, as on the page in #133.
function wishlistPage() {
  const deletes = ['Delete lantern', 'Delete mug'].map((text, i) =>
    new Form({ action: '/wishlist/delete', method: 'post', id: `delete${i}` }, [new Button(text, { type: 'submit' })]));
  const name = new Element('INPUT', { id: 'name', name: 'name' });
  const price = new Element('INPUT', { id: 'price', name: 'price' });
  const outside = new Element('INPUT', { id: 'search', name: 'q' });
  const add = new Form({ action: '/wishlist/add', method: 'post', id: 'add' }, [name, price, new Button('Add', { type: 'submit' })]);
  const forms = [...deletes, add];
  const body = new Element('BODY', {}, [...forms, outside]);
  return { forms, deletes, add, name, price, outside, body };
}

function load(page) {
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, KeyboardEvent: Event, CSS: { escape: x => x },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: {
      body: page.body,
      getElementById: () => null,
      querySelector: selector => page.body.querySelector(selector),
      querySelectorAll: selector => page.body.querySelectorAll(selector),
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  // Results come from the vm realm, so compare plain copies.
  return msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
}

const submissions = page => page.forms.map(form => form.submits + form.querySelector('button').clicks);

async function testFilledFieldNamesTheForm() {
  for (const fill of [
    { action: 'formFill', fields: { '#name': 'Umbrella' } },
    { action: 'fill', selector: '#name', value: 'Umbrella' },
    { action: 'type', selector: '#name', text: 'Umbrella' },
    { action: 'focusElement', selector: '#price' },
  ]) {
    const page = wishlistPage();
    const send = load(page);
    assert.ok(!(await send(fill)).error, fill.action);
    const result = await send({ action: 'submitForm' });
    assert.equal(result.ok, true, `${fill.action}: ${JSON.stringify(result)}`);
    assert.deepEqual(submissions(page), [0, 0, 1], `${fill.action} submits the add form only`);
    assert.equal(result.submitted, 'button');
    assert.match(result.form.ref, /^s\d+e\d+$/);
    assert.deepEqual({ ...result.form, ref: undefined },
      { ref: undefined, action: '/wishlist/add', method: 'post', button: 'Add' }, fill.action);
  }
}

async function testSeveralFormsWithoutAFilledFieldAreAmbiguous() {
  const page = wishlistPage();
  const send = load(page);
  const result = await send({ action: 'submitForm' });
  assert.equal(result.code, 'ambiguous_target', JSON.stringify(result));
  assert.equal(result.retryable, false);
  assert.match(result.hint, /ref/);
  assert.deepEqual(submissions(page), [0, 0, 0], 'nothing was submitted');
  assert.equal(result.state.forms, 3);
  assert.deepEqual(result.state.candidates.map(({ ref, ...form }) => form), [
    { action: '/wishlist/delete', method: 'post', button: 'Delete lantern' },
    { action: '/wishlist/delete', method: 'post', button: 'Delete mug' },
    { action: '/wishlist/add', method: 'post', button: 'Add' },
  ]);
  // A candidate's ref submits exactly that form.
  const chosen = await send({ action: 'submitForm', selector: result.state.candidates[2].ref });
  assert.equal(chosen.ok, true, JSON.stringify(chosen));
  assert.equal(chosen.form.action, '/wishlist/add');
  assert.deepEqual(submissions(page), [0, 0, 1]);

  // A field outside every form, or one the page removed, names no form.
  const other = wishlistPage();
  const sendOther = load(other);
  assert.ok((await sendOther({ action: 'fill', selector: '#search', value: 'q' })).ok);
  assert.equal((await sendOther({ action: 'submitForm' })).code, 'ambiguous_target');
  assert.ok((await sendOther({ action: 'fill', selector: '#name', value: 'x' })).ok);
  other.add.connected = false;
  assert.equal((await sendOther({ action: 'submitForm' })).code, 'ambiguous_target');
  assert.deepEqual(submissions(other), [0, 0, 0]);

  // At most five candidates; state.forms counts them all.
  const many = wishlistPage();
  for (let i = 0; i < 4; i++) {
    const form = new Form({ action: `/extra/${i}` }, []);
    form.parentElement = many.body;
    many.body.children.push(form);
    many.forms.push(form);
  }
  const crowded = await load(many)({ action: 'submitForm' });
  assert.equal(crowded.state.forms, 7);
  assert.equal(crowded.state.candidates.length, 5);
}

async function testSingleFormKeepsItsBehavior() {
  const name = new Element('INPUT', { id: 'name' });
  const form = new Form({ action: '/only' }, [name]);
  const page = { forms: [form], body: new Element('BODY', {}, [form]) };
  const result = await load(page)({ action: 'submitForm' });
  assert.equal(result.ok, true, JSON.stringify(result));
  assert.equal(result.submitted, 'method');
  assert.equal(form.submits, 1);
  assert.deepEqual({ ...result.form, ref: undefined }, { ref: undefined, action: '/only', method: 'get', button: '' });
}

async function testTargetedSubmitNamesItsForm() {
  const page = wishlistPage();
  const send = load(page);
  const result = await send({ action: 'submitForm', selector: '#delete1' });
  assert.equal(result.ok, true);
  assert.deepEqual({ ...result.form, ref: undefined },
    { ref: undefined, action: '/wishlist/delete', method: 'post', button: 'Delete mug' });
  assert.deepEqual(submissions(page), [0, 1, 0]);
}

(async () => {
  await testFilledFieldNamesTheForm();
  await testSeveralFormsWithoutAFilledFieldAreAmbiguous();
  await testSingleFormKeepsItsBehavior();
  await testTargetedSubmitNamesItsForm();
})().then(() => console.log('Submit form target checks passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
