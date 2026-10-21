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
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    KeyboardEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible',
      getPropertyValue: property => `computed:${property}` }) },
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
  const customStyle = await send({ action: 'getComputedStyle', selector: ref, properties: ['color', '--accent'] });
  assert.deepEqual({ ...customStyle.computed }, { color: 'computed:color', '--accent': 'computed:--accent' });
  const defaultStyle = await send({ action: 'getComputedStyle', selector: ref });
  const emptyStyle = await send({ action: 'getComputedStyle', selector: ref, properties: [] });
  assert.equal(defaultStyle.computed.display, 'computed:display');
  assert.equal(defaultStyle.computed.overflow, 'computed:overflow');
  assert.deepEqual({ ...emptyStyle.computed }, { ...defaultStyle.computed });

  const originalChildren = button.childNodes;
  const listItems = [new Element('LI', 'First'), new Element('LI', 'Second')];
  for (const tagName of ['UL', 'OL']) {
    const list = new Element(tagName);
    list.querySelectorAll = () => listItems;
    button.childNodes = [list];
    const markdown = await send({ action: 'getMarkdown', selector: ref });
    const expected = tagName === 'UL' ? '- First\n- Second' : '1. First\n2. Second';
    assert.equal(markdown.markdown, expected);
  }
  button.childNodes = originalChildren;
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
  assert.doesNotMatch(capped.snapshot, /more similar items/, 'leaf controls are not collapsed');

  // Exercise the complete action -> capped snapshot path with a target past 4,000 chars.
  const background = fs.readFileSync('extension/background/background.js', 'utf8');
  const bridge = vm.createContext({
    getActiveTabId: async () => 42,
    tabsCreated: 0, openedTabs: [], actionStarts: new Map(),
    dispatchCommand: send,
    forwardToContent: (action, params) => send({ action, ...params }),
  });
  vm.runInContext(background.slice(background.indexOf('const POST_SNAPSHOT_ACTIONS'), background.indexOf('async function dispatchCommand')), bridge);
  const search = Object.assign(new Element('INPUT', '', { 'aria-label': 'Deep search' }), { value: '', type: 'search' });
  const before = new Element('BUTTON', 'Previous control');
  const after = new Element('BUTTON', 'Next control');
  const nearby = new Element('BUTTON', 'Nearby nested control');
  const form = new Element('FORM', '', { 'aria-label': 'Search form' }, [before, search, after, new Element('DIV', '', {}, [nearby])]);
  const deepMain = new Element('MAIN', '', {}, [form]);
  const leading = Array.from({ length: 100 }, (_, i) => new Element('BUTTON', `Leading ${i} ${'long name '.repeat(12)}`));
  const trailing = new Element('BUTTON', 'Trailing control');
  body.children = [...leading, deepMain, trailing];
  for (const child of body.children) child.parentElement = body;
  const full = await send({ action: 'getSnapshot' });
  const oldRef = full.snapshot.match(/searchbox "Deep search" \[ref=(s\d+e\d+)\]/)[1];
  assert.ok(full.snapshot.indexOf(`ref=${oldRef}`) > 4000);
  const typed = await bridge.handleCommand({ action: 'type', ref: oldRef, text: 'kitsune', clear: true });
  assert.equal(typed.ok, true);
  assert.equal(typed.snapshot_truncated, true);
  assert.ok(typed.snapshot.length <= 4000);
  assert.match(typed.target_ref, /^s\d+e\d+$/);
  assert.notEqual(typed.target_ref, oldRef);
  assert.ok(typed.snapshot.includes(`    - searchbox "Deep search" [ref=${typed.target_ref}] [value="kitsune"]`));
  assert.match(typed.snapshot, /- main ""/);
  assert.match(typed.snapshot, /\n  - form "Search form"/);
  for (const name of ['Previous control', 'Next control', 'Nearby nested control', 'Leading 0']) {
    assert.ok(typed.snapshot.includes(name), `includes context and document-order fill: ${name}`);
  }
  const fullOrder = full.snapshot.split('\n').map(line => line.replace(/s\d+e\d+/g, 'REF').replace(/value="[^"]*"/g, 'VALUE'));
  const selectedOrder = typed.snapshot.split('\n').map(line => fullOrder.indexOf(line.replace(/s\d+e\d+/g, 'REF').replace(/value="[^"]*"/g, 'VALUE')));
  assert.ok(selectedOrder.every((index, i) => index >= 0 && (!i || index > selectedOrder[i - 1])), 'keeps document order and indentation');
  assert.equal((await send({ action: 'focusElement', selector: oldRef })).error, 'stale ref, call browser_snapshot again');
  const entered = await bridge.handleCommand({ action: 'keypress', ref: typed.target_ref, key: 'Enter' });
  assert.equal(entered.ok, true, 'fresh ref works for the next action');
  assert.ok(entered.snapshot.includes(`[ref=${entered.target_ref}] [value="kitsune"]`));

  // Fill must remember the actual child input, rather than its non-semantic wrapper.
  context.document.querySelector = selector => selector === '#wrapper' ? form : null;
  form.querySelector = () => search;
  const filled = await bridge.handleCommand({ action: 'fill', selector: '#wrapper', value: 'updated' });
  assert.ok(filled.snapshot.includes(`[ref=${filled.target_ref}] [value="updated"]`));

  // A full snapshot does not inherit action focus, and consumes any remembered target.
  const ordinary = await send({ action: 'getSnapshot', maxChars: 4000 });
  assert.equal(ordinary.target_ref, undefined);
  assert.doesNotMatch(ordinary.snapshot, /Deep search/);
  assert.equal((await send({ action: 'getSnapshot', maxChars: 4000, focusLastAction: true })).target_ref, undefined);

  const deepCheck = Object.assign(new Element('INPUT', '', { 'aria-label': 'Deep checkbox' }), { type: 'checkbox', checked: false, value: 'on' });
  form.children.push(deepCheck);
  deepCheck.parentElement = form;
  context.document.querySelector = selector => {
    if (selector === '#checkbox') return deepCheck;
    return selector === '#search' ? search : null;
  };
  const checked = await bridge.handleCommand({ action: 'checkBox', selector: '#checkbox', checked: true });
  assert.ok(checked.snapshot.includes(`[ref=${checked.target_ref}] [value="on" checked=true]`));

  context.document.activeElement = search;
  const active = await bridge.handleCommand({ action: 'keypress', key: 'Enter' });
  assert.ok(active.snapshot.includes(`searchbox "Deep search" [ref=${active.target_ref}]`));

  // Clicks remember the effective interactive ancestor when a coordinate hits its child.
  const icon = new Element('SPAN', 'Icon');
  icon.parentElement = after;
  context.document.elementFromPoint = () => icon;
  context.MouseEvent = context.KeyboardEvent;
  after.click = () => {};
  const clicked = await bridge.handleCommand({ action: 'click', coords: { x: 5, y: 5 } });
  assert.ok(clicked.snapshot.includes(`button "Next control" [ref=${clicked.target_ref}]`));

  const select = Object.assign(new Element('SELECT', '', { 'aria-label': 'Deep select' }), {
    value: 'old', options: [{ value: 'new', text: 'New option' }],
  });
  form.children.push(select);
  select.parentElement = form;
  const querySelector = context.document.querySelector;
  context.document.querySelector = selector => selector === '#select' ? select : querySelector(selector);
  const selected = await bridge.handleCommand({ action: 'selectOption', selector: '#select', value: 'new' });
  assert.ok(selected.snapshot.includes(`combobox "Deep select" [ref=${selected.target_ref}] [value="new"]`));

  search.closest = selector => selector === 'form' ? form : null;
  form.querySelector = () => after;
  const formResult = await bridge.handleCommand({ action: 'submitForm', selector: '#search' });
  assert.ok(formResult.snapshot.includes(`searchbox "Deep search" [ref=${formResult.target_ref}]`));

  // No-target keypress must not reuse the previous action's target.
  context.document.activeElement = body;
  const noTarget = await bridge.handleCommand({ action: 'keypress', key: 'Escape' });
  assert.equal(noTarget.target_ref, undefined);
  assert.doesNotMatch(noTarget.snapshot, /Deep checkbox/);

  // Removed/hidden targets are not resurrected just to supply a target ref.
  await send({ action: 'type', selector: '#search', text: 'removed', clear: true });
  search.isConnected = false;
  const removed = await send({ action: 'getSnapshot', maxChars: 4000, focusLastAction: true });
  assert.equal(removed.target_ref, undefined);
  assert.doesNotMatch(removed.snapshot, /Deep search/);
  search.isConnected = true;
  await send({ action: 'type', selector: '#search', text: 'hidden', clear: true });
  search.hidden = true;
  const invisible = await send({ action: 'getSnapshot', maxChars: 4000, focusLastAction: true });
  assert.equal(invisible.target_ref, undefined);
  assert.doesNotMatch(invisible.snapshot, /Deep search/);
  search.hidden = false;

  // Uncapped and already-small snapshots also report a valid fresh target ref.
  await send({ action: 'type', selector: '#search', text: 'uncapped', clear: true });
  const uncapped = await send({ action: 'getSnapshot', focusLastAction: true });
  assert.equal(uncapped.truncated, undefined);
  assert.ok(uncapped.snapshot.includes(`[ref=${uncapped.target_ref}] [value="uncapped"]`));
  body.children = [deepMain];
  const small = await bridge.handleCommand({ action: 'type', selector: '#search', text: 'small', clear: true });
  assert.equal(small.snapshot_truncated, undefined);
  assert.ok(small.snapshot.includes(`[ref=${small.target_ref}] [value="small"]`));

  // Identical names came from nested delegated-click wrappers reading the same
  // subtree, not duplicate traversal. Preserve their real controls exactly once.
  const headerControls = [new Element('BUTTON', 'Search'), new Element('A', 'Cart')];
  const wrapperText = 'Whole header '.repeat(30);
  const innerWrapper = Object.assign(new Element('DIV', wrapperText, {}, headerControls), { onclick() {} });
  const outerWrapper = Object.assign(new Element('DIV', wrapperText, {}, [innerWrapper]), { onclick() {} });
  body.children = [outerWrapper];
  const wrappers = await send({ action: 'getSnapshot' });
  assert.doesNotMatch(wrappers.snapshot, /Whole header/);
  assert.equal(wrappers.snapshot.split('\n').length, 2);
  assert.equal((wrappers.snapshot.match(/button "Search"/g) || []).length, 1);
  assert.equal((wrappers.snapshot.match(/link "Cart"/g) || []).length, 1);
  const originalListener = listener;
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  assert.equal(listener, originalListener, 'reinjection does not register a second listener');

  const customChildren = [new Element('SPAN', 'One', { role: 'button' }), new Element('SPAN', 'Two', { tabindex: '0' })];
  body.children = [Object.assign(new Element('DIV', 'Custom wrapper', {}, customChildren), { onclick() {} })];
  assert.doesNotMatch((await send({ action: 'getSnapshot' })).snapshot, /Custom wrapper/);
  body.children = [Object.assign(new Element('DIV', 'Explicit wrapper', { role: 'button' }, headerControls), { onclick() {} })];
  assert.match((await send({ action: 'getSnapshot' })).snapshot, /button "Explicit wrapper"/);

  const descendant = new Element('SPAN', 'Hidden in the control name '.repeat(20));
  const directControl = new Element('DIV', 'Show details ' + descendant.textContent, { role: 'button' }, [descendant]);
  directControl.childNodes = [{ nodeType: 3, textContent: 'Show details ' }, descendant];
  const fallback = new Element('DIV', 'Small icon label', { role: 'button' }, [new Element('SPAN', 'Small icon label')]);
  fallback.childNodes = fallback.children;
  const large = new Element('DIV', descendant.textContent, { role: 'button' }, [descendant]);
  large.childNodes = large.children;
  const labelled = new Element('DIV', 'Direct name', { role: 'button', 'aria-label': 'Explicit label', title: 'Title' });
  const titled = new Element('DIV', 'Direct name', { role: 'button', title: 'Title label' });
  const alt = new Element('DIV', 'Direct name', { role: 'button', alt: 'Alt label' });
  body.children = [directControl, fallback, large, labelled, titled, alt];
  const names = (await send({ action: 'getSnapshot' })).snapshot;
  for (const name of ['Show details', 'Small icon label', '', 'Explicit label', 'Title label', 'Alt label']) {
    assert.ok(names.includes(`button ${JSON.stringify(name)} [ref=`), `non-native name: ${name}`);
  }
  assert.doesNotMatch(names, /Hidden in the control name|Direct name/);

  const cards = Array.from({ length: 10 }, (_, i) => new Element('ARTICLE', '', {}, [
    new Element('H2', `Product ${i}`), new Element('BUTTON', `Buy ${i}`),
  ]));
  const catalog = new Element('MAIN', '', {}, [new Element('H1', 'Catalog'), new Element('SECTION', '', {}, cards)]);
  const outside = new Element('BUTTON', 'Outside catalog');
  body.children = [catalog, outside];
  for (const child of body.children) child.parentElement = body;
  context.document.querySelector = selector => ({ '#catalog': catalog, '#card-8': cards[8], '#buy-8': cards[8].children[1] })[selector] || null;
  const collapsed = await send({ action: 'getSnapshot' });
  assert.match(collapsed.snapshot, /- … 5 more similar items/);
  for (let i = 0; i < 5; i++) assert.match(collapsed.snapshot, new RegExp(`Product ${i}`));
  assert.doesNotMatch(collapsed.snapshot, /Product [5-9]|Buy [5-9]/);
  const visibleRefs = [...collapsed.snapshot.matchAll(/ref=(s\d+e(\d+))/g)];
  assert.deepEqual(visibleRefs.map(match => Number(match[2])), visibleRefs.map((_, i) => i), 'collapsed items allocate no refs');
  const generation = visibleRefs[0][1].match(/^s\d+e/)[0];
  assert.equal((await send({ action: 'focusElement', selector: generation + visibleRefs.length })).error,
    'stale ref, call browser_snapshot again', 'no hidden item is registered in refMap');

  await send({ action: 'keypress', selector: '#buy-8', key: 'Enter' });
  const focusedCard = await send({ action: 'getSnapshot', focusLastAction: true, maxChars: 4000 });
  assert.ok(focusedCard.snapshot.includes(`button "Buy 8" [ref=${focusedCard.target_ref}]`));
  assert.match(focusedCard.snapshot, /Product 8/);
  assert.match(focusedCard.snapshot, /- … 3 more similar items/);
  assert.match(focusedCard.snapshot, /- … 1 more similar items/);
  assert.doesNotMatch(focusedCard.snapshot, /Product [5-79]|Buy [5-79]/);
  await send({ action: 'keypress', selector: '#buy-8', key: 'Enter' });
  const tinyTarget = await send({ action: 'getSnapshot', focusLastAction: true, maxChars: 100 });
  assert.ok(tinyTarget.snapshot.includes(`button "Buy 8" [ref=${tinyTarget.target_ref}]`));

  const scoped = await send({ action: 'getSnapshot', selector: '#card-8' });
  assert.match(scoped.snapshot, /Product 8/);
  assert.match(scoped.snapshot, /Buy 8/);
  assert.doesNotMatch(scoped.snapshot, /Catalog|Outside|more similar/);
  const catalogOnly = await send({ action: 'getSnapshot', selector: '#catalog', maxDepth: 0 });
  assert.match(catalogOnly.snapshot, /^- main "" \[ref=s\d+e0\]$/);
  const catalogRef = catalogOnly.snapshot.match(/ref=(s\d+e0)/)[1];
  const byRef = await send({ action: 'getSnapshot', ref: catalogRef, selector: '#card-8', maxDepth: 1 });
  assert.match(byRef.snapshot, /Catalog/);
  assert.doesNotMatch(byRef.snapshot, /Product|Buy|Outside/);
  assert.equal((await send({ action: 'getSnapshot', ref: catalogRef })).error, 'stale ref, call browser_snapshot again');
  const shallowCatalog = await send({ action: 'getSnapshot', selector: '#catalog', maxDepth: 2 });
  assert.match(shallowCatalog.snapshot, /Catalog/);
  assert.doesNotMatch(shallowCatalog.snapshot, /Product|Buy|more similar/, 'no summary for items below the depth limit');
  const cardDepth = await send({ action: 'getSnapshot', selector: '#card-8', maxDepth: 1 });
  assert.match(cardDepth.snapshot, /Product 8/);
  assert.match(cardDepth.snapshot, /Buy 8/);
  const bodyDepth = await send({ action: 'getSnapshot', maxDepth: 1 });
  assert.match(bodyDepth.snapshot, /Outside catalog/);
  assert.doesNotMatch(bodyDepth.snapshot, /Catalog|Product|Buy/);
  assert.equal((await send({ action: 'getSnapshot', selector: '#missing' })).error, 'Selector not found: #missing');
  assert.match((await send({ action: 'getSnapshot', selector: '[' })).error, /Selector not found/);
  for (const maxDepth of [-1, 1.5, true]) {
    assert.equal((await send({ action: 'getSnapshot', maxDepth })).error, 'max_depth must be a non-negative integer');
  }
  body.children = Array.from({ length: 12 }, () => new Element('DIV', '', {}, [new Element('SPAN', 'Decoration')]));
  assert.equal((await send({ action: 'getSnapshot' })).snapshot, '', 'non-semantic decorations do not generate collapse noise');
}

// stale_ref names the snapshot that replaced a ref and, only for the same node with the same
// role, name and snapshot ancestors, its current ref. Nothing is dispatched either way.
async function testStaleRefSuggestions() {
  const archive = new Element('BUTTON', 'Archive');
  const main = new Element('MAIN', '', {}, [new Element('H1', 'Inbox'), archive]);
  const nav = new Element('NAV', '', {}, [new Element('BUTTON', 'Archive')]);
  const body = new Element('BODY', '', {}, [main, nav]);
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: { body, getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve));
  const snapshot = async (msg = {}) => {
    const lines = (await send({ action: 'getSnapshot', ...msg })).snapshot.split('\n');
    // The first "Archive" button is the one in main; nav's has a different ancestor.
    return lines.find(line => line.includes('button "Archive"')).match(/ref=(s\d+e\d+)/)[1];
  };
  const stale = async ref => {
    const result = await send({ action: 'focusElement', selector: ref });
    assert.equal(result.code, 'stale_ref');
    assert.equal(result.state.reason, 'snapshot_replaced');
    assert.equal(archive.focused, undefined, 'a replaced ref never acts');
    return result;
  };

  const first = await snapshot();
  const second = await snapshot({ observation: 'browser_click' });
  const replaced = await stale(first);
  assert.equal(replaced.state.latestObservation, 'browser_click');
  assert.equal(replaced.state.currentRef, second);
  assert.match(replaced.hint, /state\.currentRef is the same element/);
  assert.equal((await send({ action: 'focusElement', selector: second })).ok, true);
  archive.focused = undefined;

  // Only the three generations before the current one are kept; an older ref only names
  // the latest observation.
  for (let i = 0; i < 3; i++) await snapshot();
  assert.ok((await stale(second)).state.currentRef, 'three snapshots later');
  await snapshot();
  const old = await stale(second);
  assert.equal(old.state.latestObservation, 'browser_snapshot');
  assert.equal(old.state.currentRef, undefined);
  assert.equal(old.hint, 'A newer snapshot from browser_snapshot replaced this ref; use its refs or a new browser_snapshot.');

  // A changed name, a changed ancestor, a replaced node or a removed node is not the same element.
  let ref = await snapshot();
  Object.assign(archive, { textContent: 'Unarchive', childNodes: [{ nodeType: 3, textContent: 'Unarchive' }] });
  await send({ action: 'getSnapshot' });
  assert.equal((await stale(ref)).state.currentRef, undefined, 'renamed');
  Object.assign(archive, { textContent: 'Archive', childNodes: [{ nodeType: 3, textContent: 'Archive' }] });
  ref = await snapshot();
  main.attrs['aria-label'] = 'Archived mail';
  await send({ action: 'getSnapshot' });
  assert.equal((await stale(ref)).state.currentRef, undefined, 'moved under another landmark');
  delete main.attrs['aria-label'];
  ref = await snapshot();
  const twin = new Element('BUTTON', 'Archive');
  twin.parentElement = main;
  main.children[1] = twin;
  await send({ action: 'getSnapshot' });
  assert.equal((await stale(ref)).state.currentRef, undefined, 'a re-rendered node is not the same element');
  main.children[1] = archive;
  ref = await snapshot();
  await snapshot();
  archive.isConnected = false;
  assert.equal((await stale(ref)).state.currentRef, undefined, 'removed');
  archive.isConnected = true;

  // A ref from browser_find has no role and name to compare.
  context.document.querySelectorAll = () => [archive];
  const found = (await send({ action: 'find', query: 'button' })).results[0].ref;
  await send({ action: 'getSnapshot' });
  assert.equal((await stale(found)).state.currentRef, undefined, 'browser_find ref');
}

async function testBackground() {
  const source = fs.readFileSync('extension/background/background.js', 'utf8');
  let calls = [], snapshotError = false, actionError = false;
  const context = vm.createContext({
    getActiveTabId: async () => 42,
    tabsCreated: 0, openedTabs: [], actionStarts: new Map(),
    dispatchCommand: async params => { calls.push(params); return actionError ? { error: 'stale ref' } : { ok: true }; },
    forwardToContent: async (action, params) => {
      calls.push({ action, ...params });
      return snapshotError ? { error: 'restricted page' } : { snapshot: 'tree', truncated: true, ...(params.focusLastAction ? { target_ref: 's2e100' } : {}) };
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
    assert.equal(calls[1].focusLastAction, action !== 'navigate');
    assert.equal(calls[1].observation, { type: 'browser_type', fill: 'browser_type', selectOption: 'browser_select_option',
      checkBox: 'browser_check', submitForm: 'browser_submit_form' }[action] || `browser_${action}`);
    assert.equal(result.target_ref, action !== 'navigate' ? 's2e100' : undefined);
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
  await testStaleRefSuggestions();
  await testBackground();
  console.log('Snapshot/ref and post-action snapshot checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
