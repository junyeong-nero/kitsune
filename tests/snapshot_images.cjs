// Run with: node tests/snapshot_images.cjs (no browser or npm dependencies).
// Issue #207: named images are snapshot lines with refs; decorative images and src are not.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element {
  // nodes: strings (text nodes) and Elements, in document order.
  constructor(tagName, attrs = {}, nodes = []) {
    Object.assign(this, { tagName, attrs, nodeType: 1, isConnected: true, id: attrs.id || '',
      title: attrs.title || '' });
    this.childNodes = nodes.map(node => typeof node === 'string' ? { nodeType: 3, textContent: node } : node);
    this.children = this.childNodes.filter(node => node.nodeType === 1);
    this.children.forEach(child => { child.parentElement = this; });
  }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  get innerText() { return this.textContent; }
  get descendants() { return this.children.flatMap(child => [child, ...child.descendants]); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  matches() { return false; }
  closest() { return null; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);
const img = attrs => h('IMG', { src: '/puzzle.png', ...attrs });

(async () => {
  const dataUrl = 'data:image/png;base64,' + 'A'.repeat(5000);
  const longAlt = 'Chart '.repeat(60);
  const cards = Array.from({ length: 8 }, (_, i) =>
    h('LI', {}, img({ alt: `Product ${i}` }), h('A', { href: `/p/${i}` }, `Product ${i}`)));
  const main = h('MAIN', {},
    h('H2', {}, 'Image puzzle'),
    h('P', {}, 'Type the six characters shown in the image.'),
    img({ id: 'puzzle', alt: '왜곡된 문자 퍼즐', width: '420', height: '140' }),
    img({ alt: '' }),
    img({ alt: 'Spacer', role: 'presentation' }),
    img({ alt: 'Divider', role: 'none' }),
    img({ alt: 'Hidden', 'aria-hidden': 'true' }),
    img({}),
    img({ alt: 'Alt text', 'aria-label': 'Label wins' }),
    img({ title: 'Title only' }),
    img({ alt: '', title: 'Decorative title' }),
    img({ alt: 'Inline data', src: dataUrl }),
    img({ alt: longAlt }),
    h('A', { href: '/' }, img({ alt: 'Home' })),
    h('LABEL', {}, 'Characters ', h('INPUT', { 'aria-label': 'Characters' })),
    h('UL', {}, ...cards));
  const body = h('BODY', {}, main);

  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    location: { href: 'http://127.0.0.1/puzzle' },
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
    document: { body, title: 'Puzzle',
      getElementById: () => null,
      querySelector: selector => selector.startsWith('#')
        ? [body, ...body.descendants].find(el => el.id === selector.slice(1)) || null : null,
      querySelectorAll: () => [],
    },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve));

  for (const text of [false, true]) {
    const { snapshot } = await send({ action: 'getSnapshot', text });
    const lines = snapshot.split('\n');
    // A named image is an `img` line with a ref and no state; src is never listed.
    assert.match(snapshot, /^ {2}- img "왜곡된 문자 퍼즐" \[ref=s\d+e\d+\]$/m, snapshot);
    assert.doesNotMatch(snapshot, /puzzle\.png|data:|src=/, 'image sources are not listed');
    // Decorative (alt="", role=presentation/none), aria-hidden and unnamed images are omitted.
    assert.doesNotMatch(snapshot, /Spacer|Divider|Hidden|Decorative title|img ""/, snapshot);
    // aria-label wins over alt; title names an image without alt.
    assert.match(snapshot, /- img "Label wins" \[ref=s\d+e\d+\]$/m);
    assert.doesNotMatch(snapshot, /Alt text/);
    assert.match(snapshot, /- img "Title only" \[ref=s\d+e\d+\]$/m);
    assert.match(snapshot, /- img "Inline data" \[ref=s\d+e\d+\]$/m);
    // Long names are clipped like other names.
    const long = lines.find(line => line.includes('img "Chart'));
    assert.ok(long && JSON.parse(long.match(/img ("[^"]*")/)[1]).length === 160, long);
    // An image inside a link is listed under it.
    const link = lines.findIndex(line => /^ {2}- link "" \[ref=/.test(line));
    assert.ok(link >= 0 && /^ {4}- img "Home" \[ref=s\d+e\d+\]$/.test(lines[link + 1]), snapshot);
    // Repeated cards with images still collapse after five.
    assert.equal(lines.filter(line => /img "Product \d"/.test(line)).length, 5, snapshot);
    assert.match(snapshot, /- … 3 more similar items/);
    // The surrounding content is unchanged.
    assert.match(snapshot, /heading "Image puzzle" \[ref=s\d+e\d+\] \[level=2\]/);
    assert.match(snapshot, /textbox "Characters" \[ref=s\d+e\d+\]/);
    if (text) assert.ok(lines.includes('  - text "Type the six characters shown in the image."'), snapshot);
  }

  // The image's ref resolves to the element, for scoping or an element screenshot.
  const { snapshot } = await send({ action: 'getSnapshot' });
  const ref = snapshot.match(/img "왜곡된 문자 퍼즐" \[ref=(s\d+e\d+)\]/)[1];
  const scoped = await send({ action: 'getSnapshot', selector: ref });
  assert.match(scoped.snapshot, /^- img "왜곡된 문자 퍼즐" \[ref=s\d+e\d+\]$/, scoped.snapshot);

  // boxes (browser_snapshot): each line ends with its viewport box, after its states, and
  // repeated siblings still collapse.
  const boxed = (await send({ action: 'getSnapshot', boxes: true })).snapshot;
  assert.match(boxed, /heading "Image puzzle" \[ref=s\d+e\d+\] \[level=2\] \[box=0,0,100,30\]$/m, boxed);
  assert.ok(boxed.split('\n').filter(line => !line.includes('more similar items') && !line.includes('- text '))
    .every(line => line.endsWith('[box=0,0,100,30]')), boxed);
  assert.match(boxed, /- … 3 more similar items/);
  assert.doesNotMatch((await send({ action: 'getSnapshot' })).snapshot, /box=/);

  console.log('snapshot_images: ok');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
