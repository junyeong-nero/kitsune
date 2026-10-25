// Run with: node tests/snapshot_readiness.cjs (no browser or npm dependencies).
// Snapshots and page info report document readiness; an empty snapshot says so (#259).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element {
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
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  matches() { return false; }
  closest() { return null; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);
const setBody = (body, ...nodes) => {
  body.childNodes = body.children = nodes;
  nodes.forEach(node => { node.parentElement = body; });
};

(async () => {
  const body = h('BODY', {});
  const document = {
    body, title: 'Async page', readyState: 'loading',
    documentElement: { lang: 'en', scrollWidth: 800, scrollHeight: 600, appendChild() {} },
    createElement: () => ({ remove() {} }),
    forms: [], links: [], images: [],
    getElementById: () => null, querySelector: () => null, querySelectorAll: () => [],
  };
  let listener;
  const context = vm.createContext({
    console, URL, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    location: { href: 'https://example.test/async' },
    window: { innerWidth: 800, innerHeight: 600, scrollX: 0, scrollY: 0,
      __kitsuneDialogSnapshot: () => '{"dialogs":[]}',
      getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
    document,
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve));

  // A document still loading with nothing rendered.
  const loading = await send({ action: 'getSnapshot' });
  assert.equal(loading.snapshot, '');
  assert.equal(loading.readyState, 'loading');
  assert.equal(loading.empty, true);
  assert.match(loading.hint, /wait for text you expect with browser_wait_for/);
  assert.equal(loading.url, 'https://example.test/async');
  assert.equal((await send({ action: 'getPageInfo' })).readyState, 'loading');

  // Loaded, but the content is still hidden or not yet inserted: empty, not an error.
  document.readyState = 'complete';
  setBody(body, new Element('DIV', {}, ['Hidden result'], { hidden: true }));
  const loaded = await send({ action: 'getSnapshot' });
  assert.equal(loaded.readyState, 'complete');
  assert.equal(loaded.empty, true);
  assert.equal(loaded.error, undefined);
  assert.equal((await send({ action: 'getSnapshot', text: true })).empty, true);
  assert.equal((await send({ action: 'getPageInfo' })).readyState, 'complete');

  // Content rendered later: the marker and hint go away.
  setBody(body, h('MAIN', {}, h('H1', {}, 'Results'), h('P', {}, '3 hotels found')));
  const filled = await send({ action: 'getSnapshot' });
  assert.match(filled.snapshot, /3 hotels found/);
  assert.equal(filled.readyState, 'complete');
  assert.equal(filled.empty, undefined);
  assert.equal(filled.hint, undefined);

  // A budget that cuts everything is truncation, not emptiness.
  const cut = await send({ action: 'getSnapshot', maxChars: 0 });
  assert.equal(cut.truncated, true);
  assert.equal(cut.empty, undefined);

  // Compact and post-action snapshots keep their fields: no readiness, no empty marker.
  setBody(body);
  const compact = await send({ action: 'getSnapshot', text: false });
  assert.equal(compact.snapshot, '');
  assert.equal(compact.readyState, undefined);
  assert.equal(compact.empty, undefined);
  const postAction = await send({ action: 'getSnapshot', observation: 'browser_navigate', maxChars: 4000 });
  assert.equal(postAction.readyState, undefined);
  assert.equal(postAction.empty, undefined);
  assert.equal(postAction.hint, undefined);

  console.log('Snapshot readiness checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
