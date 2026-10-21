// Run with: node tests/readable_snapshot.cjs (no browser or npm dependencies).
// browser_snapshot(text=True): readable text lines next to refs, bounded, redacted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const MARKER = 'KITSUNE_FAKE';

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
  get descendants() { return this.children.flatMap(child => [child, ...child.descendants]); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  matches() { return false; }
  closest() { return null; }
  focus() { this.focused = true; }
  dispatchEvent() { return true; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);
const field = (tagName, attrs, props) => new Element(tagName, attrs, [], props);

(async () => {
  const email = field('INPUT', { 'aria-label': 'Email' }, { type: 'email', value: 'user@example.test' });
  const password = field('INPUT', { 'aria-label': 'Password' }, { type: 'password', value: `${MARKER}_PASSWORD` });
  const card = field('INPUT', { 'aria-label': 'Card', autocomplete: 'cc-number' },
    { type: 'text', value: `${MARKER}_CARD` });
  // A sensitive custom host whose light DOM carries text.
  const secretHost = new Element('X-SECRET-FIELD', { type: 'password' }, [`${MARKER}_HOST`]);
  const remember = field('INPUT', { 'aria-label': 'Remember me' }, { type: 'checkbox', checked: false, value: 'on' });
  const booking = h('FORM', { 'aria-label': 'Booking' },
    h('LABEL', {}, 'Email ', email),
    password,
    h('P', {}, 'Card ending ', card, ' is on file.'),
    secretHost,
    remember,
    h('BUTTON', {}, 'Book'));
  booking.id = 'booking';
  const terms = h('P', {}, 'See ', h('A', { href: '/terms' }, 'terms'), ' for details.');
  terms.id = 'terms';
  const main = h('MAIN', {},
    h('H1', {}, 'Booking ', h('SPAN', {}, 'details')),
    h('P', {}, 'Cancel free until ', h('B', {}, 'Oct 30'), '.'),
    booking,
    terms);
  const nav = h('NAV', {}, h('UL', {}, h('LI', {}, h('A', {}, 'Home')), h('LI', {}, h('A', {}, 'Help'))));
  const hidden = new Element('DIV', {}, ['Hidden fact'], { hidden: true });
  const ariaHidden = h('DIV', { 'aria-hidden': 'true' }, 'Aria hidden fact');
  const body = h('BODY', {}, nav, main, hidden, ariaHidden);

  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    KeyboardEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    location: { href: 'https://example.test/booking' },
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
    document: { body, title: 'Booking - Example',
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
  const assertNoLeak = (label, result) => {
    assert.ok(!JSON.stringify(result).includes(MARKER), `${label} leaks a sensitive value`);
  };

  // The default snapshot is unchanged: no prose, url or title.
  const compact = await send({ action: 'getSnapshot' });
  assert.doesNotMatch(compact.snapshot, /Cancel free|- text /);
  assert.equal(compact.url, undefined);
  assert.equal(compact.title, undefined);

  // Paragraph facts appear next to editable controls, which keep their refs.
  const oldRef = compact.snapshot.match(/button "Book" \[ref=(s\d+e\d+)\]/)[1];
  const read = await send({ action: 'getSnapshot', text: true });
  assertNoLeak('text snapshot', read);
  assert.equal(read.url, 'https://example.test/booking');
  assert.equal(read.title, 'Booking - Example');
  assert.equal(read.truncated, undefined);
  const lines = read.snapshot.split('\n');
  assert.ok(lines.includes('  - text "Cancel free until Oct 30."'), read.snapshot);
  assert.ok(lines.includes('    - text "Card ending is on file."'), 'fields never join text lines');
  assert.ok(lines.includes('  - text "See terms for details."'), 'inline links stay in the sentence');
  assert.match(read.snapshot, /heading "Booking details" \[ref=s\d+e\d+\] \[level=1\]/);
  assert.match(read.snapshot, /textbox "Email" \[ref=s\d+e\d+\] \[value="user@example.test"\]/);
  assert.match(read.snapshot, /textbox "Password" \[ref=s\d+e\d+\]$/m);
  assert.match(read.snapshot, /textbox "Card" \[ref=s\d+e\d+\]$/m);
  assert.ok(read.snapshot.indexOf('Cancel free') < read.snapshot.indexOf('textbox "Email"'), 'document order');
  for (const name of ['Home', 'Help', 'details', 'Book', 'Email']) {
    assert.ok(!lines.includes(`- text "${name}"`) && !read.snapshot.includes(`text "${name}"`),
      `control and heading names are not repeated as text: ${name}`);
  }
  assert.doesNotMatch(read.snapshot, /Hidden fact|Aria hidden/);
  assert.ok(lines.filter(line => line.includes('- text ')).every(line => !line.includes('[ref=')), 'text lines have no refs');
  // Text snapshots start a new ref generation like any snapshot.
  assert.equal((await send({ action: 'focusElement', selector: oldRef })).error, 'stale ref, call browser_snapshot again');
  const termsRef = read.snapshot.match(/link "terms" \[ref=(s\d+e\d+)\]/)[1];
  assert.equal((await send({ action: 'focusElement', selector: termsRef })).ok, true);

  // Scoped and depth-limited reads.
  const scoped = await send({ action: 'getSnapshot', selector: '#booking', text: true });
  assert.match(scoped.snapshot, /Card ending is on file\./);
  assert.doesNotMatch(scoped.snapshot, /Cancel free|See terms/);
  const scopedLink = await send({ action: 'getSnapshot', selector: '#terms', text: true });
  assert.equal(scopedLink.snapshot.split('\n')[0], '- text "See terms for details."');
  assert.doesNotMatch((await send({ action: 'getSnapshot', maxDepth: 1, text: true })).snapshot, /Cancel free/);
  assert.match((await send({ action: 'getSnapshot', maxDepth: 2, text: true })).snapshot, /Cancel free until Oct 30\./);

  // Post-action snapshots include text only when the bridge asks for it.
  const background = fs.readFileSync('extension/background/background.js', 'utf8');
  const calls = [];
  const bridge = vm.createContext({
    getActiveTabId: async () => 42,
    tabsCreated: 0, openedTabs: [], actionStarts: new Map(),
    userActiveError: async () => null, guardedTabId: async () => null, touchStart() {}, touchEnd() {},
    dispatchCommand: send,
    forwardToContent: (action, params) => { calls.push({ action, ...params }); return send({ action, ...params }); },
  });
  vm.runInContext(background.slice(background.indexOf('const POST_SNAPSHOT_ACTIONS'), background.indexOf('async function dispatchCommand')), bridge);
  context.document.querySelector = selector => selector === '#remember' ? remember : null;
  const plainAction = await bridge.handleCommand({ action: 'checkBox', selector: '#remember', checked: true });
  assert.equal(calls.at(-1).text, undefined);
  assert.doesNotMatch(plainAction.snapshot, /Cancel free/);
  const textAction = await bridge.handleCommand({ action: 'checkBox', selector: '#remember', checked: false, snapshotText: true });
  assertNoLeak('post-action text snapshot', textAction);
  assert.equal(calls.at(-1).text, true);
  assert.equal(calls.at(-1).maxChars, 4000);
  assert.match(textAction.snapshot, /- text "Cancel free until Oct 30\."/);
  assert.ok(textAction.snapshot.includes(`checkbox "Remember me" [ref=${textAction.target_ref}] [value="on" checked=false]`));
  assert.equal(textAction.url, undefined, 'post-action results keep their own fields');
  // Explicit text snapshots report the tab they read.
  assert.equal((await bridge.handleCommand({ action: 'getSnapshot', text: true })).tabId, 42);
  assert.equal((await bridge.handleCommand({ action: 'getSnapshot', text: true, tabId: 7 })).tabId, 7);
  assert.equal((await bridge.handleCommand({ action: 'getSnapshot' })).tabId, undefined);

  // Long pages are capped with an explicit marker; long lines are clipped.
  body.children.forEach(child => { child.isConnected = false; });
  const long = Array.from({ length: 400 }, (_, i) => h('P', {}, `Paragraph ${i} `.padEnd(100, 'x')));
  body.childNodes = body.children = long;
  long.forEach(child => { child.parentElement = body; });
  const capped = await send({ action: 'getSnapshot', text: true });
  assert.equal(capped.truncated, true);
  assert.ok(capped.snapshot.length <= 12000, `length ${capped.snapshot.length}`);
  assert.equal(capped.snapshot.split('\n').at(-1), '- … truncated; scope with selector, ref or max_depth');
  assert.match(capped.snapshot, /Paragraph 0 /);
  assert.doesNotMatch(capped.snapshot, /Paragraph 399 /);
  const small = await send({ action: 'getSnapshot', text: true, maxChars: 500 });
  assert.ok(small.snapshot.length <= 500);
  assert.equal(small.snapshot.split('\n').at(-1), '- … truncated; scope with selector, ref or max_depth');
  const essay = h('P', {}, 'y'.repeat(3000));
  body.childNodes = body.children = [essay];
  essay.parentElement = body;
  assert.equal((await send({ action: 'getSnapshot', text: true })).snapshot, `- text "${'y'.repeat(1000)}…"`);

  // Repeated cards still collapse; prose siblings with inline links and text lists do not.
  const cards = Array.from({ length: 10 }, (_, i) => h('ARTICLE', {}, h('H2', {}, `Product ${i}`), h('BUTTON', {}, `Buy ${i}`)));
  const prose = Array.from({ length: 8 }, (_, i) => h('P', {}, `Fact ${i} with `, h('A', {}, `link ${i}`)));
  const items = Array.from({ length: 8 }, (_, i) => h('LI', {}, `Item ${i}`));
  body.childNodes = body.children = [h('SECTION', {}, ...cards), h('DIV', {}, ...prose), h('UL', {}, ...items)];
  body.children.forEach(child => { child.parentElement = body; });
  const repetitive = await send({ action: 'getSnapshot', text: true });
  assert.match(repetitive.snapshot, /- … 5 more similar items/);
  assert.doesNotMatch(repetitive.snapshot, /Product [5-9]/);
  assert.match(repetitive.snapshot, /- text "Fact 7 with link 7"/);
  assert.match(repetitive.snapshot, /link "link 7" \[ref=/);
  assert.match(repetitive.snapshot, /- text "Item 7"/);
  assert.equal(repetitive.truncated, undefined);

  console.log('Readable snapshot checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
