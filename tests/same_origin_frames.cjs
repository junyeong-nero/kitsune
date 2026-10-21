// Run with: node tests/same_origin_frames.cjs (no browser or npm dependencies).
// #157: snapshot, dom, find, forms, refs, selectors and actions reach into same-origin
// iframes (bounded depth); cross-origin iframes are listed but stay opaque.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const ORIGIN = 'https://shop.test';
const SECRET = 'KITSUNE_FAKE_FRAME_SECRET';

// One compound selector: tag, #id, [attr], [attr=value]; comma lists of them.
function matchesOne(el, selector) {
  const m = /^([a-z0-9]*)(#[\w-]+)?((?:\[[^\]]+\])*)$/i.exec(selector.trim());
  if (!m) return false;
  if (m[1] && el.tagName !== m[1].toUpperCase()) return false;
  if (m[2] && el.id !== m[2].slice(1)) return false;
  for (const [, attr, value] of m[3].matchAll(/\[([\w-]+)(?:="?([^"\]]*)"?)?\]/g)) {
    const actual = el.getAttribute(attr);
    if (actual == null || (value !== undefined && actual !== value)) return false;
  }
  return true;
}

class Element {
  constructor(tagName, attrs = {}, nodes = [], props = {}) {
    Object.assign(this, { tagName, attrs, nodeType: 1, connected: true, events: [], clicks: 0 }, props);
    this.childNodes = nodes.map(node => typeof node === 'string' ? { nodeType: 3, textContent: node } : node);
    this.children = this.childNodes.filter(node => node.nodeType === 1);
    this.children.forEach(child => { child.parentElement = this; });
    this.rect ??= { x: 0, y: 0, width: 100, height: 20 };
  }
  get id() { return this.attrs.id || ''; }
  get name() { return this.attrs.name || ''; }
  get type() { return this.attrs.type || (this.tagName === 'BUTTON' ? 'submit' : this.tagName === 'INPUT' ? 'text' : ''); }
  get title() { return this.attrs.title || ''; }
  get placeholder() { return this.attrs.placeholder || ''; }
  get htmlFor() { return this.attrs.for || ''; }
  get form() { return this.closest('form'); }
  get isConnected() { return this.connected && (!this.parentElement || this.parentElement.isConnected); }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  get innerText() { return this.textContent; }
  get descendants() { return this.children.flatMap(child => [child, ...child.descendants]); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  matches(selector) { return selector.split(',').some(s => matchesOne(this, s)); }
  querySelectorAll(selector) { return this.descendants.filter(el => el.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let el = this; el; el = el.parentElement) if (el.matches(selector)) return el;
    return null;
  }
  focus() { this.ownerDocument.activeElement = this; }
  click() { this.clicks++; }
  scrollIntoView() {}
  dispatchEvent(event) { this.events.push(event); return true; }
  getClientRects() { return [this.rect]; }
  getBoundingClientRect() { const r = this.rect; return { ...r, left: r.x, top: r.y }; }
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);
const field = (attrs, props = {}) => new Element(attrs.tag || 'INPUT', attrs, [], { value: '', ...props });

const style = { display: 'block', visibility: 'visible', paddingLeft: '0px', paddingTop: '0px',
  getPropertyValue: () => '' };

class Document {
  constructor(body, { origin = ORIGIN, frameElement = null, at = () => null } = {}) {
    this.body = body;
    // Not documentElement: the content script then skips its page-script injection.
    this.root = h('HTML', {}, body);
    this.defaultView = { origin, frameElement, getComputedStyle: () => style };
    this.activeElement = body;
    this.at = at;
    for (const el of [this.root, ...this.root.descendants]) el.ownerDocument = this;
  }
  querySelectorAll(selector) { return this.root.querySelectorAll(selector); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getElementById(id) { return this.querySelectorAll(`#${id}`)[0] || null; }
  elementFromPoint(x, y) { return this.at(x, y); }
}

// A frame at (left, top) with a 2px border; doc() builds the document it holds.
function frame(attrs, rect, doc) {
  const el = new Element('IFRAME', attrs, [], { rect, clientLeft: 2, clientTop: 2 });
  if (doc) el.contentDocument = doc(el);
  return el;
}

// The page from #157: an address editor in a same-origin iframe, a cross-origin payment
// widget and same-origin frames nested deeper than the depth bound.
function page() {
  const zip = field({ id: 'zip', name: 'zip' }, { rect: { x: 10, y: 10, width: 120, height: 20 } });
  const detail = field({ name: 'detail', placeholder: '상세주소' }, { value: '101호' });
  const pin = field({ name: 'pin', type: 'password' }, { value: SECRET });
  const save = h('BUTTON', { type: 'submit' }, '저장');
  save.rect = { x: 10, y: 100, width: 60, height: 24 };
  const addressForm = h('FORM', { action: '/address', method: 'post' },
    h('LABEL', { for: 'zip' }, '우편번호'), zip, detail, pin, save);
  const address = frame({ title: '배송지 편집', src: '/address-editor' }, { x: 50, y: 200, width: 400, height: 300 },
    el => new Document(h('BODY', {}, h('H2', {}, '배송지'), addressForm), {
      frameElement: el,
      at: (x, y) => (x >= 10 && x <= 70 && y >= 100 && y <= 124 ? save : null),
    }));
  const payment = frame({ title: 'Payment', src: 'https://pay.example/widget?session=TOKEN#card' },
    { x: 0, y: 600, width: 300, height: 100 },
    () => new Document(h('BODY', {}, field({ name: 'card' })), { origin: 'https://pay.example' }));
  const pixel = frame({ src: 'https://ads.example/p' }, { x: 0, y: 0, width: 0, height: 0 });
  // frames[0] holds frames[1] holds frames[2] holds frames[3]: the fourth is too deep.
  let innermost;
  const nest = level => frame({ title: `level ${level}` }, { x: 0, y: 0, width: 200, height: 100 },
    el => new Document(h('BODY', {}, level < 4 ? nest(level + 1) : (innermost = h('BUTTON', {}, 'Deep'))),
      { frameElement: el }));
  const nested = nest(1);
  const search = field({ name: 'q', 'aria-label': 'Search' });
  const body = h('BODY', {}, h('MAIN', {}, h('H1', {}, 'Checkout'), search, address, payment, pixel, nested));
  const top = new Document(body, { at: (x, y) => (y >= 200 && y < 500 && x >= 50 ? address : body) });
  return { top, zip, detail, pin, save, addressForm, address, search, innermost };
}

function load(p) {
  let listener;
  const Event = class { constructor(type, options) { Object.assign(this, { type }, options); } };
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
    Event, KeyboardEvent: Event, MouseEvent: Event, InputEvent: Event,
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    location: { href: `${ORIGIN}/checkout`, origin: ORIGIN },
    window: { origin: ORIGIN, getComputedStyle: () => style },
    document: p.top,
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  return msg => new Promise(resolve => listener(msg, {}, resolve));
}

const refOf = (snapshot, line) => {
  const match = new RegExp(`${line} \\[ref=(s\\d+e\\d+)\\]`).exec(snapshot);
  assert.ok(match, `${line} in\n${snapshot}`);
  return match[1];
};

(async () => {
  const p = page();
  const send = load(p);

  // Snapshot: the frame is a node with its content below it; the cross-origin frame is
  // listed without its query, the 0x0 tracking frame is not, and nesting is bounded.
  const { snapshot } = await send({ action: 'getSnapshot' });
  const lines = snapshot.split('\n');
  const frameLine = lines.findIndex(line => line.includes('iframe "배송지 편집"'));
  assert.ok(frameLine >= 0, snapshot);
  const zipLine = lines.findIndex(line => line.includes('textbox "우편번호"'));
  assert.ok(zipLine > frameLine, 'frame content follows the frame line');
  assert.ok(lines[zipLine].search(/\S/) > lines[frameLine].search(/\S/), 'frame content is nested under it');
  assert.match(snapshot, /textbox "상세주소" \[ref=s\d+e\d+\] \[value="101호"\]/);
  assert.match(snapshot, /textbox "" \[ref=s\d+e\d+\]$/m, 'password field is listed without value');
  assert.match(snapshot, /iframe "Payment" \[ref=s\d+e\d+\] \[content=cross_origin src="https:\/\/pay\.example\/widget"\]/);
  assert.doesNotMatch(snapshot, /TOKEN|ads\.example|textbox "card"|Deep/);
  assert.match(snapshot, /iframe "level 3"/);
  assert.match(snapshot, /iframe "level 4" \[ref=s\d+e\d+\] \[content=too_deep\]$/m);
  assert.ok(!snapshot.includes(SECRET));

  // Refs from the snapshot act on the frame's elements.
  const zipRef = refOf(snapshot, 'textbox "우편번호"');
  const fill = await send({ action: 'fill', selector: zipRef, value: '06236' });
  assert.equal(fill.ok, true, JSON.stringify(fill));
  assert.equal(p.zip.value, '06236');
  assert.deepEqual(p.zip.events.map(e => e.type), ['input', 'change']);
  const saveRef = refOf(snapshot, 'button "저장"');
  const click = await send({ action: 'click', selector: saveRef });
  assert.equal(click.ok, true, JSON.stringify(click));
  assert.equal(p.save.clicks, 1);
  assert.equal(p.save.events[0].view, p.address.contentDocument.defaultView, 'events carry the frame window');

  // CSS selectors fall through to frames; type and select see the same fields.
  const typed = await send({ action: 'type', selector: 'input[name=detail]', text: '202호', clear: true });
  assert.equal(typed.ok, true, JSON.stringify(typed));
  assert.equal(p.detail.value, '202호');

  // form_fill by label and placeholder inside the frame; submit without a target
  // submits the frame's form.
  const filled = await send({ action: 'formFill', fields: { '우편번호': '04524', '상세주소': '303호' } });
  assert.deepEqual(Object.values(filled.results).map(r => r.ok), [true, true], JSON.stringify(filled));
  assert.equal(p.zip.value, '04524');
  assert.equal(p.detail.value, '303호');
  const submitted = await send({ action: 'submitForm' });
  assert.equal(submitted.ok, true, JSON.stringify(submitted));
  assert.equal(submitted.form.action, '/address');
  assert.equal(p.save.clicks, 2);

  // browser_forms: frame fields name their frame, bounds are in the top viewport, the
  // password stays redacted, and the cross-origin frame is reported.
  const forms = await send({ action: 'getFormFields' });
  const zipField = forms.fields.find(f => f.name === 'zip');
  assert.equal(zipField.frame, '배송지 편집');
  assert.equal(zipField.label, '우편번호');
  assert.deepEqual([...zipField.bounds], [50 + 2 + 10, 200 + 2 + 10, 120, 20]);
  const pinField = forms.fields.find(f => f.name === 'pin');
  assert.equal(pinField.redacted, true);
  assert.equal(pinField.value, undefined);
  assert.equal(forms.fields.find(f => f.name === 'q').frame, undefined, 'top-document fields have no frame');
  assert.ok(!JSON.stringify(forms).includes(SECRET));
  assert.deepEqual(JSON.parse(JSON.stringify(forms.unavailableFrames.map(({ title, src, content }) => ({ title, src, content })))), [
    { title: 'Payment', src: 'https://pay.example/widget', content: 'cross_origin' },
    { title: 'level 4', src: '', content: 'too_deep' },
  ]);
  const unavailable = forms.unavailableFrames[0].ref;
  assert.equal((await send({ action: 'focusElement', selector: unavailable })).ok, true);
  assert.equal(p.top.activeElement.title, 'Payment', 'its ref names the frame element');

  // find and dom reach the frame too.
  const found = await send({ action: 'find', query: '우편번호' });
  assert.ok(found.results.some(r => r.tag === 'label' && r.selector === 'form > label' && r.bounds[0] === 52), JSON.stringify(found));
  const dom = await send({ action: 'getAccessibilityTree', depth: 12 });
  const domJson = JSON.stringify(dom.tree);
  assert.match(domJson, /"name":"zip"/);
  assert.match(domJson, /"title":"Payment","content":"cross_origin","src":"https:\/\/pay\.example\/widget"/);
  assert.ok(!domJson.includes(SECRET));

  // A coordinate click inside the frame hits the frame's button at frame-local coordinates.
  const coords = await send({ action: 'click', coords: { x: 50 + 2 + 20, y: 200 + 2 + 110 } });
  assert.equal(coords.ok, true, JSON.stringify(coords));
  assert.equal(coords.tag, 'button');
  assert.equal(p.save.clicks, 3);
  const down = p.save.events.filter(e => e.type === 'mousedown').at(-1);
  assert.deepEqual([down.clientX, down.clientY], [20, 110]);

  // A selector matching fields in the page and in the frame is ambiguous.
  const ambiguous = await send({ action: 'fill', selector: 'input', value: 'x' });
  assert.equal(ambiguous.code, 'ambiguous_target', JSON.stringify(ambiguous));

  // Keypresses without a target go to the element focused inside the focused frame.
  p.top.activeElement = p.address;
  p.address.contentDocument.activeElement = p.detail;
  const key = await send({ action: 'keypress', key: 'Tab' });
  assert.equal(key.target, 'input');
  assert.equal(p.detail.events.at(-1).type, 'keyup');

  // Selectors never reach the too-deep frame or the cross-origin one.
  assert.equal((await send({ action: 'click', selector: 'button' , first: true })).text, '저장');
  assert.equal((await send({ action: 'fill', selector: 'input[name=card]', value: 'x' })).code, 'not_found');
  assert.equal(p.innermost.clicks, 0);

  console.log('same_origin_frames: ok');
})().catch(error => { console.error(error); process.exit(1); });
