// Run with: node tests/snapshot_refs.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element {
  constructor(tagName, textContent = '', attrs = {}, children = []) {
    Object.assign(this, { tagName, textContent, attrs, children, nodeType: 1,
      childNodes: [{ nodeType: 3, textContent }], isConnected: true });
    children.forEach(child => { child.parentElement = this; });
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, value) { this.attrs[key] = value; }
  removeAttribute(key) { delete this.attrs[key]; }
  closest() { return null; }
  focus() { this.focused = true; }
  blur() { this.blurred = true; }
  dispatchEvent(event) { (this.events ??= []).push(event); return true; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}

async function testContent() {
  const heading = new Element('H1', 'Account');
  const button = new Element('BUTTON', 'Log in');
  const input = Object.assign(new Element('INPUT', '', { 'aria-labelledby': 'email-label' }), { value: 'a@b', type: 'email' });
  const checkbox = Object.assign(new Element('INPUT', '', { 'aria-label': 'Remember' }), { type: 'checkbox', checked: true, disabled: true, value: 'on' });
  const hidden = new Element('BUTTON', 'Secret', { 'aria-hidden': 'true' });
  const main = new Element('MAIN', '', {}, [heading, button, input, checkbox, hidden]);
  const body = new Element('BODY', '', {}, [new Element('DIV', 'Prose should not appear'), main]);
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
    KeyboardEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: { body,
      getElementById: id => id === 'email-label' ? { textContent: 'Email address' } : null,
      querySelector: selector => {
        if (selector === '[') throw new Error('Invalid CSS selector');
        return selector === '#login' ? button : null;
      },
      querySelectorAll: selector => selector === 'button' ? [button] : [button, input, checkbox],
    },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve));
  const first = await send({ action: 'getSnapshot' });
  assert.match(first.snapshot, /- main ""/);
  assert.match(first.snapshot, /heading "Account" .*level=1/);
  assert.match(first.snapshot, /textbox "Email address" .*value="a@b"/);
  assert.match(first.snapshot, /checkbox "Remember" .*checked=true disabled/);
  assert.doesNotMatch(first.snapshot, /Prose|Secret/);
  const ref = first.snapshot.match(/button "Log in" \[ref=(s\d+e\d+)\]/)[1];
  assert.equal((await send({ action: 'focusElement', selector: ref })).ok, true);
  assert.equal(button.focused, true);
  const waited = await send({ action: 'waitForElement', selector: ref });
  assert.equal(waited.ok, true);
  assert.equal(waited.tag, 'button');
  assert.match(waited.ref, /^s\d+e\d+$/);
  assert.equal((await send({ action: 'waitForElement', selector: '#login' })).ok, true);
  assert.equal((await send({ action: 'waitForElement', selector: '[' })).error, 'Invalid CSS selector');
  assert.equal((await send({ action: 'keypress', selector: ref, key: 'Enter', modifiers: { ctrl: true } })).ok, true);
  assert.deepEqual(button.events.map(event => event.type), ['keydown', 'keyup']);
  assert.equal(button.events[0].ctrlKey, true);
  assert.equal((await send({ action: 'blurElement', selector: ref })).ok, true);
  assert.equal(button.blurred, true);
  let submitted = false;
  button.closest = selector => selector === 'form' ? {
    querySelector: () => ({ click: () => { submitted = true; } }),
  } : null;
  assert.equal((await send({ action: 'submitForm', selector: ref })).ok, true);
  assert.equal(submitted, true);
  assert.equal((await send({ action: 'getMarkdown', selector: ref })).markdown, 'Log in');
  const marker = await send({ action: 'markScreenshotTarget', selector: ref });
  assert.equal(button.hasAttribute(marker.attribute), true);
  const found = (await send({ action: 'find', query: 'button' })).results[0].ref;
  assert.match(found, /^s\d+e\d+$/);
  assert.equal((await send({ action: 'focusElement', selector: found })).ok, true);
  button.isConnected = false;
  assert.equal((await send({ action: 'focusElement', selector: ref })).error, 'stale ref, call browser_snapshot again');
  assert.equal((await send({ action: 'waitForElement', selector: ref })).error, 'stale ref, call browser_snapshot again');
  button.isConnected = true;
  button.hidden = true;
  const pendingWait = send({ action: 'waitForElement', selector: ref, pollInterval: 1 });
  button.isConnected = false;
  assert.equal((await pendingWait).error, 'stale ref, call browser_snapshot again');
  button.isConnected = true;
  button.hidden = false;
  await send({ action: 'getAccessibilityTree', depth: 6 });
  for (const stale of [ref, found, 'r0']) {
    assert.equal((await send({ action: 'focusElement', selector: stale })).error, 'stale ref, call browser_snapshot again');
  }
  assert.equal((await send({ action: 'focusElement', selector: '#login' })).ok, true);
  body.children = Array.from({ length: 300 }, () => new Element('BUTTON', 'A long button name '.repeat(10)));
  const capped = await send({ action: 'getSnapshot', maxChars: 4000 });
  assert.ok(capped.snapshot.length <= 4000);
  assert.equal(capped.truncated, true);
}

async function testBackground() {
  const source = fs.readFileSync('extension/background/background.js', 'utf8');
  let calls = [], snapshotError = false, actionError = false;
  const context = vm.createContext({
    getActiveTabId: async () => 42,
    dispatchCommand: async params => { calls.push(params); return actionError ? { error: 'stale ref' } : { ok: true }; },
    forwardToContent: async (action, params) => {
      calls.push({ action, ...params });
      return snapshotError ? { error: 'restricted page' } : { snapshot: 'tree', truncated: true };
    },
  });
  vm.runInContext(source.slice(source.indexOf('const POST_SNAPSHOT_ACTIONS'), source.indexOf('async function dispatchCommand')), context);
  for (const action of ['click', 'type', 'fill', 'selectOption', 'checkBox', 'keypress', 'submitForm', 'navigate']) {
    calls = [];
    const result = await context.handleCommand({ action, ref: 's1e0', element: 'audit only' });
    assert.equal(result.snapshot, 'tree');
    assert.equal(result.snapshot_truncated, true);
    assert.equal(calls[0].selector, 's1e0');
    assert.equal(calls[0].tabId, 42);
    assert.equal(calls[1].action, 'getSnapshot');
    assert.equal(calls[1].maxChars, 4000);
    assert.equal(calls[1].tabId, 42);
  }
  for (const action of ['scroll', 'waitForElement', 'blurElement', 'getHTML', 'getBounds',
                         'getComputedStyle', 'clickAndWaitNavigation', 'getMarkdown']) {
    calls = [];
    await context.handleCommand({ action, selector: '#fallback', ref: 's1e0', element: 'audit only' });
    assert.equal(calls[0].selector, 's1e0');
    assert.equal(calls[0].element, 'audit only');
    assert.equal(calls.length, 1);
  }
  calls = [];
  await context.handleCommand({ action: 'keypress', element: '#not-a-selector', includeSnapshot: false });
  assert.equal(calls[0].selector, undefined, 'audit descriptions never become selectors');
  calls = [];
  assert.equal((await context.handleCommand({ action: 'click', includeSnapshot: false })).snapshot, undefined);
  assert.equal(calls.length, 1);
  snapshotError = true;
  const failed = await context.handleCommand({ action: 'click', tabId: 7 });
  assert.equal(failed.ok, true);
  assert.equal(failed.snapshot_error, 'restricted page');
  assert.equal(calls.at(-1).tabId, 7);
  actionError = true;
  calls = [];
  assert.equal((await context.handleCommand({ action: 'click' })).error, 'stale ref');
  assert.equal(calls.length, 1);
  calls = [];
  await context.handleCommand({ action: 'drag', ref: 's1e0', toRef: 's1e1' });
  assert.equal(calls[0].from, 's1e0');
  assert.equal(calls[0].to, 's1e1');
}

(async () => {
  await testContent();
  await testBackground();
  console.log('Snapshot/ref and post-action snapshot checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
