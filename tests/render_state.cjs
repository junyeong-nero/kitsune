// Run with: node tests/render_state.cjs (no browser or npm dependencies).
// #631: renderState says whether a page navigate or a new tab just loaded looks still rendering.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element {
  constructor(tagName, attrs = {}) {
    Object.assign(this, { tagName, attrs, nodeType: 1, childNodes: [], isConnected: true });
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  closest() { return null; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}

async function main() {
  const body = new Element('BODY');
  const page = { text: '', busy: [], media: false };
  body.querySelector = () => page.media ? new Element('IMG') : null;
  Object.defineProperty(body, 'innerText', { get: () => page.text });
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
    document: { body, querySelector: () => null,
      querySelectorAll: selector => selector === '[aria-busy="true"]' ? page.busy : [] },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const state = () => new Promise(resolve =>
    listener({ action: 'renderState' }, {}, result => resolve(JSON.parse(JSON.stringify(result)))));

  for (const [text, placeholder] of [
    ['PipeCRM Deals Contacts\nLoading...', 'Loading...'],
    ['  Loading…  ', 'Loading…'],
    ['loading', 'loading'],
    ['Header\nPlease wait...', 'Please wait...'],
  ]) {
    page.text = text;
    assert.deepEqual(await state(), { rendering: true, placeholder }, text);
  }
  // A placeholder is a whole line, not a word in content.
  page.text = 'Radiant Solar\nLoading dock access: OPEN';
  assert.deepEqual(await state(), {});

  page.text = '';
  assert.deepEqual(await state(), { rendering: true }, 'no content yet');
  page.media = true;
  assert.deepEqual(await state(), {}, 'an image-only page has content');
  page.media = false;

  page.text = 'Flights';
  page.busy = [new Element('DIV', { 'aria-busy': 'true' })];
  assert.deepEqual(await state(), { rendering: true }, 'a visible aria-busy element');
  page.busy = [];
  assert.deepEqual(await state(), {});

  console.log('render_state: all tests passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
