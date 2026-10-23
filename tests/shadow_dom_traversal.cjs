// Run with: node tests/shadow_dom_traversal.cjs (no browser or npm dependencies).
// Issue #156: snapshot, dom, find, forms, selectors, refs, coordinate clicks and keypress
// see fields inside open and closed shadow roots, and a form_fill label that matches
// fields in more than one tree (the page and a web component) writes nothing.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const MARKER = 'KITSUNE_FAKE';
const PASSWORD = `${MARKER}_SHADOW_PASSWORD_000`;

// Comma lists of compound selectors: tag, '*', '#id', '[attr]', '[attr="value"]'.
// Combinators and pseudo-classes match nothing.
function matchesCompound(el, part) {
  if (part === '*') return true;
  const parsed = part.match(/^([\w-]*)((?:#[\w-]+)?)((?:\[[\w-]+(?:\^?=(?:"[^"]*"|[^\]]*))?\])*)$/);
  if (!parsed) return false;
  const [, tag, id, attrs] = parsed;
  if (tag && el.tagName !== tag.toUpperCase()) return false;
  if (id && el.id !== id.slice(1)) return false;
  for (const [, name, op, value] of attrs.matchAll(/\[([\w-]+)(?:(\^?=)"?([^"\]]*)"?)?\]/g)) {
    const actual = el.getAttribute(name);
    if (actual == null) return false;
    if (op === '=' && actual !== value) return false;
    if (op === '^=' && !actual.startsWith(value)) return false;
  }
  return true;
}
const matches = (el, selector) => selector.split(',').some(part => matchesCompound(el, part.trim()));

class Text {
  constructor(text) { Object.assign(this, { nodeType: 3, textContent: text }); }
}

class Node {
  constructor() { this.childNodes = []; }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  append(...nodes) {
    for (const item of nodes) {
      const node = typeof item === 'string' ? new Text(item) : item;
      node.parentNode = this;
      this.childNodes.push(node);
    }
    return this;
  }
  // One tree: shadow roots are separate trees, as in the DOM.
  get descendants() { return this.children.flatMap(child => [child, ...child.descendants]); }
  querySelectorAll(selector) { return this.descendants.filter(el => matches(el, selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getElementById(id) { return this.descendants.find(el => el.id === id) || null; }
}

class ShadowRoot extends Node {
  constructor(host) {
    super();
    Object.assign(this, { host, activeElement: null, hit: null });
  }
  elementFromPoint() { return this.hit || this.host; }
}

const FIELD_PROPS = ['type', 'name', 'placeholder', 'value'];

class Element extends Node {
  constructor(tagName, attrs = {}, ...children) {
    super();
    Object.assign(this, { tagName, attrs, nodeType: 1, isConnected: true, events: [],
      shadowRoot: null, openOrClosedShadowRoot: null, style: {} });
    for (const prop of FIELD_PROPS) if (prop in attrs) this[prop] = attrs[prop];
    if (tagName === 'INPUT') {
      this.type ??= 'text';
      this.value ??= '';
      if (this.type === 'checkbox') this.checked = false;
    }
    if (tagName === 'LABEL') this.htmlFor = attrs.for || '';
    this.append(...children);
  }
  get id() { return this.attrs.id || ''; }
  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
  getRootNode() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node;
  }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  get innerText() { return this.textContent; }
  get firstElementChild() { return this.children[0] || null; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  matches(selector) { return matches(this, selector); }
  closest(selector) {
    for (let el = this; el; el = el.parentElement) if (el.matches(selector)) return el;
    return null;
  }
  attachShadow({ mode }) {
    const root = new ShadowRoot(this);
    // Page code sees only open roots; Firefox content scripts see closed ones too.
    this.openOrClosedShadowRoot = root;
    if (mode === 'open') this.shadowRoot = root;
    return root;
  }
  click() { this.dispatchEvent({ type: 'click' }); }
  focus() { this.focused = true; }
  scrollIntoView(options) { this.scrolled = options; }
  // A dispatched click runs native activation, as in a browser.
  dispatchEvent(event) {
    this.events.push(event.type);
    if (event.type === 'click' && this.type === 'checkbox') this.checked = !this.checked;
    return true;
  }
  getBoundingClientRect() { return { x: 0, y: 0, left: 0, top: 0, width: 100, height: 30 }; }
  getClientRects() { return [this.getBoundingClientRect()]; }
}

// A slot renders its host's light children.
class SlotElement extends Element {
  constructor() { super('SLOT'); }
  host() { return this.getRootNode().host; }
  assignedElements() { return this.host().children; }
  assignedNodes() { return this.host().childNodes; }
}

const h = (tagName, attrs, ...children) => new Element(tagName, attrs, ...children);

function fixture() {
  // A signup form elsewhere on the page, with the same "이메일" label and email name.
  const pageEmail = h('INPUT', { id: 'signup-email', type: 'email', name: 'email' });
  const signup = h('FORM', { id: 'signup' }, h('LABEL', { for: 'signup-email' }, '이메일'), pageEmail);

  const newsletter = h('NEWSLETTER-SIGNUP', { id: 'newsletter' });
  const email = h('INPUT', { id: 'news-email', type: 'email', name: 'email' });
  const weekly = h('INPUT', { type: 'checkbox', name: 'weekly' });
  const offers = h('INPUT', { type: 'checkbox', name: 'offers' });
  const password = h('INPUT', { type: 'password', name: 'pw', value: PASSWORD });
  const submit = h('BUTTON', { type: 'submit' }, '구독');
  newsletter.attachShadow({ mode: 'open' }).append(
    h('STYLE', {}, ':host { display: block }'),
    h('FORM', {},
      h('H3', {}, '주간 소식'),
      h('P', {}, '매주 월요일에 보내 드립니다'),
      h('LABEL', {}, '이메일 ', email),
      h('LABEL', {}, weekly, ' 주간 소식 받기'),
      h('LABEL', {}, offers, ' 이벤트 알림'),
      h('LABEL', {}, '비밀번호 ', password),
      submit));

  const closedNote = h('INPUT', { 'aria-label': 'Closed note' });
  const closedHost = h('X-CLOSED-FIELD', {});
  closedHost.attachShadow({ mode: 'closed' }).append(closedNote);

  const slotted = h('BUTTON', {}, 'Slotted action');
  const card = h('X-CARD', {}, slotted);
  card.attachShadow({ mode: 'open' }).append(h('DIV', {}, new SlotElement()));

  const body = h('BODY', {}, h('H1', {}, 'Gallery'), signup, newsletter, closedHost, card);
  return { body, pageEmail, newsletter, email, weekly, offers, password, submit, closedHost, closedNote, slotted };
}

function page(body) {
  const document = new Node();
  document.append(body);
  Object.assign(document, {
    body, title: 'Gallery', activeElement: body, hit: null,
    elementFromPoint() { return this.hit; },
    documentElement: { appendChild() {} },
    createElement: () => ({ remove() {} }),
  });
  let listener;
  const event = class { constructor(type, options) { Object.assign(this, { type }, options); } };
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout, document,
    innerWidth: 800, innerHeight: 600, scrollY: 0, devicePixelRatio: 1,
    getComputedStyle: el => ({ position: el.position || 'static', visibility: el.style.visibility || 'visible' }),
    location: { href: 'http://gallery.test/' },
    Event: event, MouseEvent: event, KeyboardEvent: event, InputEvent: event,
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve))
    .then(result => JSON.parse(JSON.stringify(result)));
  return { send, document };
}

const refOf = (snapshot, line) => {
  const match = snapshot.match(new RegExp(`${line} \\[ref=(s\\d+e\\d+)\\]`));
  assert.ok(match, `${line} in:\n${snapshot}`);
  return match[1];
};

(async () => {
  // Issue #222: exact capture markers reach open/closed shadow targets and clean up detached nodes.
  for (const closed of [false, true]) {
    const dom = fixture();
    const host = closed ? dom.closedHost : dom.newsletter;
    const target = closed ? dom.closedNote : dom.email;
    host.position = 'sticky';
    const { send, document } = page(dom.body);
    const { snapshot } = await send({ action: 'getSnapshot' });
    const ref = refOf(snapshot, closed ? 'textbox "Closed note"' : 'textbox "이메일"');
    // The email label also exists outside the component; take its component-specific ref.
    const targetRef = closed ? ref : refOf(snapshot.split('주간 소식')[1], 'textbox "이메일"');
    target.setAttribute('id', 'shadow-capture');
    const cssMarked = await send({ action: 'markScreenshotTarget', selector: '#shadow-capture' });
    assert.ok(target.hasAttribute(cssMarked.attribute), 'shadow CSS uses the shared resolver');
    assert.equal((await send({ action: 'captureMeasure', selector: cssMarked.selector })).w, 100);
    await send({ action: 'unmarkScreenshotTarget', attribute: cssMarked.attribute });
    assert.equal(target.hasAttribute(cssMarked.attribute), false);
    const marked = await send({ action: 'markScreenshotTarget', selector: targetRef });
    assert.ok(target.hasAttribute(marked.attribute));
    assert.equal(document.querySelector(marked.selector), null, 'the marker is invisible to document selectors');
    const measured = await send({ action: 'captureMeasure', selector: marked.selector });
    assert.equal(measured.w, 100);
    assert.equal(measured.h, 30);
    assert.deepEqual(await send({ action: 'captureScrollIntoView', selector: marked.selector, block: 'start' }), { ok: true });
    assert.equal(target.scrolled.block, 'start');
    await send({ action: 'hideFixed', selector: marked.selector });
    assert.notEqual(host.style.visibility, 'hidden', 'sticky shadow ancestor is not hidden');
    await send({ action: 'restoreFixed' });
    target.isConnected = false;
    assert.deepEqual(await send({ action: 'captureMeasure', selector: marked.selector }), { missing: true });
    await send({ action: 'unmarkScreenshotTarget', attribute: marked.attribute });
    assert.equal(target.hasAttribute(marked.attribute), false, 'detached target marker is removed');
    target.isConnected = true;
    assert.deepEqual(await send({ action: 'captureMeasure', selector: marked.selector }), { missing: true }, 'registry entry is removed');
    await send({ action: 'unmarkScreenshotTarget', attribute: marked.attribute });
  }

  // The snapshot lists the component's fields with refs; a ref check toggles the checkbox.
  {
    const dom = fixture();
    const { send } = page(dom.body);
    const { snapshot } = await send({ action: 'getSnapshot' });
    assert.doesNotMatch(snapshot, new RegExp(MARKER), 'shadow password stays redacted');
    refOf(snapshot, 'heading "주간 소식"');
    refOf(snapshot, 'textbox "이메일"');
    refOf(snapshot, 'button "구독"');
    refOf(snapshot, 'textbox "Closed note"');
    assert.match(snapshot, /textbox "비밀번호" \[ref=s\d+e\d+\]$/m, 'no value state on the password');
    assert.equal(snapshot.match(/button "Slotted action"/g).length, 1, 'a slotted child appears once');
    const weeklyRef = refOf(snapshot, 'checkbox "주간 소식 받기"');
    refOf(snapshot, 'checkbox "이벤트 알림"');
    assert.deepEqual(await send({ action: 'checkBox', selector: weeklyRef, checked: true }), { ok: true, checked: true });
    assert.equal(dom.weekly.checked, true);
    assert.equal(dom.offers.checked, false);

    const text = await send({ action: 'getSnapshot', text: true });
    assert.match(text.snapshot, /- text "매주 월요일에 보내 드립니다"/);

    const tree = await send({ action: 'getAccessibilityTree', depth: 12 });
    const names = [];
    (function collect(node) { names.push(node.name); (node.children || []).forEach(collect); })(tree.tree);
    assert.ok(['weekly', 'offers', 'pw'].every(name => names.includes(name)), 'browser_dom lists shadow fields');
    assert.doesNotMatch(JSON.stringify(tree), new RegExp(MARKER));
  }

  // find, forms and selectors reach into shadow roots; their refs resolve into the tree.
  {
    const dom = fixture();
    const { send } = page(dom.body);
    const found = await send({ action: 'find', query: '주간 소식' });
    assert.ok(found.results.some(r => r.tag === 'h3' && r.text === '주간 소식'), JSON.stringify(found));
    const boxes = await send({ action: 'find', query: 'input[type=checkbox]' });
    assert.equal(boxes.results.length, 2, JSON.stringify(boxes));
    assert.deepEqual(await send({ action: 'checkBox', selector: boxes.results[1].ref, checked: true }),
      { ok: true, checked: true });
    assert.equal(dom.offers.checked, true);

    const { fields } = await send({ action: 'getFormFields' });
    const field = name => fields.filter(f => f.name === name);
    assert.deepEqual(field('email').map(f => f.label), ['이메일', '이메일'], 'page and component email');
    assert.equal(field('weekly')[0].label, '주간 소식 받기');
    assert.deepEqual([field('pw')[0].redacted, field('pw')[0].value], [true, undefined]);
    assert.doesNotMatch(JSON.stringify(fields), new RegExp(MARKER));

    // A selector matching a field in the light DOM and one in a shadow root names neither.
    const ambiguous = await send({ action: 'type', selector: 'input[type=email]', text: 'x' });
    assert.equal(ambiguous.code, 'ambiguous_target', JSON.stringify(ambiguous));
    assert.equal(ambiguous.state.matches, 2);
    assert.deepEqual(await send({ action: 'fill', selector: 'h3', value: 'x' }).then(r => r.code), 'not_fillable');
    assert.deepEqual(await send({ action: 'click', selector: 'button[type=submit]' }).then(r => r.tag), 'button');
    assert.equal(dom.submit.events.includes('click'), true, 'a selector inside a shadow root resolves');
    // A value-attribute selector cannot probe a sensitive value attribute inside a shadow root.
    const probe = await send({ action: 'click', selector: 'input[value^="K"]' });
    assert.match(probe.error, /value-attribute selectors are disabled/);
  }

  // Coordinates resolve through shadow roots to the deepest element; keypress follows focus.
  {
    const dom = fixture();
    const { send, document } = page(dom.body);
    document.hit = dom.newsletter;
    dom.newsletter.shadowRoot.hit = dom.offers;
    const clicked = await send({ action: 'click', coords: { x: 10, y: 10 } });
    assert.equal(clicked.tag, 'input', JSON.stringify(clicked));
    assert.equal(dom.offers.checked, true, 'the coordinate click toggled the checkbox, not the host');
    const described = await send({ action: 'describeTarget', coords: { x: 10, y: 10 } });
    assert.equal(described.tag, 'input');

    document.activeElement = dom.newsletter;
    dom.newsletter.shadowRoot.activeElement = dom.email;
    assert.equal((await send({ action: 'keypress', key: 'a' })).target, 'input');
    assert.equal((await send({ action: 'describeTarget', key: 'Enter' })).tag, 'input');
  }

  // form_fill: a label or name matching fields in two trees writes nothing.
  {
    const dom = fixture();
    const { send } = page(dom.body);
    const label = await send({ action: 'formFill', fields: { '이메일': 'reader@example.test' } });
    assert.equal(label.error, 'No field was filled', JSON.stringify(label));
    const result = label.results['이메일'];
    assert.equal(result.code, 'ambiguous_target');
    assert.equal(result.retryable, false);
    assert.equal(result.state.matches, 2);
    assert.deepEqual(result.state.candidates.map(c => [c.role, c.name]), [['textbox', '이메일'], ['textbox', '이메일']]);
    assert.doesNotMatch(JSON.stringify(label), /reader@example/, 'the value is not echoed');
    const byName = await send({ action: 'formFill', fields: { email: 'reader@example.test' } });
    assert.equal(byName.results.email.code, 'ambiguous_target');
    assert.deepEqual([dom.pageEmail.value, dom.email.value], ['', ''], 'neither email field was written');

    // A candidate's ref names one field.
    assert.deepEqual(await send({ action: 'fill', selector: result.state.candidates[1].ref, value: 'reader@example.test' }),
      { ok: true, pierced: false, tag: 'input' });
    assert.deepEqual([dom.pageEmail.value, dom.email.value], ['', 'reader@example.test']);

    // A label only the component has, and a selector inside its root, fill that component.
    const own = await send({ action: 'formFill', fields: { '주간 소식 받기': true, '#news-email': 'b@example.test' } });
    assert.deepEqual(own.results, { '주간 소식 받기': { ok: true, tag: 'input', checked: true },
      '#news-email': { ok: true, tag: 'input' } });
    assert.deepEqual([dom.weekly.checked, dom.email.value, dom.pageEmail.value], [true, 'b@example.test', '']);

    // first: true fills the first match only, the page's field.
    const first = await send({ action: 'formFill', fields: { '이메일': 'c@example.test' }, first: true });
    assert.deepEqual(first.results['이메일'], { ok: true, tag: 'input' });
    assert.deepEqual([dom.pageEmail.value, dom.email.value], ['c@example.test', 'b@example.test']);
  }

  console.log('Shadow DOM traversal checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
