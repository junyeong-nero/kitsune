// Run with: node tests/find_query_scoring.cjs (no browser or npm dependencies).
// Issue #152: browser_find scores query terms, so descriptive queries find their target
// and unmatched elements (such as a header nav) never pass the filter.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const MARKER = 'KITSUNE_FAKE';
const CANDIDATE_TAGS = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'LABEL', 'H1', 'H2', 'H3',
  'H4', 'H5', 'H6', 'P', 'SPAN', 'DIV', 'LI', 'TD', 'TH', 'IMG']);

class Element {
  // nodes: strings (text nodes) and Elements, in document order.
  constructor(tagName, attrs = {}, nodes = [], props = {}) {
    Object.assign(this, { tagName, attrs, nodeType: 1, isConnected: true, id: '' }, props);
    this.childNodes = nodes.map(node => typeof node === 'string' ? { nodeType: 3, textContent: node } : node);
    this.children = this.childNodes.filter(node => node.nodeType === 1);
    this.children.forEach(child => { child.parentElement = this; });
  }
  get firstElementChild() { return this.children[0] || null; }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  get descendants() { return this.children.flatMap(child => [child, ...child.descendants]); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  matches() { return false; }
  closest() { return null; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);
const field = (tagName, attrs, props) => new Element(tagName, attrs, [], props);

(async () => {
  const nav = h('NAV', {}, ...['Sign in', 'Store', 'Home', 'Discovery Queue', 'Wishlist', 'Points Shop',
    '홈', '신작', '인기', '할인'].map(text => h('A', { href: '#' }, text)));
  const reviewType = new Element('BUTTON', { 'aria-haspopup': 'listbox' }, ['Review Type'], { id: 'review-type' });
  const koreanReviewType = new Element('BUTTON', {}, ['리뷰 유형'], { id: 'filter-ko' });
  const search = field('INPUT', {}, { id: 'search', type: 'search', placeholder: 'Search the store', value: '' });
  const sort = new Element('SELECT', { 'aria-label': 'Sort by' }, [], { id: 'sort', value: 'helpful' });
  const password = field('INPUT', { 'aria-label': 'Password' },
    { id: 'password', type: 'password', value: `${MARKER}_PASSWORD_000` });
  const main = h('MAIN', {},
    h('H2', {}, 'Customer reviews'),
    h('DIV', {}, reviewType, h('BUTTON', {}, 'Playtime'), h('BUTTON', {}, 'Language'), koreanReviewType),
    h('P', {}, 'Showing 1,234 reviews that match the filters above'),
    search, sort, password);
  const body = h('BODY', {}, nav, main);

  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
    document: { body,
      getElementById: () => null,
      querySelector: () => null,
      // Only find's candidate list matches; natural-language queries match no CSS selector.
      querySelectorAll: selector => selector.startsWith('a, button, input')
        ? body.descendants.filter(el => CANDIDATE_TAGS.has(el.tagName) || el.hasAttribute('role')) : [],
    },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const find = query => new Promise(resolve => listener({ action: 'find', query }, {}, resolve));
  const navTexts = new Set(nav.children.map(link => link.textContent));
  const assertNoNav = (query, found) => {
    const leaked = found.results.filter(result => navTexts.has(result.text)).map(result => result.text);
    assert.equal(leaked.join(), '', `${query} returns unmatched header links`);
  };

  // Descriptive queries, with filler words, role words and a quoted label, find their target first.
  for (const [query, selector] of [
    ["'Review Type' dropdown button in Customer reviews filters", '#review-type'],
    ['Review Type dropdown', '#review-type'],
    ['리뷰 유형 드롭다운', '#filter-ko'],
    ['search bar', '#search'],
    ['sort dropdown', '#sort'],
  ]) {
    const found = await find(query);
    assert.equal(found.results[0]?.selector, selector, `${query}: ${JSON.stringify(found.results)}`);
    assertNoNav(query, found);
    assert.equal(found.hint, undefined, query);
  }

  // Text matches outrank a container whose text runs the labels together.
  const reviewsText = await find('text Showing N reviews that match the filters');
  assert.equal(reviewsText.results[0].tag, 'p', JSON.stringify(reviewsText.results));
  assertNoNav('reviews text', reviewsText);

  // Nothing matches: no interactive fallback, just a hint. A role word alone is no match.
  for (const query of ['xyzzy frobnicate', 'xyzzy button']) {
    const found = await find(query);
    assert.equal(found.results.length, 0, query);
    assert.match(found.hint, /No element matches/, query);
  }

  // A sensitive value is never matched: its words find the field only by its label, exactly
  // as a decoy with the same label does, and the value's own words find nothing.
  const secret = await find(`${MARKER}_PASSWORD_000`);
  const decoy = await find('KITSUNE_DECOY_PASSWORD_000');
  assert.ok(!JSON.stringify(secret).includes(MARKER), 'find leaks the password value');
  const summary = found => Array.from(found.results, ({ selector, text, score }) => [selector, text, score]);
  assert.deepEqual(summary(secret), summary(decoy));
  assert.deepEqual(summary(secret), [['#password', '', summary(secret)[0][2]]]);
  assert.equal((await find(MARKER)).results.length, 0);

  console.log('find query scoring checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
