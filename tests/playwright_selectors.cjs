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
function compoundMatches(el, compound, selector) {
  const m = COMPOUND.exec(compound);
  if (!m) throw syntaxError(selector);
  const [, tag, rest, disabled] = m;
  if (tag && tag !== '*' && el.tagName !== tag.toUpperCase()) return false;
  if (disabled && !('disabled' in el.attrs)) return false;
  for (const [, kind, name, value] of rest.matchAll(/([.#[])([\w-]+)(?:="?([^"\]]*)"?)?\]?/g)) {
    if (kind === '.' && !String(el.attrs.class || '').split(' ').includes(name)) return false;
    if (kind === '#' && el.attrs.id !== name) return false;
    if (kind === '[' && (value === undefined ? !(name in el.attrs) : el.attrs[name] !== value)) return false;
  }
  return true;
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
  return selector.split(',').some(alternative => {
    const tokens = alternative.trim().split(/\s*(>)\s*|\s+/).filter(Boolean);
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
  get protocol() { return new URL(this.attrs.href).protocol; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
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
  return { html, body, list, options, nativeOption, boxes, pages, saves, policy, mic, secret };
}

function load(dom) {
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent: Event, KeyboardEvent: Event, InputEvent: Event,
    CSS: { escape: x => x }, setTimeout, HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getComputedStyle: el => ({ display: 'block', visibility: 'visible',
      webkitTextSecurity: 'data-mask' in el.attrs ? 'disc' : 'none', getPropertyValue: () => '' }) },
    document: {
      body: dom.body, documentElement: Object.assign(dom.html, { appendChild() {} }), activeElement: dom.body,
      createElement: () => ({ remove() {} }),
      getElementById: id => dom.html.querySelector('#' + id),
      querySelector: selector => dom.html.matches(selector) ? dom.html : dom.html.querySelector(selector),
      querySelectorAll: selector => [dom.html, ...dom.html.descendants()].filter(el => el.matches(selector)),
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

const clicks = elements => elements.map(el => el.events.filter(type => type === 'click').length);

async function clickOnly(selector, target) {
  const dom = page();
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
  for (const selector of ['text=hunter2', 'span:has-text("hunter")', 'role=button[name="hunter2"]']) {
    const result = await send({ action: 'hover', selector });
    assert.equal(result.code, 'not_found', `${selector}: ${JSON.stringify(result)}`);
  }
  // Script text is not page text.
  assert.equal((await send({ action: 'click', selector: 'text=베트남', first: true })).ok, true);
  assert.deepEqual(clicks([dom.html.querySelector('script')]), [0]);
}

async function testUnsupportedSyntaxIsNotRetryable() {
  const selectors = {
    'xpath=//button': /XPath/,
    '//button[text()="2"]': /XPath/,
    'text=/베트/': /regular-expression/,
    'e412 >> xpath=..': /ref/,
    's1e2 >> button': /ref/,
    // The background expands a short ref given alone; one used inside CSS reaches the page.
    'e197': /not an element name/,
    'button:near(#p1)': /:near\(\)/,
    'li:has(:has-text("베트남"))': /inside :has\(\)/,
    'role=button[pressed]': /\[pressed\]/,
    'role=button[name=/2/]': /role= attribute/,
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
    assert.equal(dom.html.descendants().some(el => el.events.length), false, `${selector} dispatched nothing`);
  }

  // An actionability wait fails at once instead of polling until its timeout.
  const send = load(page());
  const started = Date.now();
  const waited = await send({ action: 'click', selector: 'xpath=//button', wait: true, timeout: 5000 });
  assert.equal(waited.code, 'unsupported_selector');
  assert.ok(Date.now() - started < 1000);
  // Read actions report it too.
  assert.equal((await send({ action: 'getBounds', selector: 'text=/x/' })).code, 'unsupported_selector');
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
  console.log('Playwright selector checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
