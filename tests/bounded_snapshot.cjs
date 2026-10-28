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
  scrollIntoView() {}
  addEventListener() {}
  removeEventListener() {}
  dispatchEvent() { return true; }
  click() {}
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
// another card in between. A cut snapshot without a target lists no URLs (#321).
function assertWholeCards(snapshot, label, cut = false) {
  const lines = snapshot.split('\n').filter(line => !line.includes('truncated'));
  let current = null;
  const seen = new Map();
  for (const line of lines) {
    const number = line.match(/(?:desk[- ]|variant |price |discount )(\d+)/i);
    // A shortened URL belongs to the card it follows.
    if (!number && line.trim() !== '- /url: "…"') continue;
    const i = number ? Number(number[1]) : current;
    if (line.includes(`heading "Desk ${i}"`)) current = i;
    assert.equal(i, current, `${label}: a line of card ${i} under card ${current}\n${snapshot}`);
    seen.set(i, [...(seen.get(i) || []), line.trim()]);
  }
  const last = [...seen.keys()].at(-1);
  for (const [i, got] of seen) {
    // A cut snapshot without a target lists no link URLs at all (#321).
    const url = cut ? [] : [`/url: "/p/desk-${i}"`];
    const expected = [`heading "Desk ${i}"`, `link "Desk ${i}"`, ...url,
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
    MouseEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    navigator: { platform: 'Linux' },
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
    const cards = assertWholeCards(bounded.snapshot, label, true);
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
    assertWholeCards(recut.snapshot, `${label} recut to ${tighter}`, true);
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

  // #310: the gallery's crowded layout, a banner with a 68-link navigation (every link to one
  // long URL) before main. Same-origin URLs are paths and a repeated one is left out; a cut
  // snapshot lists main before the banner and a long footer, which wait as markers.
  context.location.href = 'https://shop.test/t/component-gallery/crowded/ui-sortable';
  const menuUrl = 'https://shop.test/t/component-gallery/crowded/index?layout=crowded&variant=0';
  const link = (name, href) => new Element('A', {}, [name], { href });
  const crowded = (cards, footerLinks = 3) => setBody(
    h('HEADER', {}, link('Component gallery', context.location.href),
      h('NAV', { 'aria-label': '사이트 메뉴' },
        h('UL', {}, ...Array.from({ length: 68 }, (_, i) => h('LI', {}, link(`메뉴 항목 ${i}`, menuUrl)))))),
    h('MAIN', {}, h('H1', {}, 'Sortable list'), h('UL', {}, ...Array.from({ length: cards }, (_, i) => card(i)))),
    h('FOOTER', {}, ...Array.from({ length: footerLinks }, (_, i) => link(`Help ${i}`, `https://help.example/gallery/${i}`))));
  // The characters of main's lines: its line and every deeper line after it.
  const mainChars = snapshot => {
    const lines = snapshot.split('\n');
    const start = lines.findIndex(line => line.startsWith('- main '));
    if (start < 0) return 0;
    let end = start + 1;
    while (end < lines.length && lines[end].startsWith('  ')) end++;
    return lines.slice(start, end).join('\n').length;
  };
  crowded(60, 40);
  const page = (await send({ action: 'getSnapshot' })).snapshot;
  const pageLines = page.split('\n');
  assert.match(pageLines[1], /^  - link "Component gallery" \[ref=s\d+e\d+\]$/);
  assert.match(pageLines[2], /^  - navigation "사이트 메뉴" /, 'a link to the page itself has no /url line');
  const navUrls = pageLines.filter(line => line.includes('/url: "/t/'));
  assert.deepEqual(navUrls, ['      - /url: "/t/component-gallery/crowded/index?layout=crowded&variant=0"'],
    'only the first of the links to one URL has its /url line');
  assert.equal(pageLines.filter(line => line.startsWith('    - link "메뉴 항목')).length, 68);
  assert.match(page, /- link "Help 0" \[ref=s\d+e\d+\]\n    - \/url: "https:\/\/help\.example\/gallery\/0"/,
    'a cross-origin URL stays absolute');
  assert.equal(assertWholeCards(page, 'crowded unbounded').length, 60);

  const bounded = await send({ action: 'getSnapshot', maxChars: 12000 });
  const core = bridge.boundPublicSnapshot(bounded, { maxChars: 12000 });
  const coreLines = core.snapshot.split('\n');
  assert.equal(core.truncated, true);
  assert.ok(core.snapshot.length <= 12000);
  const bannerRef = coreLines[0].match(/^- banner "" \[ref=(s\d+e\d+)\]$/)[1];
  assert.equal(coreLines[1], `  - … 70 lines (69 links) not shown; snapshot ${bannerRef} to list all`);
  assert.match(coreLines[2], /^- main "" \[ref=/, 'main follows the banner in document order');
  const footerRef = coreLines.at(-3).match(/^- contentinfo "" \[ref=(s\d+e\d+)\]$/)[1];
  assert.equal(coreLines.at(-2), `  - … 40 lines (40 links) not shown; snapshot ${footerRef} to list all`);
  assert.equal(coreLines.at(-1), '- … truncated; scope with selector, ref or max_depth');
  const kept = assertWholeCards(core.snapshot, 'crowded core', true);
  assert.ok(kept.length > 30 && kept.every((n, j) => n === j), `main is a prefix of its cards: ${kept.length}`);
  assert.ok(mainChars(core.snapshot) > 11000, `main keeps ${mainChars(core.snapshot)} characters`);
  // The bridge's recut keeps the markers with their landmarks and the cards whole.
  const recut = bridge.boundPublicSnapshot(bounded, { maxChars: 7000 });
  assert.ok(recut.snapshot.length <= 7000);
  assert.deepEqual(recut.snapshot.split('\n').slice(0, 3), coreLines.slice(0, 3));
  assertWholeCards(recut.snapshot, 'crowded recut', true);
  // The marker's ref lists the navigation.
  const banner = (await send({ action: 'getSnapshot', selector: bannerRef })).snapshot;
  assert.equal((banner.match(/- link "메뉴 항목/g) || []).length, 68);

  // With a short main the rest of the budget lists the banner as far as it fits, and its
  // marker counts what is still left out. A short footer is listed in place.
  crowded(3);
  const short = await send({ action: 'getSnapshot', maxChars: 2500 });
  const shortLines = short.snapshot.split('\n');
  assert.ok(short.snapshot.length <= 2500);
  assert.equal(assertWholeCards(short.snapshot, 'short main', true).length, 3);
  assert.match(short.snapshot, /- contentinfo "" \[ref=s\d+e\d+\]\n  - link "Help 0"/, 'the footer is listed whole');
  const listed = shortLines.filter(line => line.startsWith('    - link "메뉴 항목')).length;
  assert.ok(listed > 10 && listed < 68, `${listed} navigation links listed`);
  const marker = shortLines.findIndex(line => line.startsWith('  - … '));
  assert.equal(shortLines[marker], `  - … ${68 - listed} lines (${68 - listed} links) not shown; snapshot ${
    shortLines[0].match(/\[ref=(s\d+e\d+)\]/)[1]} to list all`);
  assert.match(shortLines[marker - 1], /^    - link "메뉴 항목/);
  assert.match(shortLines[marker + 1], /^- main /);
  // A short navigation keeps its place before main.
  setBody(h('NAV', {}, link('Home', 'https://shop.test/'), link('Cart', 'https://shop.test/cart')),
    h('MAIN', {}, h('UL', {}, ...Array.from({ length: 60 }, (_, i) => card(i)))));
  assert.deepEqual((await send({ action: 'getSnapshot', maxChars: 5000 })).snapshot.split('\n').slice(0, 4)
    .map(line => line.replace(/ \[ref=.*/, '')), ['- navigation ""', '  - link "Home"', '  - link "Cart"', '- main ""']);
  crowded(3);
  // A budget that fits everything collapses nothing.
  const roomy = await send({ action: 'getSnapshot', maxChars: 100000 });
  assert.equal(roomy.truncated, undefined);
  assert.doesNotMatch(roomy.snapshot, /not shown/);

  // A link whose /url line is left out is listed with the link it repeats: in a delta ...
  await send({ action: 'ping', deltaBaseline: true, snapshotText: true });
  const renamed = context.document.body.children[0].children[1].children[0].children[40].children[0];
  renamed.childNodes[0].textContent = '메뉴 항목 마흔';
  const delta = (await send({ action: 'getSnapshot', delta: true, text: true })).snapshot.split('\n');
  assert.deepEqual(delta.map(line => line.replace(/ \[ref=.*/, '')), ['- banner ""', '  - navigation "사이트 메뉴"',
    '    - link "메뉴 항목 0"', '      - /url: "/t/component-gallery/crowded/index?layout=crowded&variant=0"',
    '    - link "메뉴 항목 마흔"']);
  // ... and after an action on a link: a delta lists the target with its own /url line ...
  crowded(60, 40);
  const cardLink = context.document.body.children[1].children[1].children[5].children[0].children[0];
  const listing = (await send({ action: 'getSnapshot' })).snapshot;
  const cardRef = listing.match(/link "Desk 5" \[ref=(s\d+e\d+)\]/)[1];
  assert.equal(cardLink.textContent, 'Desk 5');
  assert.equal((await send({ action: 'click', selector: cardRef, deltaBaseline: true })).ok, true);
  const clicked = await send({ action: 'getSnapshot', delta: true, focusLastAction: true, observation: 'browser_click' });
  assert.deepEqual(clicked.snapshot.split('\n').map(line => line.replace(/ \[ref=.*/, '')),
    ['- main ""', '  - heading "Desk 5"', '    - link "Desk 5"', '      - /url: "/p/desk-5"']);
  // ... a targeted repeated link keeps its own, and a targeted cut defers no landmark.
  const menuRef = listing.match(/link "메뉴 항목 40" \[ref=(s\d+e\d+)\]/)[1];
  assert.equal((await send({ action: 'click', selector: menuRef })).ok, true);
  const aimed = await send({ action: 'getSnapshot', maxChars: 6000, focusLastAction: true });
  assert.equal(aimed.target_ref, menuRef);
  assert.match(aimed.snapshot, new RegExp(`link "메뉴 항목 40" \\[ref=${menuRef}\\]\n      - /url: "/t/`));
  assert.doesNotMatch(aimed.snapshot, /not shown/);
  // Without main nothing is deferred: the banner comes first, as before.
  setBody(...context.document.body.children.filter(child => child.tagName !== 'MAIN'),
    h('DIV', {}, h('UL', {}, ...Array.from({ length: 60 }, (_, i) => card(i)))));
  const noMain = await send({ action: 'getSnapshot', maxChars: 12000 });
  assert.equal(noMain.truncated, true);
  assert.doesNotMatch(noMain.snapshot, /not shown/);
  assert.equal(noMain.snapshot.split('\n').filter(line => line.startsWith('    - link "메뉴 항목')).length, 68);

  // In the bridge's recut a span that does not fit whole is kept with shortened URLs, and a
  // repeated link is left out when the link it repeats does not fit.
  const long = ['- navigation "Menu" [ref=e1]', `  - link "${'A'.repeat(60)}" [ref=e2]`, `    - /url: "/${'a'.repeat(60)}"`,
    '  - link "B" [ref=e3]', '- button "Next" [ref=e4]'].join('\n');
  const shortened = bridge.boundPublicSnapshot({ truncated: true, cuts: [0, 1, 4, 5], snapshot: long }, { maxChars: 230 });
  assert.equal(shortened.snapshot, ['- navigation "Menu" [ref=e1]', `  - link "${'A'.repeat(60)}" [ref=e2]`,
    '    - /url: "…"', '  - link "B" [ref=e3]', '- button "Next" [ref=e4]',
    '- … truncated; scope with selector, ref or max_depth'].join('\n'));
  const repeat = bridge.boundPublicSnapshot({ truncated: true, cuts: [0, 1, 3, 4, 5], snapshot: long }, { maxChars: 140 });
  assert.equal(repeat.snapshot, ['- navigation "Menu" [ref=e1]', '- button "Next" [ref=e4]',
    '- … truncated; scope with selector, ref or max_depth'].join('\n'));

  console.log('Bounded snapshot checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
