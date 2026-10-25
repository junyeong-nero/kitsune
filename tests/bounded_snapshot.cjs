// Run with: node tests/bounded_snapshot.cjs (no browser or npm dependencies).
// #262: a bounded snapshot ends between blocks, so a product card is never split.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element {
  // nodes: strings (text nodes) and Elements, in document order.
  constructor(tagName, attrs = {}, nodes = [], props = {}) {
    Object.assign(this, { tagName, attrs, nodeType: 1, isConnected: true }, props);
    this.childNodes = nodes.map(node => typeof node === 'string' ? { nodeType: 3, textContent: node } : node);
    this.children = this.childNodes.filter(node => node.nodeType === 1);
    this.children.forEach(child => { child.parentElement = this; });
  }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  get innerText() { return this.textContent; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  matches() { return false; }
  closest() { return null; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);

// Heterogeneous cards (a discount on odd ones, a cart button on every third), so that
// consecutive cards differ and sibling collapsing leaves them all listed.
function card(i) {
  const name = new Element('A', {}, [`Desk ${i}`], { href: `https://shop.test/p/desk-${i}` });
  const parts = [h('H3', {}, name), h('P', {}, `Variant ${i}: oak, 120x60 cm`), h('P', {}, `Price ${i}: ₩${100 + i},000`)];
  if (i % 2) parts.push(h('P', {}, `Discount ${i}: -${i}%`));
  if (i % 3 === 0) parts.push(h('BUTTON', {}, `Add desk ${i} to cart`));
  return h('LI', {}, ...parts);
}

// Every listed card has its name, URL, variant, price and discount, in order, and no line of
// another card in between.
function assertWholeCards(snapshot, label) {
  const lines = snapshot.split('\n').filter(line => !line.includes('truncated'));
  let current = null;
  const seen = new Map();
  for (const line of lines) {
    const number = line.match(/(?:desk[- ]|variant |price |discount )(\d+)/i);
    if (!number) continue;
    const i = Number(number[1]);
    if (line.includes(`heading "Desk ${i}"`)) current = i;
    assert.equal(i, current, `${label}: a line of card ${i} under card ${current}\n${snapshot}`);
    seen.set(i, [...(seen.get(i) || []), line.trim()]);
  }
  for (const [i, got] of seen) {
    const expected = [`heading "Desk ${i}"`, `link "Desk ${i}"`, `/url: "https://shop.test/p/desk-${i}"`,
      `text "Variant ${i}:`, `text "Price ${i}:`, ...(i % 2 ? [`text "Discount ${i}:`] : []),
      ...(i % 3 === 0 ? [`button "Add desk ${i} to cart"`] : [])];
    assert.equal(got.length, expected.length, `${label}: card ${i} is incomplete\n${got.join('\n')}`);
    expected.forEach((part, j) => assert.ok(got[j].includes(part), `${label}: card ${i} lacks ${part}`));
  }
  return [...seen.keys()];
}

(async () => {
  let listener;
  const body = h('BODY', {});
  const context = vm.createContext({
    console, URL, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    location: { href: 'https://shop.test/search?q=desk' },
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
    document: { body, title: 'Desks', getElementById: () => null, querySelector: () => null,
      querySelectorAll: () => [] },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve));
  const setBody = (...children) => {
    body.childNodes = body.children = children;
    children.forEach(child => { child.parentElement = body; });
  };

  // The final public budget, as the bridge applies it after aliases and URL redaction.
  const background = fs.readFileSync('extension/background/background.js', 'utf8');
  const bridge = vm.createContext({});
  vm.runInContext(background.slice(background.indexOf('function snapshotLineRef'),
    background.indexOf('function compactContentRefs')), bridge);

  // Product search results: at every budget the snapshot ends between cards.
  setBody(h('MAIN', {}, h('H1', {}, 'Search results for desk'), h('UL', {}, ...Array.from({ length: 60 }, (_, i) => card(i)))));
  const whole = await send({ action: 'getSnapshot' });
  assert.equal(whole.truncated, undefined);
  assert.equal(whole.cuts, undefined, 'unbounded snapshots have no cut points');
  assert.equal(assertWholeCards(whole.snapshot, 'unbounded').length, 60, 'heterogeneous cards are not collapsed');
  for (let maxChars = 2000; maxChars <= 9000; maxChars += 37) {
    const bounded = await send({ action: 'getSnapshot', maxChars });
    const label = `maxChars ${maxChars}`;
    assert.equal(bounded.truncated, true, label);
    assert.ok(bounded.snapshot.length <= maxChars, label);
    assert.equal(bounded.snapshot.split('\n').at(-1), '- … truncated; scope with selector, ref or max_depth');
    const cards = assertWholeCards(bounded.snapshot, label);
    assert.ok(cards.length > 0 && cards.every((n, j) => n === j), `${label}: a document-order prefix of cards`);
    // A card is about 300 characters: the cut leaves at most one card's worth unused.
    assert.ok(bounded.snapshot.length > maxChars - 400, `${label}: ${bounded.snapshot.length}`);
    for (const line of bounded.snapshot.split('\n').filter(line => line.includes('[ref='))) {
      assert.match(line, /\[ref=s\d+e\d+\]( \[level=\d\])?$/, `${label}: malformed ref row`);
    }

    // When aliases and URL redaction lengthen it, the bridge cuts again at the same boundaries.
    const tighter = Math.floor(maxChars * 0.6);
    const recut = bridge.boundPublicSnapshot(bounded, { maxChars: tighter });
    assert.ok(recut.snapshot.length <= tighter, `${label}: recut to ${tighter}`);
    assert.equal(recut.cuts, undefined, 'cut points stay inside the bridge');
    assert.equal(recut.truncated, true);
    assertWholeCards(recut.snapshot, `${label} recut to ${tighter}`);
  }
  // The bridge drops the cut points of a snapshot that fits too.
  const fits = await send({ action: 'getSnapshot', maxChars: 100000 });
  assert.ok(Array.isArray(fits.cuts));
  assert.equal(bridge.boundPublicSnapshot(fits, { maxChars: 100000 }).cuts, undefined);

  // A long article: a short section is kept or left out whole; a long one ends between
  // paragraphs instead of leaving most of the budget unused.
  const paragraph = (s, p) => h('P', {}, `Section ${s} paragraph ${p}. `.padEnd(240, 'x'));
  const section = (s, count) => h('SECTION', {}, h('H2', {}, `Section ${s}`),
    ...Array.from({ length: count }, (_, p) => paragraph(s, p)));
  setBody(h('ARTICLE', {}, h('H1', {}, 'Long article'), ...Array.from({ length: 20 }, (_, s) => section(s, 4))));
  for (let maxChars = 5000; maxChars <= 12000; maxChars += 211) {
    const bounded = await send({ action: 'getSnapshot', maxChars });
    const shown = bounded.snapshot.split('\n').filter(line => line.includes('heading "Section'));
    const last = shown.length - 1;
    for (let p = 0; p < 4; p++) {
      assert.ok(bounded.snapshot.includes(`Section ${last} paragraph ${p}.`), `maxChars ${maxChars}: section ${last} is cut`);
    }
    assert.doesNotMatch(bounded.snapshot, new RegExp(`Section ${last + 1}\\b`));
  }
  setBody(h('ARTICLE', {}, h('H1', {}, 'Long article'), section(0, 200)));
  const essay = await send({ action: 'getSnapshot', maxChars: 12000 });
  assert.equal(essay.truncated, true);
  assert.ok(essay.snapshot.length > 12000 - 300, `a long section is cut between paragraphs: ${essay.snapshot.length}`);

  console.log('Bounded snapshot checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
