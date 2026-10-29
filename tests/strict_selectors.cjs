// Run with: node tests/strict_selectors.cjs (no browser or npm dependencies).
// #134: a write action whose CSS selector matches several actionable elements fails with
// ambiguous_target and dispatches nothing; first: true acts on the first match; refs and
// read actions are unaffected.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element extends EventTarget {
  constructor(tagName, attrs = {}, children = [], text = '') {
    super();
    Object.assign(this, { tagName, attrs, children, parentElement: null, isConnected: true,
      textContent: text, innerText: text, value: attrs.value || '', checked: false, events: [],
      scrollTop: 0, scrollLeft: 0 });
    for (const child of children) child.parentElement = this;
    for (const type of ['click', 'dblclick', 'mouseover', 'input', 'change', 'keydown', 'dragstart', 'drop']) {
      this.addEventListener(type, event => this.events.push(event.type));
    }
  }
  get type() { return this.attrs.type || (this.tagName === 'BUTTON' ? 'submit' : 'text'); }
  get form() { return this.closest('form'); }
  get placeholder() { return this.attrs.placeholder || ''; }
  get htmlFor() { return this.attrs.for || ''; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  // Selectors used here: tag, .class, #id, [attr], [name="x"], ':disabled', and comma lists.
  matches(selector) {
    if (selector === '*') return true;
    return selector.split(',').map(s => s.trim()).some(s => {
      const m = /^([a-z]*)(?:\.([\w-]+))?(?:#([\w-]+))?(?:\[(\w+)(?:="?([^"\]]*)"?)?\])?(:disabled)?$/.exec(s);
      if (!m) throw new SyntaxError(`unsupported selector ${s}`);
      const [, tag, cls, id, attr, value, disabled] = m;
      return (!disabled || 'disabled' in this.attrs) && (!tag || this.tagName === tag.toUpperCase()) &&
        (!cls || String(this.attrs.class || '').split(' ').includes(cls)) &&
        (!id || this.attrs.id === id) && (!attr || (value === undefined ? attr in this.attrs : this.attrs[attr] === value));
    });
  }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  querySelectorAll(selector) { return this.descendants().filter(el => el.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let el = this; el; el = el.parentElement) if (el.matches(selector)) return el;
    return null;
  }
  getClientRects() { return 'hidden' in this.attrs ? [] : [{}]; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 }; }
  focus() { if (this.tagName === 'INPUT') this.events.push('focus'); }
  blur() { this.events.push('blur'); }
  scrollIntoView() { this.events.push('scrollIntoView'); }
  scrollBy({ top = 0, left = 0 }) { this.scrollTop += top; this.scrollLeft += left; }
  click() { this.dispatchEvent(new Event('click', { bubbles: true })); }
}

class Form extends Element {
  submit() { this.dispatchEvent(new Event('submit')); this.events.push('submit'); }
}

class Select extends Element {
  constructor(attrs) {
    super('SELECT', attrs);
    this.options = [{ value: 'a', text: 'A' }, { value: 'b', text: 'B' }];
  }
}

function page() {
  const deletes = ['Delete lantern', 'Delete mug', 'Delete tent'].map((text, i) =>
    new Element('BUTTON', { class: 'item', id: `del${i}` }, [], text));
  const hiddenButton = new Element('BUTTON', { class: 'item', hidden: '' }, [], 'Hidden');
  const fields = [0, 1].map(i => new Element('INPUT', { class: 'field', name: 'title', id: `field${i}` }));
  const boxes = [0, 1].map(i => new Element('INPUT', { class: 'box', type: 'checkbox', hidden: '' }));
  const selects = [0, 1].map(() => new Select({ class: 'pick' }));
  const forms = [0, 1].map(i => new Form('FORM', { class: 'f', action: `/f${i}` }, [], ''));
  const files = [0, 1].map(() => new Element('INPUT', { class: 'upload', type: 'file', hidden: '' }));
  const single = new Element('BUTTON', { class: 'one' }, [], 'Only');
  const disabled = new Element('BUTTON', { class: 'one', disabled: '' }, [], 'Disabled');
  const secret = new Element('INPUT', { class: 'one', type: 'hidden' });
  const body = new Element('BODY', {}, [hiddenButton, ...deletes, ...fields, ...boxes, ...selects, ...forms,
    ...files, single, disabled, secret]);
  return { body, deletes, hiddenButton, fields, boxes, selects, forms, files, single, disabled };
}

class MouseEvent extends Event {}

function load(dom) {
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, KeyboardEvent: Event, DragEvent: Event, InputEvent: Event,
    File, atob, DataTransfer: class { constructor() { this.items = { add() {} }; this.files = []; } },
    CSS: { escape: x => x }, setTimeout, MessageChannel,
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { mozInnerScreenX: 0, mozInnerScreenY: 0,
      getComputedStyle: el => ({ display: 'hidden' in el.attrs ? 'none' : 'block', visibility: 'visible' }) },
    document: {
      body: dom.body, activeElement: dom.body, 
      documentElement: { scrollWidth: 100, scrollHeight: 100, appendChild() {} },
      createElement: () => ({ remove() {} }),
      getElementById: id => dom.body.querySelector('#' + id),
      querySelector: selector => dom.body.querySelector(selector),
      querySelectorAll: selector => dom.body.querySelectorAll(selector),
      // The element last scrolled into view is the one shown at its centre.
      elementFromPoint: () => dom.body.descendants().findLast(el => el.events.includes('scrollIntoView')) || null,
      execCommand() {},
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

// Dispatched events per element; an action scrolls its target into view before dispatching.
const events = elements => elements.map(el => el.events.filter(type => type !== 'scrollIntoView').length);

function assertAmbiguous(result, selector, matches, label) {
  assert.equal(result.code, 'ambiguous_target', `${label}: ${JSON.stringify(result)}`);
  assert.equal(result.retryable, false, label);
  assert.match(result.hint, /first/, label);
  assert.equal(result.state.selector, selector, label);
  assert.equal(result.state.matches, matches, label);
  assert.ok(result.state.candidates.length === Math.min(matches, 5), label);
  for (const candidate of result.state.candidates) assert.match(candidate.ref, /^s\d+e\d+$/, label);
}

async function testSelectorMatchingThreeButtons() {
  const dom = page();
  const send = load(dom);
  const ambiguous = await send({ action: 'click', selector: '.item' });
  // The hidden fourth match is not actionable for a pointer action.
  assertAmbiguous(ambiguous, '.item', 3, 'click');
  assert.deepEqual(ambiguous.state.candidates.map(({ ref, ...c }) => c), [
    { role: 'button', name: 'Delete lantern' }, { role: 'button', name: 'Delete mug' },
    { role: 'button', name: 'Delete tent' }]);
  assert.deepEqual(events([dom.hiddenButton, ...dom.deletes]), [0, 0, 0, 0], 'zero clicks');

  // A candidate's ref acts on exactly that element.
  assert.equal((await send({ action: 'click', selector: ambiguous.state.candidates[1].ref })).ok, true);
  assert.deepEqual(events(dom.deletes), [0, 2, 0]);

  // first: true acts on the first match, exactly once.
  const first = await send({ action: 'click', selector: '.item', first: true });
  assert.equal(first.ok, true, JSON.stringify(first));
  assert.deepEqual(events([dom.hiddenButton, ...dom.deletes]), [2, 0, 2, 0]);

  // An actionability wait fails at once instead of waiting.
  const waited = await send({ action: 'click', selector: '.item', wait: true, timeout: 5000 });
  assert.equal(waited.code, 'ambiguous_target');
  assert.deepEqual(events([dom.hiddenButton, ...dom.deletes]), [2, 0, 2, 0]);
}

async function testEveryWriteActionIsStrict() {
  const cases = [
    ['doubleClick', { selector: '.item' }, d => d.deletes, 3],
    ['screenPoint', { selector: '.item', click: true }, d => [d.hiddenButton, ...d.deletes], 3],
    ['describeTarget', { selector: '.item' }, d => d.deletes, 3],
    ['focusElement', { selector: '.field' }, d => d.fields, 2],
    ['blurElement', { selector: '.field' }, d => d.fields, 2],
    ['scroll', { selector: '.field', direction: 'down', amount: 30 }, d => d.fields, 2],
    ['hover', { selector: '.item' }, d => d.deletes, 3],
    ['drag', { from: '.item', to: '.one' }, d => d.deletes, 3],
    ['drag', { from: '.one', to: '.item' }, d => d.deletes, 3],
    ['type', { selector: '.field', text: 'x' }, d => d.fields, 2],
    ['fill', { selector: '.field', value: 'x' }, d => d.fields, 2],
    ['setEditableContent', { selector: '.field', value: 'x', format: 'text' }, d => d.fields, 2],
    ['keypress', { selector: '.field', key: 'a' }, d => d.fields, 2],
    // Value actions count hidden native inputs: they act on them.
    ['checkBox', { selector: '.box', checked: true }, d => d.boxes, 2],
    ['selectOption', { selector: '.pick', value: 'b' }, d => d.selects, 2],
    ['submitForm', { selector: '.f' }, d => d.forms, 2],
  ];
  for (const [action, params, targets, matches] of cases) {
    const dom = page();
    const send = load(dom);
    const selector = params.selector || (params.from === '.item' ? params.from : params.to);
    const result = await send({ action, ...params });
    assertAmbiguous(result, selector, matches, action);
    assert.ok(targets(dom).every(el => el.events.length === 0 && el.value === '' && !el.checked),
      `${action} dispatched nothing`);
    const first = await send({ action, ...params, first: true });
    assert.ok(!first.error, `${action} first: ${JSON.stringify(first)}`);
  }

  const dom = page();
  const send = load(dom);
  const filled = await send({ action: 'formFill', fields: { '.field': 'x', title: 'y' } });
  assert.equal(filled.error, 'No field was filled');
  assertAmbiguous(filled.results['.field'], '.field', 2, 'formFill selector key');
  assertAmbiguous(filled.results.title, '[name="title"]', 2, 'formFill name key');
  assert.deepEqual(dom.fields.map(el => el.value), ['', '']);
  assert.ok(!(await send({ action: 'formFill', fields: { '.field': 'x' }, first: true })).error);
  assert.deepEqual(dom.fields.map(el => el.value), ['x', '']);

  await send({ action: 'uploadChunk', uploadId: 'u', index: 0, data: btoa('hi') });
  const upload = await send({ action: 'uploadCommit', uploadId: 'u', selector: '.upload',
    files: [{ name: 'a.txt', type: 'text/plain', start: 0, end: 1 }], mode: 'input' });
  assertAmbiguous(upload, '.upload', 2, 'uploadCommit');
  assert.deepEqual(events(dom.files), [0, 0]);
}

async function testImplicitUploadAndFuzzyFieldsAreStrict() {
  const dom = page();
  const send = load(dom);
  const upload = async extra => {
    await send({ action: 'uploadChunk', uploadId: 'implicit', index: 0, data: btoa('hi') });
    return send({ action: 'uploadCommit', uploadId: 'implicit',
      files: [{ name: 'a.txt', type: 'text/plain', start: 0, end: 1 }], mode: 'input', ...extra });
  };
  assertAmbiguous(await upload({}), 'input[type=file]', 2, 'implicit upload');
  assert.deepEqual(events(dom.files), [0, 0]);
  assert.equal((await upload({ first: true })).ok, true);
  assert.deepEqual(events(dom.files), [2, 0]);
  assert.equal((await upload({ mode: 'drop' })).mode, 'drop', 'an explicit body drop ignores file inputs');

  for (const byLabel of [true, false]) {
    const fields = [0, 1].map(() => new Element('INPUT', byLabel ? {} : { placeholder: 'Delivery address' }));
    const labels = fields.map(field => new Element('LABEL', {}, [field], 'Delivery address'));
    const dom = { body: new Element('BODY', {}, byLabel ? labels : fields) };
    const send = load(dom);
    const result = await send({ action: 'formFill', fields: { Delivery: 'new' } });
    const failure = result.results.Delivery;
    assert.equal(failure.code, 'ambiguous_target', JSON.stringify(result));
    assert.equal(failure.retryable, false);
    assert.equal(failure.state.field, 'Delivery');
    assert.equal(failure.state.matches, 2);
    assert.equal(failure.state.candidates.length, 2);
    assert.deepEqual(fields.map(el => el.value), ['', '']);
    assert.deepEqual(events(fields), [0, 0]);
    assert.ok(!(await send({ action: 'formFill', fields: { Delivery: 'new' }, first: true })).error);
    assert.deepEqual(fields.map(el => el.value), ['new', '']);
    assert.ok(!(await send({ action: 'fill', selector: failure.state.candidates[1].ref, value: 'second' })).error);
    assert.deepEqual(fields.map(el => el.value), ['new', 'second']);
  }

  // Two labels associated with one control are not two targets. Labels still precede placeholders.
  const field = new Element('INPUT', { id: 'delivery' });
  const duplicateLabels = [0, 1].map(() => new Element('LABEL', { for: 'delivery' }, [], 'Delivery'));
  const placeholder = new Element('INPUT', { placeholder: 'Delivery' });
  const unique = load({ body: new Element('BODY', {}, [...duplicateLabels, field, placeholder]) });
  assert.ok(!(await unique({ action: 'formFill', fields: { Delivery: 'unique' } })).error);
  assert.equal(field.value, 'unique');
  assert.equal(placeholder.value, '');
}

async function testOneActionableMatchAndReadsKeepFirstMatch() {
  const dom = page();
  const send = load(dom);
  // .one matches an enabled button, a disabled one and an <input type=hidden>.
  assert.equal((await send({ action: 'click', selector: '.one' })).ok, true);
  assert.deepEqual(events([dom.single, dom.disabled]), [2, 0]);
  // Reads keep first-match semantics.
  assert.equal((await send({ action: 'getBounds', selector: '.item' })).width, 100);
  assert.equal((await send({ action: 'screenPoint', selector: '.item' })).viewportX, 50);
  assert.equal((await send({ action: 'describeTarget', selector: '.item', first: true })).text, 'Hidden');
}

(async () => {
  await testSelectorMatchingThreeButtons();
  await testEveryWriteActionIsStrict();
  await testImplicitUploadAndFuzzyFieldsAreStrict();
  await testOneActionableMatchAndReadsKeepFirstMatch();
})().then(() => console.log('Strict selector checks passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
