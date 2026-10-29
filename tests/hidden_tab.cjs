// Run with: node tests/hidden_tab.cjs (no browser or npm dependencies).
// Snapshots and actions say when the tab is hidden, and an unrendered item of an open popup or a
// hidden tab gets its own not_rendered hint (#328).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {}

class Element extends EventTarget {
  constructor(tagName, id, attrs = {}, children = [], style = {}) {
    super();
    Object.assign(this, { tagName, id, attrs, nodeType: 1, isConnected: true, hidden: false, rects: 1,
      popoverOpen: false, style: { display: 'block', visibility: 'visible', ...style }, events: [] });
    this.childNodes = this.children = children;
    children.forEach(child => { child.parentElement = this; });
    for (const type of ['mousedown', 'click', 'dblclick', 'mouseover']) {
      this.addEventListener(type, event => this.events.push(event.type));
    }
  }
  get textContent() { return this.children.length ? this.children.map(c => c.textContent).join('') : this.id; }
  get innerText() { return this.textContent; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  // The selectors notRenderedWarning asks about; anything else matches nothing.
  matches(selector) {
    return selector.split(/,\s*/).some(part => {
      if (part === ':popover-open') return this.popoverOpen;
      if (part === 'dialog[open]') return this.tagName === 'DIALOG' && 'open' in this.attrs;
      if (part === '[popover]') return 'popover' in this.attrs;
      const role = /^\[role=(\w+)\]$/.exec(part);
      return !!role && this.attrs.role === role[1];
    });
  }
  closest() { return null; }
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
  getClientRects() { return Array.from({ length: this.rects }, () => this.getBoundingClientRect()); }
  click() { this.dispatchEvent(new MouseEvent('click', { bubbles: true })); }
}

function contentScript(elements, body) {
  let listener;
  const document = {
    body, title: 'Menu page', readyState: 'complete', visibilityState: 'visible',
    documentElement: { lang: 'en', scrollWidth: 800, scrollHeight: 600, appendChild() {} },
    createElement: () => ({ remove() {} }),
    forms: [], links: [], images: [],
    getElementById: () => null,
    querySelector: selector => elements[selector] || null,
    querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
  };
  const context = vm.createContext({
    console, URL, crypto: webcrypto, Event, MouseEvent, PointerEvent: MouseEvent, setTimeout,
    CSS: { escape: x => x },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    location: { href: 'https://example.test/menu' },
    window: { innerWidth: 800, innerHeight: 600, scrollX: 0, scrollY: 0,
      __kitsuneDialogSnapshot: () => '{"dialogs":[]}',
      getComputedStyle: el => ({ ...el.style, getPropertyValue: () => '' }) },
    document,
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve))
    .then(result => JSON.parse(JSON.stringify(result)));
  return { send, document };
}

async function testVisibilityState() {
  const go = new Element('BUTTON', 'go');
  const body = new Element('BODY', 'body', {}, [new Element('MAIN', 'main', {}, [go])]);
  const { send, document } = contentScript({ '#go': go }, body);

  // A shown tab adds nothing.
  assert.equal((await send({ action: 'getSnapshot', text: true })).visibilityState, undefined);
  assert.equal((await send({ action: 'click', selector: '#go' })).visibilityState, undefined);

  document.visibilityState = 'hidden';
  for (const msg of [
    { action: 'getSnapshot', text: true }, { action: 'getSnapshot', text: false },
    { action: 'getSnapshot', observation: 'browser_click', maxChars: 4000 },
    { action: 'click', selector: '#go' }, { action: 'hover', selector: '#go' },
    { action: 'doubleClick', selector: '#go' },
  ]) {
    const result = await send(msg);
    assert.equal(result.error, undefined, JSON.stringify(result));
    assert.equal(result.visibilityState, 'hidden', JSON.stringify(msg));
  }
  // Failed actions say so too; the hidden tab can explain a missing target.
  const missing = await send({ action: 'click', selector: '#missing' });
  assert.equal(missing.code, 'not_found');
  assert.equal(missing.visibilityState, 'hidden');
  // So do actions that waited for their target.
  assert.equal((await send({ action: 'click', selector: '#go', wait: true, timeout: 0 })).visibilityState, 'hidden');
  // Other reads keep their shape.
  assert.equal((await send({ action: 'getPageInfo' })).visibilityState, undefined);
  assert.equal((await send({ action: 'ping' })).visibilityState, undefined);
}

async function testNotRenderedHints() {
  const field = new Element('INPUT', 'field', {}, [], { display: 'none' });
  const menuItem = new Element('DIV', 'menu-item', { role: 'menuitem' }, [], { display: 'none' });
  const menu = new Element('DIV', 'menu', { role: 'menu' }, [new Element('DIV', 'group', {}, [menuItem])]);
  const closedItem = new Element('DIV', 'closed-item', { role: 'option' }, [], { display: 'none' });
  new Element('DIV', 'closed', { role: 'listbox' }, [closedItem], { display: 'none' });
  const dialogItem = new Element('BUTTON', 'dialog-item', {}, [], { visibility: 'hidden' });
  // An open <dialog> or popover is open whether or not it renders.
  new Element('DIALOG', 'dialog', { open: '' }, [dialogItem], { display: 'none' });
  const popoverItem = Object.assign(new Element('BUTTON', 'popover-item'), { rects: 0 });
  const popover = new Element('DIV', 'popover', { popover: 'auto' }, [popoverItem], { display: 'none' });
  popover.popoverOpen = true;
  const shut = Object.assign(new Element('BUTTON', 'shut'), { rects: 0 });
  new Element('DIV', 'shut-popover', { popover: 'auto' }, [shut], { display: 'none' });
  // A hidden native checkbox in a rendered modal still has a label to act on.
  const modalField = new Element('INPUT', 'modal-field', {}, [], { display: 'none' });
  new Element('DIV', 'modal', { role: 'dialog' }, [new Element('LABEL', 'remember', {}, [modalField])]);
  // The popup walk goes up through shadow roots and slots.
  const shadowItem = new Element('DIV', 'shadow-item', { role: 'menuitem' }, [], { display: 'none' });
  shadowItem.parentNode = { nodeType: 11, host: new Element('DIV', 'shadow-menu', { role: 'menu' }) };
  const slottedItem = new Element('DIV', 'slotted-item', { role: 'option' }, [], { display: 'none' });
  new Element('DIV', 'slot-host', {}, [slottedItem]);
  slottedItem.assignedSlot = new Element('SLOT', 'slot');
  new Element('DIV', 'slot-listbox', { role: 'listbox' }, [slottedItem.assignedSlot]);
  // A role popup beneath an unpainted shared layer is still open (#338).
  const unpaintedItem = new Element('DIV', 'unpainted-item', { role: 'menuitem' }, [], { display: 'none' });
  new Element('DIV', 'layer', {}, [new Element('DIV', 'unpainted', { role: 'menu' }, [unpaintedItem])],
    { display: 'none' });
  const elements = { 'unpainted-item': unpaintedItem, field, 'menu-item': menuItem, 'closed-item': closedItem, 'dialog-item': dialogItem,
    'popover-item': popoverItem, shut, 'modal-field': modalField, 'shadow-item': shadowItem,
    'slotted-item': slottedItem };
  const { send, document } = contentScript(
    Object.fromEntries(Object.entries(elements).map(([id, el]) => [`#${id}`, el])), new Element('BODY', 'body'));

  const warningFor = async (id, action = 'click') => {
    const el = elements[id];
    el.events = [];
    const result = await send({ action, selector: `#${id}` });
    assert.equal(result.ok, true, `${action} still succeeds on #${id}`);
    assert.ok(el.events.length, `${action} still dispatches to #${id}`);
    assert.equal(result.warning.code, 'hidden_or_covered');
    return result.warning;
  };

  // A hidden form control, or an item of a closed popup, in a shown tab: act on the label instead.
  for (const id of ['field', 'closed-item', 'shut', 'modal-field']) {
    const warning = await warningFor(id);
    assert.deepEqual(warning.state, { reason: 'not_rendered' }, id);
    assert.match(warning.hint, /prefer its label or visible control/);
  }
  // An item of an open popup: dispatching works, so keep acting on it.
  for (const id of ['unpainted-item', 'menu-item', 'dialog-item', 'popover-item', 'shadow-item', 'slotted-item']) {
    for (const action of ['click', 'doubleClick', 'hover']) {
      const warning = await warningFor(id, action);
      assert.deepEqual(warning.state, { reason: 'not_rendered', inPopup: true }, `${action} #${id}`);
      assert.match(warning.hint, /open menu, listbox, dialog or popover that the page has not painted/);
      assert.match(warning.hint, /Events dispatched to it work normally/);
      assert.match(warning.hint, /belongs to the control you meant/);
      assert.doesNotMatch(warning.hint, /prefer its label/);
    }
  }

  // In a hidden tab every unrendered target is flagged. A form control keeps the label hint: CSS hides
  // it whether or not the page paints.
  document.visibilityState = 'hidden';
  for (const id of ['field', 'modal-field']) {
    const warning = await warningFor(id);
    assert.deepEqual(warning.state, { reason: 'not_rendered', hiddenTab: true }, id);
    assert.match(warning.hint, /prefer its label or visible control/);
  }
  // Other targets get the background-tab hint.
  for (const [id, state] of [
    ['closed-item', { reason: 'not_rendered', hiddenTab: true }],
    ['menu-item', { reason: 'not_rendered', hiddenTab: true, inPopup: true }],
  ]) {
    const warning = await warningFor(id);
    assert.deepEqual(warning.state, state, id);
    assert.match(warning.hint, /The tab is in the background, and the page may not paint this element until the tab is shown/);
    assert.match(warning.hint, /Events dispatched to it work normally/);
  }
}

async function testBackgroundCopiesVisibility() {
  const source = fs.readFileSync('extension/background/background.js', 'utf8');
  let hidden = true;
  const context = vm.createContext({
    getActiveTabId: async () => 42,
    tabsCreated: 0, openedTabs: [], actionStarts: new Map(), tabLoads: new Map(), mainFrameStatus: new Map(),
    userActiveError: async () => null, guardedTabId: async () => null, touchStart() {}, touchEnd() {},
    protectedTabError: async () => null,
    getOpenedTabs: async () => ({ opened_tabs: [] }),
    // navigate is the background's own: its result does not come from the page. The click ran while
    // the tab was hidden.
    dispatchCommand: async params => params.action === 'click'
      ? { ok: true, tag: 'button', visibilityState: 'hidden' } : { ok: true, url: 'https://example.test/' },
    forwardToContent: async () => ({ snapshot: 'tree', ...(hidden ? { visibilityState: 'hidden' } : {}) }),
  });
  vm.runInContext(source.slice(source.indexOf('const POST_SNAPSHOT_ACTIONS'), source.indexOf('async function dispatchCommand')), context);
  const navigated = await context.handleCommand({ action: 'navigate', url: 'https://example.test/' });
  assert.equal(navigated.snapshot, 'tree');
  assert.equal(navigated.visibilityState, 'hidden');
  assert.equal((await context.handleCommand({ action: 'click', selector: '#go' })).visibilityState, 'hidden');
  hidden = false;
  assert.equal((await context.handleCommand({ action: 'navigate', url: 'https://example.test/' })).visibilityState,
    undefined);
  // The user showed the tab before the post-action snapshot: the result describes that snapshot.
  const clicked = await context.handleCommand({ action: 'click', selector: '#go' });
  assert.equal(clicked.snapshot, 'tree');
  assert.equal(clicked.visibilityState, undefined);
}

(async () => {
  await testVisibilityState();
  await testNotRenderedHints();
  await testBackgroundCopiesVisibility();
  console.log('Hidden tab checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
