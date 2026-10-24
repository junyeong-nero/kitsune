// Run with: node tests/playwright_selectors.cjs (no browser or npm dependencies).
// #219: targets in the Playwright selector forms agents write (text=, role=, :has-text(),
// :text-is(), >> chains, nth=, xpath=..) resolve, strictly; other Playwright syntax, invalid
// CSS and refs used as CSS fail with unsupported_selector (not retryable), not not_found.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Text {
  constructor(text) { Object.assign(this, { nodeType: 3, textContent: text }); }
}

const syntaxError = selector => new DOMException(`'${selector}' is not a valid selector`, 'SyntaxError');

// Compounds of tag, #id, .class, [attr], [attr=v], [attr="v"] and :disabled, joined by
// descendant or child combinators, in comma lists. Anything else is a SyntaxError.
const COMPOUND = /^([\p{L}\p{N}-]+|\*)?((?:[.#][\w-]+|\[[\w-]+(?:=(?:"[^"]*"|[\w-]+))?\])*)(:disabled)?$/u;
const SIMPLE = /\.([\w-]+)|#([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|([\w-]+)))?\]/g;
function compoundMatches(el, compound, selector) {
  const m = COMPOUND.exec(compound);
  if (!m) throw syntaxError(selector);
  const [, tag, rest, disabled] = m;
  if (tag && tag !== '*' && el.tagName !== tag.toUpperCase()) return false;
  if (disabled && !('disabled' in el.attrs)) return false;
  for (const [, cls, id, name, quoted, bare] of rest.matchAll(SIMPLE)) {
    const value = quoted ?? bare;
    if (cls && !String(el.attrs.class || '').split(' ').includes(cls)) return false;
    if (id && el.attrs.id !== id) return false;
    if (name && (value === undefined ? !(name in el.attrs) : el.attrs[name] !== value)) return false;
  }
  return true;
}
// selector split at each separator outside quotes.
function splitOutsideQuotes(selector, separator) {
  const parts = [''];
  let quoted = false;
  for (const c of selector) {
    if (c === '"') quoted = !quoted;
    if (!quoted && separator.test(c)) parts.push(c.trim() ? c : ' ', '');
    else parts[parts.length - 1] += c;
  }
  return parts;
}
function complexMatches(el, steps, selector) {
  const [compound, combinator] = steps[steps.length - 1];
  if (!compoundMatches(el, compound, selector)) return false;
  if (steps.length === 1) return true;
  const rest = steps.slice(0, -1);
  for (let up = el.parentElement; up; up = up.parentElement) {
    if (complexMatches(up, rest, selector)) return true;
    if (combinator === '>') break;
  }
  return false;
}
function selectorMatches(el, selector) {
  return splitOutsideQuotes(selector, /,/).filter((_, i) => i % 2 === 0).some(alternative => {
    const tokens = splitOutsideQuotes(alternative.trim(), /[\s>]/).map(token => token.trim()).filter(Boolean);
    const steps = [];
    let combinator = ' ';
    for (const token of tokens) {
      if (token === '>') combinator = '>';
      else { steps.push([token, combinator]); combinator = ' '; }
    }
    if (!steps.length) throw syntaxError(selector);
    return complexMatches(el, steps, selector);
  });
}

class Element extends EventTarget {
  constructor(tagName, attrs = {}, childNodes = []) {
    super();
    this.childNodes = childNodes.map(child => typeof child === 'string' ? new Text(child) : child);
    Object.assign(this, { nodeType: 1, tagName: tagName.toUpperCase(), attrs, parentElement: null,
      isConnected: true, events: [] });
    if (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(this.tagName)) this.value = attrs.value || '';
    for (const child of this.children) child.parentElement = this;
    for (const type of ['click', 'mouseover', 'input', 'change']) {
      this.addEventListener(type, event => this.events.push(event.type));
    }
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  get firstElementChild() { return this.children[0] || null; }
  get previousElementSibling() {
    const siblings = this.parentElement?.children || [];
    return siblings[siblings.indexOf(this) - 1] || null;
  }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  get innerText() { return this.textContent; }
  get id() { return this.attrs.id || ''; }
  get name() { return this.attrs.name || ''; }
  get type() { return this.attrs.type || (this.tagName === 'BUTTON' ? 'submit' : 'text'); }
  get placeholder() { return this.attrs.placeholder || ''; }
  get hidden() { return 'hidden' in this.attrs; }
  get href() { return this.attrs.href; }
  get labels() {
    let root = this;
    while (root.parentElement) root = root.parentElement;
    const wrapping = this.closest('label');
    return [...root.descendants().filter(el => el.tagName === 'LABEL' && this.id && el.attrs.for === this.id),
      ...(wrapping ? [wrapping] : [])];
  }
  get protocol() { return new URL(this.attrs.href).protocol; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, value) { this.attrs[key] = value; }
  removeAttribute(key) { delete this.attrs[key]; }
  matches(selector) { return selectorMatches(this, selector); }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  querySelectorAll(selector) { return this.descendants().filter(el => el.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let el = this; el; el = el.parentElement) if (el.matches(selector)) return el;
    return null;
  }
  getClientRects() {
    for (let el = this; el; el = el.parentElement) if (el.hidden) return [];
    return [{}];
  }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 }; }
  scrollIntoView() {}
  focus() {}
  click() { this.dispatchEvent(new Event('click', { bubbles: true })); }
}

const h = (tag, attrs, ...children) => new Element(tag, attrs, children);

// A shadow root attached to host; its children have no parent element.
class ShadowRoot {
  constructor(host, children) {
    Object.assign(this, { nodeType: 11, host, childNodes: children });
    for (const child of children) Object.assign(child, { parentElement: null, parentNode: this });
    host.shadowRoot = this;
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  querySelectorAll(selector) { return this.descendants().filter(el => el.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getElementById(id) { return this.descendants().find(el => el.id === id) || null; }
}

// The XPath the tests write: [.]//tag with a [text()='…'] (or "…") or [@attr='…'] predicate, then /.. or
// /text(). Anything else is a SyntaxError, as document.evaluate throws.
const XPATH = /^(\.)?\/\/([\w*]+)(?:\[(?:text\(\)=(?:'([^']*)'|"([^"]*)")|@([\w-]+)='([^']*)')\])?(\/\.\.|\/text\(\))?$/;
function compileXPath(expression) {
  const m = XPATH.exec(expression);
  if (!m) throw new DOMException(`'${expression}' is not a valid XPath expression`, 'SyntaxError');
  return m;
}
function evaluateXPath(dom, expression, context) {
  dom.xpaths.push(expression);
  const [, relative, tag, single, double, attr, value, step] = compileXPath(expression);
  const text = single ?? double;
  let nodes = (relative ? context.descendants() : [dom.html, ...dom.html.descendants()])
    .filter(el => (tag === '*' || el.tagName === tag.toUpperCase()) &&
      (text === undefined || el.childNodes.some(node => node.nodeType === 3 && node.textContent === text)) &&
      (attr === undefined || el.attrs[attr] === value));
  if (step === '/..') nodes = [...new Set(nodes.map(el => el.parentElement).filter(Boolean))];
  if (step === '/text()') nodes = nodes.flatMap(el => el.childNodes.filter(node => node.nodeType === 3));
  return { snapshotLength: nodes.length, snapshotItem: i => nodes[i] };
}

function page() {
  const options = [h('li', { role: 'option' }, '캐나다'), h('li', { role: 'option' }, '베트남')];
  const nativeOption = h('option', {}, '베트남');
  const boxes = [h('input', { type: 'checkbox', id: 'usb' }), h('input', { type: 'checkbox', id: 'kb' })];
  const pages = [h('button', { id: 'p1' }, '1'), h('button', { id: 'p2' }, '2'), h('button', { id: 'p12' }, '12')];
  const saves = [h('button', { class: 's' }, '저장'), h('button', { class: 's', id: 'last-save' }, '저장')];
  const policy = h('a', { id: 'policy', href: 'https://example.com/p' }, '공식 정책 — 2027년 ', h('b', {}, '현재'), ' 적용');
  const mic = h('input', { type: 'checkbox', id: 'mic' });
  const secret = h('span', { id: 'secret', 'data-mask': '' }, 'hunter2');
  const list = h('ul', { id: 'countries', role: 'listbox' }, ...options);
  const body = h('body', {},
    list,
    h('select', { id: 'native', hidden: '' }, nativeOption),
    h('table', {}, h('tr', {}, h('td', {}, 'USB 허브'), h('td', {}, boxes[0])),
      h('tr', {}, h('td', {}, '키보드'), h('td', {}, boxes[1]))),
    ...pages,
    h('div', { class: 'save' }, saves[0]), h('div', { class: 'save' }, saves[1]),
    h('p', {}, policy),
    mic, h('label', { for: 'mic' }, '마이크 선택'),
    h('input', { type: 'submit', id: 'send', value: '보내기' }),
    secret,
    h('script', {}, '베트남'));
  const html = h('html', {}, body);
  return { html, body, list, options, nativeOption, boxes, pages, saves, policy, mic, secret, xpaths: [] };
}

// #240: the gallery, table and meeting-notes pages of the benchmark's remaining selector errors.
// It holds no redacted text, so XPath may run.
function galleryPage() {
  const cell = (n, attrs = {}) => h('button', { id: `cell${n}`, 'aria-label': `${n}번 칸`, ...attrs },
    h('img', { id: `cell${n}-img`, alt: `${n}번 칸 그림` }));
  const body = h('body', {},
    h('img', { id: 'puzzle', alt: '왜곡된 문자 퍼즐' }),
    h('div', { id: 'grid' }, h('div', { id: 'cell1-wrap' }, cell(1)), cell(2), cell(7)),
    h('section', { id: 'stock' }, h('h2', { id: 'stock-title' }, '재고 표'), h('p', { id: 'stock-note' }, '사과 3개')),
    h('input', { type: 'checkbox', id: 'mic' }), h('label', { for: 'mic' }, '마이크 선택'),
    h('label', { id: 'volume-label' }, '음량 ', h('input', { type: 'range', id: 'volume' })),
    h('span', { id: 't71' }, '#71 완료'), h('span', { id: 't8' }, '#8 진행'),
    h('button', { id: 'save68', 'aria-label': '회의록 #68 저장' }, '저장'),
    h('button', { id: 'save66' }, '회의록 #66 저장'),
    h('img', { id: 'chart', 'aria-labelledby': 'chart-caption' }), h('span', { id: 'chart-caption' }, '매출 차트'),
    h('input', { type: 'image', id: 'search', alt: '검색' }),
    h('map', {}, h('area', { id: 'seoul', href: 'https://example.com/seoul', alt: '서울' })),
    h('svg', { id: 'logo', role: 'img' }, h('title', {}, '로고'), h('desc', {}, '회사 로고 설명')),
    h('img', { id: 'spacer', alt: '', title: '간격' }),
    h('img', { id: 'zoom', alt: 'Zoom in', title: 'Magnifier', tabindex: '0' }),
    h('span', { id: 'dont' }, "Don't stop"), h('h2', { id: 'its' }, "It's here"),
    h('span', { id: 'chevron' }, 'Next >> page'), h('code', { id: 'endpoint' }, 'GET /api/users'),
    h('span', { id: 'email-label' }, 'Email Address'), h('input', { id: 'email', 'aria-labelledby': 'email-label' }));
  return { html: h('html', {}, body), body, xpaths: [] };
}

// A sign-in form inside a shadow root: XPath scoped to the form evaluates in that root.
function shadowPage(...fields) {
  const host = h('div', { id: 'host' });
  new ShadowRoot(host, [h('form', { class: 'in-shadow' }, ...fields)]);
  const body = h('body', {}, h('h2', { id: 'title' }, 'Sign in'), host);
  return { html: h('html', {}, body), body, xpaths: [] };
}

// The real-browser screenshot fixture: "capture" is in two buttons in nested shadow roots and
// in the inline script and style; the heading says "Capture formats".
function captureShadowPage() {
  const hosts = ['open', 'closed'].map(mode => {
    const host = h('capture-host', {});
    const inner = h('capture-inner', {});
    new ShadowRoot(host, [inner]);
    new ShadowRoot(inner, [h('button', { id: `${mode}-capture` }, `${mode} shadow capture`)]);
    return host;
  });
  const buttons = hosts.map(host => host.shadowRoot.children[0].shadowRoot.children[0]);
  const body = h('body', {},
    h('style', {}, 'capture-host button { width: 160px; }'),
    h('h1', { id: 'heading' }, 'Capture formats'), h('div', { id: 'components' }, ...hosts),
    h('label', { for: 'email' }, 'Email', h('script', {}, 'trackCapture()')), h('input', { id: 'email' }),
    h('script', {}, "document.createElement('capture-host').attachShadow({ mode })"));
  return { html: h('html', {}, body), body, buttons, xpaths: [] };
}

// The same form inside a same-origin frame.
function framePage(...fields) {
  const xpaths = [];
  const body = h('body', {}, h('h2', { id: 'title' }, 'Sign in'),
    frame(xpaths, h('body', {}, h('form', { class: 'in-frame' }, ...fields))));
  return { html: h('html', {}, body), body, xpaths };
}

const fakeWindow = () => ({ getComputedStyle: el => ({ display: 'block', visibility: 'visible',
  webkitTextSecurity: 'data-mask' in el.attrs ? 'disc' : 'none', getPropertyValue: () => '' }) });

// A document over dom.html; XPath evaluations are recorded in dom.xpaths.
function fakeDocument(dom) {
  return {
    body: dom.body, documentElement: Object.assign(dom.html, { appendChild() {} }), activeElement: dom.body,
    createElement: () => ({ remove() {} }),
    getElementById: id => dom.html.querySelector('#' + id),
    querySelector: selector => dom.html.matches(selector) ? dom.html : dom.html.querySelector(selector),
    querySelectorAll: selector => [dom.html, ...dom.html.descendants()].filter(el => el.matches(selector)),
    createExpression: expression => { compileXPath(expression); return {}; },
    evaluate: (expression, context) => evaluateXPath(dom, expression, context),
  };
}

// A same-origin frame showing body (and its elements' shadow roots); XPath evaluations in it
// are recorded in xpaths.
function frame(xpaths, body) {
  const iframe = h('iframe', { id: 'frame' });
  const html = h('html', {}, body);
  const doc = fakeDocument({ html, body, xpaths });
  doc.defaultView = { ...fakeWindow(), frameElement: iframe };
  for (const el of [html, ...html.descendants(), ...html.descendants().flatMap(el => el.shadowRoot?.descendants() || [])]) {
    el.ownerDocument = doc;
  }
  iframe.contentDocument = doc;
  return iframe;
}

function load(dom) {
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent: Event, KeyboardEvent: Event, InputEvent: Event,
    CSS: { escape: x => x }, setTimeout, HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    XPathResult: { ORDERED_NODE_SNAPSHOT_TYPE: 7 },
    window: fakeWindow(),
    document: fakeDocument(dom),
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  // Results come from the vm realm, so compare plain copies.
  return msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
}

const clicks = elements => elements.map(el => el.events.filter(type => type === 'click').length);

async function clickOnly(selector, target, fixture = page) {
  const dom = fixture();
  const send = load(dom);
  const result = await send({ action: 'click', selector });
  assert.equal(result.ok, true, `${selector}: ${JSON.stringify(result)}`);
  const all = [dom.html, ...dom.html.descendants()];
  assert.deepEqual(all.filter(el => el.events.includes('click')).map(el => el.tagName + '#' + el.id),
    [target(dom)].map(el => el.tagName + '#' + el.id), selector);
  return { dom, send, result };
}

async function testReportedSelectorsResolve() {
  // ui-select: text and role selectors pick the visible option, not the hidden native one.
  await clickOnly('text=베트남', dom => dom.options[1]);
  await clickOnly('role=option[name="캐나다"]', dom => dom.options[0]);
  await clickOnly('[role=option]:has-text("베트남")', dom => dom.options[1]);
  await clickOnly('internal:role=option[name="베트"i]', dom => dom.options[1]);
  await clickOnly('internal:text="캐나"i', dom => dom.options[0]);
  // ui-table: exact text, and a row filter followed by more CSS.
  await clickOnly('button:text-is("2")', dom => dom.pages[1]);
  await clickOnly('internal:role=button[name="2"s]', dom => dom.pages[1]);
  await clickOnly('table tr:has-text("USB 허브") input[type=checkbox]', dom => dom.boxes[0]);
  await clickOnly('tr:has-text("키보드") > td > input', dom => dom.boxes[1]);
  // pb-research-01: lax text spanning child elements matches the smallest element holding it.
  await clickOnly('text=공식 정책 — 2027년 현재 적용', dom => dom.policy);
  await clickOnly('text="공식 정책 — 2027년 현재 적용"', dom => dom.policy);
  // ui-infinite: nth= after a pseudo-class, and chains that search inside earlier matches.
  await clickOnly('button:has-text("저장") >> nth=-1', dom => dom.saves[1]);
  await clickOnly('.save >> nth=1 >> button', dom => dom.saves[1]);
  await clickOnly('css=.save >> text=저장 >> nth=0', dom => dom.saves[0]);
  // A label names its checkbox; a button input's value is its text.
  await clickOnly('role=checkbox[name="마이크 선택"]', dom => dom.mic);
  await clickOnly('text=보내기', dom => dom.html.querySelector('#send'));
  await clickOnly('"보내기"', dom => dom.html.querySelector('#send'));
  await clickOnly('#usb:visible', dom => dom.boxes[0]);

  // xpath=.. is the parent; read actions resolve the same way.
  const dom = page();
  const send = load(dom);
  const parent = await send({ action: 'hover', selector: 'li:has-text("베트남") >> xpath=..' });
  assert.equal(parent.tag, 'ul', JSON.stringify(parent));
  assert.equal((await send({ action: 'hover', selector: 'li >> .. >> ..' })).tag, 'body');
  for (const selector of ['role=button[name="2"]', 'button:text-is("2")', 'text="2"']) {
    const marker = await send({ action: 'markScreenshotTarget', selector });
    assert.ok(marker.attribute, JSON.stringify(marker));
    assert.ok(dom.pages[1].hasAttribute(marker.attribute));
    await send({ action: 'unmarkScreenshotTarget', attribute: marker.attribute });
    assert.ok(!dom.pages[1].hasAttribute(marker.attribute));
  }
  const bounds = await send({ action: 'getBounds', selector: 'role=listbox' });
  assert.ok(!bounds.error, JSON.stringify(bounds));
}

async function testPlaywrightSelectorsStayStrict() {
  const cases = [
    ['text=저장', 2], ['button:has-text("저장")', 2], ['internal:role=button[name="2"i]', 2],
    ['.save >> button', 2], ['role=option', 2],
  ];
  for (const [selector, matches] of cases) {
    const dom = page();
    const send = load(dom);
    const result = await send({ action: 'click', selector });
    assert.equal(result.code, 'ambiguous_target', `${selector}: ${JSON.stringify(result)}`);
    assert.equal(result.retryable, false);
    assert.equal(result.state.matches, matches, selector);
    assert.deepEqual(clicks([...dom.saves, ...dom.pages, ...dom.options]), [0, 0, 0, 0, 0, 0, 0], selector);
    const first = await send({ action: 'click', selector, first: true });
    assert.equal(first.ok, true, `${selector} first: ${JSON.stringify(first)}`);
  }
}

async function testTextIsReadRedacted() {
  const dom = page();
  const send = load(dom);
  for (const selector of ['text=hunter2', 'span:has-text("hunter")', 'role=button[name="hunter2"]',
    'text=/hunter/', 'internal:text=/hunter2/']) {
    const result = await send({ action: 'hover', selector });
    assert.equal(result.code, 'not_found', `${selector}: ${JSON.stringify(result)}`);
  }
  // Script text is not page text.
  assert.equal((await send({ action: 'click', selector: 'text=베트남', first: true })).ok, true);
  assert.deepEqual(clicks([dom.html.querySelector('script')]), [0]);
}

async function testUnsupportedSyntaxIsNotRetryable() {
  const selectors = {
    // This page holds masked text, which XPath would read unredacted.
    'xpath=//button': /XPath on a page holding redacted text/,
    '//button[text()="2"]': /XPath on a page holding redacted text/,
    // XPath compiles before an earlier part is searched.
    '#missing >> xpath=((': /"\(\(" is not an XPath expression/,
    'text=/(/': /not a valid regular expression/,
    'role=button[name=/(/]': /not a valid regular expression/,
    'internal:label=마이크': /internal:label= takes a quoted string or \/regex\//,
    'button >> s1e2': /only start a >> chain/,
    // The background expands a short ref given alone; one used inside CSS reaches the page.
    'e197': /not an element name/,
    'button:near(#p1)': /:near\(\)/,
    'li:has(:has-text("베트남"))': /inside :has\(\)/,
    'role=button[pressed]': /\[pressed\]/,
    'role=button[name~="2"]': /role= attribute/,
    'role=button[name*=/2/]': /role= attribute/,
    'nth=0': /nth=/,
    'button >> ': /empty part/,
    'id=p1': /id= selector engine/,
    'checkbox "마이크 선택"': /snapshot line/,
    '저장': /not an element name/,
    'e12 > button': /not an element name/,
    'div[': /not valid CSS/,
  };
  for (const [selector, reason] of Object.entries(selectors)) {
    const dom = page();
    const send = load(dom);
    const result = await send({ action: 'click', selector });
    assert.equal(result.code, 'unsupported_selector', `${selector}: ${JSON.stringify(result)}`);
    assert.equal(result.retryable, false, selector);
    assert.match(result.error, reason, selector);
    assert.match(result.hint, /text=.*role=.*>> chains/, selector);
    assert.equal(result.state.selector, selector);
    const capture = await send({ action: 'markScreenshotTarget', selector });
    assert.equal(capture.code, 'unsupported_selector', selector);
    assert.equal(capture.retryable, false);
    assert.equal(dom.html.descendants().some(el => el.events.length), false, `${selector} dispatched nothing`);
    assert.deepEqual(dom.xpaths, [], `${selector} never evaluated XPath`);
  }

  // An actionability wait fails at once instead of polling until its timeout.
  const send = load(page());
  const started = Date.now();
  const waited = await send({ action: 'click', selector: 'xpath=//button', wait: true, timeout: 5000 });
  assert.equal(waited.code, 'unsupported_selector');
  assert.ok(Date.now() - started < 1000);
  const invalidAfterMiss = await send({ action: 'click', selector: '#missing >> xpath=((', wait: true, timeout: 5000 });
  assert.equal(invalidAfterMiss.code, 'unsupported_selector', JSON.stringify(invalidAfterMiss));
  assert.ok(Date.now() - started < 1000);
  // Read actions report it too.
  assert.equal((await send({ action: 'getBounds', selector: 'text=/(/' })).code, 'unsupported_selector');
}

const byId = (dom, id) => [dom.html, ...dom.html.descendants()].find(el => el.id === id);

// The element a read action (first match) resolves selector to.
async function readTarget(send, dom, selector) {
  const marker = await send({ action: 'markScreenshotTarget', selector });
  assert.ok(marker.attribute, `${selector}: ${JSON.stringify(marker)}`);
  const el = [dom.html, ...dom.html.descendants()].find(node => node.hasAttribute(marker.attribute));
  await send({ action: 'unmarkScreenshotTarget', attribute: marker.attribute });
  return el.id || el.tagName.toLowerCase();
}

// Hover, a strict pointer action, as in the issue's probe.
async function hoverOnly(selector, id) {
  const dom = galleryPage();
  const send = load(dom);
  const result = await send({ action: 'hover', selector });
  assert.equal(result.ok, true, `${selector}: ${JSON.stringify(result)}`);
  const all = [dom.html, ...dom.html.descendants()];
  assert.deepEqual(all.filter(el => el.events.includes('mouseover')).map(el => el.id), [id], selector);
}

async function testImagesMatchByAlternativeName() {
  // #240 table 1: role=img[name=…] matches the alt text the snapshot names the image by.
  await hoverOnly('role=img[name="왜곡된 문자 퍼즐"]', 'puzzle');
  await hoverOnly('role=img[name="2번 칸 그림"]', 'cell2-img');
  await hoverOnly('img[alt="2번 칸 그림"]', 'cell2-img');
  await hoverOnly('role=button[name="7번 칸"]', 'cell7');
  const dom = galleryPage();
  const send = load(dom);
  const all = await send({ action: 'hover', selector: 'role=img' });
  assert.equal(all.code, 'ambiguous_target', JSON.stringify(all));
  assert.equal((await send({ action: 'hover', selector: 'role=img', first: true })).ok, true);
  const snapshot = (await send({ action: 'getSnapshot' })).snapshot;
  assert.match(snapshot, /- img "왜곡된 문자 퍼즐" \[ref=/);
  assert.match(snapshot, /- img "2번 칸 그림" \[ref=/);

  // Other names that come from attributes: aria-labelledby, an image input's and an area's
  // alt, an SVG's <title> (not its <desc>), and substrings and regexes of those names.
  await hoverOnly('role=img[name="매출 차트"]', 'chart');
  await hoverOnly('role=button[name="검색"]', 'search');
  await hoverOnly('role=link[name="서울"]', 'seoul');
  await hoverOnly('role=img[name="로고"]', 'logo');
  await hoverOnly('internal:role=img[name="퍼즐"i]', 'puzzle');
  await hoverOnly('role=img[name=/^2번/]', 'cell2-img');
  // alt="" marks an image decorative; its title does not name it, as in the snapshot.
  const decorative = await send({ action: 'hover', selector: 'role=img[name="간격"]' });
  assert.equal(decorative.code, 'not_found', JSON.stringify(decorative));
  assert.doesNotMatch(snapshot, /간격/);
  // A clickable image is named by its alt before its title, in the snapshot as in role=.
  await hoverOnly('role=img[name="Zoom in"]', 'zoom');
  assert.match(snapshot, /- button "Zoom in" \[ref=/);
  assert.doesNotMatch(snapshot, /Magnifier/);
}

async function testRemainingPlaywrightForms() {
  // #240 table 2, each form as the model wrote it.
  const dom = galleryPage();
  const send = load(dom);
  assert.equal(await readTarget(send, dom, "xpath=//h2[text()='재고 표']/.."), 'stock');
  assert.equal(await readTarget(send, dom, "xpath=//button[@aria-label='1번 칸']/.."), 'cell1-wrap');
  assert.equal(await readTarget(send, dom, '//h2'), 'stock-title');
  await hoverOnly('internal:label="마이크 선택"i', 'mic');
  await hoverOnly('text=/#7\\d/', 't71');
  await hoverOnly('role=button[name=/#66 저장/]', 'save66');
  await hoverOnly('button[aria-label="회의록 #68 저장"], button:has-text("회의록 #68 저장")', 'save68');

  // XPath after >> is relative to the earlier match, and only elements are selected.
  dom.xpaths.length = 0;
  assert.equal(await readTarget(send, dom, '#grid >> xpath=//img'), 'cell1-img');
  assert.deepEqual(dom.xpaths, ['.//img']);
  const textNodes = await send({ action: 'hover', selector: 'xpath=//h2/text()' });
  assert.equal(textNodes.code, 'not_found', JSON.stringify(textNodes));
  const invalid = await send({ action: 'hover', selector: 'xpath=count(//h2)' });
  assert.equal(invalid.code, 'unsupported_selector', JSON.stringify(invalid));
  assert.match(invalid.error, /not an XPath expression that selects elements/);
  const ambiguousXPath = await send({ action: 'hover', selector: 'xpath=//button' });
  assert.equal(ambiguousXPath.code, 'ambiguous_target', 'XPath stays strict');

  // internal:label= is getByLabel: "…"i is a case-insensitive substring, "…" and "…"s the
  // whole label, /…/ a regex; aria-label and wrapping labels count.
  await hoverOnly('internal:label="마이크"i', 'mic');
  await hoverOnly('internal:label="마이크 선택"s', 'mic');
  await hoverOnly('internal:label=/^마이크/', 'mic');
  await hoverOnly('internal:label="회의록 #68 저장"', 'save68');
  await hoverOnly('internal:label="음량"i', 'volume');
  const partial = await send({ action: 'hover', selector: 'internal:label="마이크"' });
  assert.equal(partial.code, 'not_found', JSON.stringify(partial));
  // aria-labelledby names a field; "…"i ignores ASCII case, "…" does not.
  await hoverOnly('internal:label="email address"i', 'email');
  await hoverOnly('internal:label="Email Address"', 'email');
  await hoverOnly('internal:label=/^EMAIL/i', 'email');
  const exactCase = await send({ action: 'hover', selector: 'internal:label="email address"' });
  assert.equal(exactCase.code, 'not_found', JSON.stringify(exactCase));
  // Regex text matches the smallest element, as lax text does; flags apply.
  await hoverOnly('text=/#71 완료/', 't71');
  await hoverOnly('internal:text=/#8 진행/i', 't8');
  await hoverOnly('text=/^회의록 #6\\d/ >> nth=0', 'save66');
  const regexAmbiguous = await send({ action: 'hover', selector: 'role=button[name=/번 칸$/]' });
  assert.equal(regexAmbiguous.code, 'ambiguous_target');
  assert.equal(regexAmbiguous.state.matches, 3);
}

async function testRefStartsChain() {
  // #240: e95 >> xpath=.. and e95 >> text=… resolve the ref, then the chain inside it.
  const dom = galleryPage();
  const send = load(dom);
  const snapshot = (await send({ action: 'getSnapshot' })).snapshot;
  const ref = label => new RegExp(`- ${label} \\[ref=(s\\d+e\\d+)\\]`).exec(snapshot)?.[1];
  const image = ref('img "2번 칸 그림"');
  assert.ok(image, snapshot);
  assert.equal(await readTarget(send, dom, `${image} >> xpath=..`), 'cell2');
  assert.equal(await readTarget(send, dom, `${image} >> .. >> ..`), 'grid');
  const button = ref('button "7번 칸"');
  assert.equal(await readTarget(send, dom, `${button} >> role=img`), 'cell7-img');
  assert.equal(await readTarget(send, dom, `${button} >> xpath=//img`), 'cell7-img');
  const result = await send({ action: 'hover', selector: `${image} >> xpath=..` });
  assert.equal(result.ok, true, JSON.stringify(result));
  assert.deepEqual(byId(dom, 'cell2').events, ['mouseover']);
  const missing = await send({ action: 'hover', selector: `${button} >> text=없음` });
  assert.equal(missing.code, 'not_found');

  // A ref the page does not know is stale, as on its own; one later in a chain is refused.
  for (const selector of ['e412 >> xpath=..', 's1e999 >> button', `s9${image.slice(2)} >> text=x`]) {
    const stale = await send({ action: 'hover', selector });
    assert.equal(stale.code, 'stale_ref', `${selector}: ${JSON.stringify(stale)}`);
  }
  const later = await send({ action: 'hover', selector: `#grid >> ${image}` });
  assert.equal(later.code, 'unsupported_selector');
  assert.match(later.error, /only start a >> chain/);

  // A chain starting at a ref whose element was removed is stale, not a miss.
  const cell7 = byId(dom, 'cell7');
  cell7.parentElement.childNodes.splice(cell7.parentElement.childNodes.indexOf(cell7), 1);
  cell7.isConnected = false;
  const removed = await send({ action: 'hover', selector: `${button} >> role=img` });
  assert.equal(removed.code, 'stale_ref', JSON.stringify(removed));
  assert.equal(removed.state.reason, 'element_removed');
  assert.equal(removed.state.ref, button);
}

async function testRegexValuesAndQuotesInChains() {
  // A /…/ value is a regex only when flags (or nothing) follow its last /; otherwise text= is
  // lax text, as in Playwright.
  await hoverOnly('text=/api/users', 'endpoint');
  await hoverOnly('text=/API\\/USERS$/i', 'endpoint');
  const dom = galleryPage();
  const send = load(dom);
  const internal = await send({ action: 'hover', selector: 'internal:text=/api/users' });
  assert.equal(internal.code, 'unsupported_selector', JSON.stringify(internal));
  assert.equal((await send({ action: 'hover', selector: 'text=/api/x' })).code, 'not_found');

  // An apostrophe inside a regex, or inside a lax text= value, does not open a string; a
  // quoted >> stays part of its string. Regex flags ignore ASCII case.
  await hoverOnly("text=/don't/i >> nth=0", 'dont');
  await hoverOnly("text=don't STOP >> nth=0", 'dont');
  await hoverOnly("role=heading[name=/it's/i][level=2]", 'its');
  await hoverOnly("role=heading[name=/it's/i][level=2] >> nth=0", 'its');
  await hoverOnly('text="Next >> page"', 'chevron');
  await hoverOnly("text='Next >> page' >> nth=0", 'chevron');
  await hoverOnly('span:has-text("next >> page") >> nth=0', 'chevron');
  await hoverOnly('role=heading[name="It\'s here"] >> nth=0', 'its');
  const caseSensitive = await send({ action: 'hover', selector: "text=/don't/ >> nth=0" });
  assert.equal(caseSensitive.code, 'not_found', JSON.stringify(caseSensitive));
}

async function testScriptTextNeverMatchesAndShadowTextDoes() {
  // #240: script and style text is not page text, for regexes as for lax text, so the body
  // holding the script is never the smallest match. Shadow buttons match either way.
  const dom = captureShadowPage();
  const send = load(dom);
  for (const selector of ['text=/capture-host/', 'text=capture-host', 'internal:text=/attachShadow/',
    'text=/width: 160px/', 'text=/trackCapture/', 'internal:label=/trackCapture/']) {
    const result = await send({ action: 'hover', selector });
    assert.equal(result.code, 'not_found', `${selector}: ${JSON.stringify(result)}`);
  }
  for (const [selector, matches] of [['text=/capture/', 2], ['internal:text=/shadow capture$/', 2],
    ['text=capture', 3]]) {
    const result = await send({ action: 'hover', selector });
    assert.equal(result.code, 'ambiguous_target', `${selector}: ${JSON.stringify(result)}`);
    assert.equal(result.state.matches, matches, selector);
  }
  assert.equal((await send({ action: 'hover', selector: 'text=/capture/ >> nth=1' })).ok, true);
  assert.equal((await send({ action: 'hover', selector: 'text=/^Capture formats$/' })).ok, true);
  assert.equal((await send({ action: 'hover', selector: 'internal:label=/^Email$/' })).ok, true);
  const hovered = el => el.events.includes('mouseover');
  assert.deepEqual(dom.buttons.map(hovered), [false, true]);
  assert.deepEqual([dom.html, ...dom.html.descendants()].filter(hovered).map(el => el.id), ['heading', 'email']);
}

async function testXPathGuardCoversShadowRootsAndFrames() {
  // #240: a scope inside a shadow root evaluates XPath in that root, so a sensitive value
  // attribute or masked text there refuses XPath, scoped or not, before it runs.
  const probe = 'css=form.in-shadow >> xpath=.//input';
  let dom = shadowPage(h('input', { type: 'password', id: 'pw' }));
  let send = load(dom);
  const clean = await send({ action: 'hover', selector: probe });
  assert.equal(clean.ok, true, JSON.stringify(clean));
  assert.deepEqual(dom.xpaths, ['.//input']);
  const redacted = {
    password: () => h('input', { type: 'password', id: 'pw', value: 'hunter2' }),
    masked: () => h('span', { id: 'otp', 'data-mask': '' }, '483920'),
  };
  for (const [kind, field] of Object.entries(redacted)) {
    for (const selector of [probe, 'form.in-shadow >> xpath=.//span', 'xpath=//h2']) {
      dom = shadowPage(field());
      send = load(dom);
      const refused = await send({ action: 'hover', selector });
      assert.equal(refused.code, 'unsupported_selector', `${kind} ${selector}: ${JSON.stringify(refused)}`);
      assert.match(refused.error, /XPath on a page holding redacted text/);
      assert.deepEqual(dom.xpaths, [], `${kind} ${selector} never evaluated XPath`);
    }
  }

  // Same-origin frames: XPath runs in each frame document, which is checked with its shadow roots.
  dom = framePage(h('input', { type: 'password', id: 'pw' }));
  send = load(dom);
  const inFrame = await send({ action: 'hover', selector: 'form.in-frame >> xpath=.//input' });
  assert.equal(inFrame.ok, true, JSON.stringify(inFrame));
  assert.deepEqual(dom.xpaths, ['.//input']);
  const shadowInFrame = () => {
    const host = h('div', { id: 'widget' });
    new ShadowRoot(host, [h('b', { 'data-mask': '' }, '483920')]);
    return host;
  };
  for (const [kind, field] of Object.entries({ ...redacted, shadowInFrame })) {
    for (const selector of ['form.in-frame >> xpath=.//input', 'xpath=//h2']) {
      dom = framePage(h('input', { type: 'password', id: 'pw' }), field());
      send = load(dom);
      const refused = await send({ action: 'hover', selector });
      assert.equal(refused.code, 'unsupported_selector', `${kind} ${selector}: ${JSON.stringify(refused)}`);
      assert.deepEqual(dom.xpaths, [], `${kind} ${selector} never evaluated XPath`);
    }
  }
}

async function testSelectorListsAndScopedMisses() {
  // #240: a CSS list with Playwright pseudo-classes is a union, then strict.
  const dom = galleryPage();
  const send = load(dom);
  const both = await send({ action: 'hover',
    selector: 'button[aria-label="회의록 #68 저장"], button:has-text("#66")' });
  assert.equal(both.code, 'ambiguous_target', JSON.stringify(both));
  assert.equal(both.state.matches, 2);
  const list = 'button[aria-label="회의록 #69 저장"], button:has-text("회의록 #69 저장")';
  const missing = await send({ action: 'hover', selector: list });
  assert.equal(missing.code, 'not_found', JSON.stringify(missing));
  // A scoped snapshot miss is a not_found with a hint, not a bare "Selector not found".
  for (const action of ['getSnapshot', 'getAccessibilityTree']) {
    const scoped = await send({ action, selector: list });
    assert.equal(scoped.code, 'not_found', `${action}: ${JSON.stringify(scoped)}`);
    assert.equal(scoped.error, `Selector not found: ${list}`);
    assert.match(scoped.hint, /browser_snapshot/);
    assert.deepEqual(scoped.state, { selector: list });
  }
  const unsupported = await send({ action: 'getSnapshot', selector: 'button[aria-label="x"], button:near(#cell1)' });
  assert.equal(unsupported.code, 'unsupported_selector', JSON.stringify(unsupported));
}

async function testPlainCssIsUnchanged() {
  const dom = page();
  const send = load(dom);
  assert.equal((await send({ action: 'click', selector: '#p2' })).ok, true);
  assert.deepEqual(clicks(dom.pages), [0, 1, 0]);
  assert.equal((await send({ action: 'click', selector: '.s' })).code, 'ambiguous_target');
  const missing = await send({ action: 'click', selector: '#missing' });
  assert.equal(missing.code, 'not_found');
  assert.equal(missing.retryable, true);
  // One rendered match among hidden ones is the target, for CSS too.
  assert.equal((await send({ action: 'click', selector: 'option, #p1' })).ok, true);
  assert.deepEqual(clicks([dom.nativeOption, ...dom.pages]), [0, 1, 1, 0]);
}

(async () => {
  await testReportedSelectorsResolve();
  await testPlaywrightSelectorsStayStrict();
  await testTextIsReadRedacted();
  await testUnsupportedSyntaxIsNotRetryable();
  await testPlainCssIsUnchanged();
  await testImagesMatchByAlternativeName();
  await testRemainingPlaywrightForms();
  await testRefStartsChain();
  await testSelectorListsAndScopedMisses();
  await testRegexValuesAndQuotesInChains();
  await testXPathGuardCoversShadowRootsAndFrames();
  await testScriptTextNeverMatchesAndShadowTextDoes();
  console.log('Playwright selector checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
