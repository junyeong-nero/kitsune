// Run with: node tests/opened_popup.cjs (no browser or npm dependencies).
// #329: a click or hover that opens a popup reports it as opened_popup with the control that
// opened it and its items' refs, also when a background tab never painted it, and snapshots
// keep such a popup, marked [not_rendered], while it stays open.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    Object.assign(this, { clientX: init.clientX, clientY: init.clientY, button: init.button || 0 });
  }
}

// A selector list of tag, #id, [attr], [attr=value] and :pseudo parts; :popover-open is the only
// pseudo-class an element matches.
function matches(el, selector) {
  return selector.split(',').some(part => {
    const match = /^([a-z]+)?((?:\[[^\]]+\]|#[\w-]+|:[\w-]+)*)$/i.exec(part.trim());
    if (!match) return false;
    if (match[1] && match[1].toUpperCase() !== el.tagName) return false;
    return (match[2].match(/\[[^\]]+\]|#[\w-]+|:[\w-]+/g) || []).every(token => {
      if (token.startsWith('#')) return el.id === token.slice(1);
      if (token.startsWith(':')) return token === ':popover-open' && !!el.popoverOpen;
      const [, name, value] = /^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(token) || [];
      return name !== undefined && el.hasAttribute(name) && (value === undefined || el.getAttribute(name) === value);
    });
  });
}

const observers = new Set();

class Element extends EventTarget {
  constructor(tagName, attrs = {}, nodes = [], props = {}) {
    super();
    Object.assign(this, { tagName, attrs, nodeType: 1, childNodes: [], parentElement: null, display: 'block',
      isContentEditable: false, onclick: null }, props);
    nodes.forEach(node => this.appendChild(node, false));
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  get firstElementChild() { return this.children[0] || null; }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  get innerText() { return this.textContent; }
  get isConnected() {
    for (let el = this; el; el = el.parentElement) if (el === document.body) return true;
    return false;
  }
  appendChild(node, notify = true) {
    const child = typeof node === 'string' ? { nodeType: 3, textContent: node } : node;
    child.parentElement = this;
    this.childNodes.push(child);
    if (notify) for (const observer of observers) observer.record({ type: 'childList', target: this, addedNodes: [child] });
    return child;
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  get id() { return this.attrs.id || ''; }
  get hidden() { return this.hasAttribute('hidden'); }
  matches(selector) { return matches(this, selector); }
  closest(selector) {
    for (let el = this; el; el = el.parentElement) if (el.matches(selector)) return el;
    return null;
  }
  contains(other) {
    for (let el = other; el; el = el.parentElement) if (el === this) return true;
    return false;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = el => el.children.forEach(child => { if (child.matches(selector)) found.push(child); walk(child); });
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  getClientRects() {
    for (let el = this; el; el = el.parentElement) if (el.display === 'none' || el.hidden) return [];
    return [{}];
  }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
  scrollIntoView() {}
  focus() {}
  click() {}
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);

class FakeMutationObserver {
  constructor(callback) { Object.assign(this, { callback, queue: [], roots: [] }); }
  observe(root) {
    this.roots.push(root);
    observers.add(this);
  }
  record(record) {
    if (!this.roots.some(root => root === document || root.contains(record.target))) return;
    this.queue.push(record);
    queueMicrotask(() => { if (this.queue.length) this.callback(this.queue.splice(0)); });
  }
  takeRecords() { return this.queue.splice(0); }
  disconnect() { observers.delete(this); }
}

// A video row's "Action menu" fills a menu into a container at the end of <body> that a
// background tab never paints (display: none), next to a menu left over from earlier.
const saved = [];
const popupLayer = h('DIV', { id: 'popup-layer' },
  h('DIV', { role: 'menu', 'aria-label': 'Old menu' }, h('DIV', { role: 'menuitem' }, 'Stale item')));
popupLayer.display = 'none';
const more = h('BUTTON', { id: 'more', 'aria-label': 'Action menu for Fox video', 'aria-haspopup': 'menu',
  'aria-expanded': 'false' });
more.addEventListener('click', () => {
  if (more.getAttribute('aria-expanded') === 'true') {
    more.setAttribute('aria-expanded', 'false');
    return;
  }
  more.setAttribute('aria-expanded', 'true');
  if (popupLayer.querySelector('#video-menu')) return;
  const items = ['Add to queue', 'Save to Watch later', 'Share'].map(name => {
    const item = h('DIV', { role: 'menuitem' }, name);
    item.addEventListener('click', () => saved.push(name));
    return item;
  });
  popupLayer.appendChild(h('DIV', { id: 'video-menu', role: 'menu', 'aria-label': 'Video actions' }, ...items));
});
// A sort control whose listbox (aria-controls) is shown in place.
const sortList = h('UL', { id: 'sort-list', role: 'listbox', hidden: '' },
  h('LI', { role: 'option' }, 'Newest'), h('LI', { role: 'option' }, 'Oldest'));
const sort = h('BUTTON', { id: 'sort', 'aria-expanded': 'false', 'aria-controls': 'sort-list' }, 'Sort');
sort.addEventListener('click', () => {
  sort.setAttribute('aria-expanded', 'true');
  sortList.removeAttribute('hidden');
});
const like = h('BUTTON', { id: 'like' }, 'Like');
// A filter menu that stays in its hidden container and is only filled in.
const filterMenu = h('DIV', { id: 'filter-menu', role: 'menu', 'aria-label': 'Filters' });
const filterLayer = h('DIV', {}, filterMenu);
filterLayer.display = 'none';
const filter = h('BUTTON', { id: 'filter', 'aria-expanded': 'false' }, 'Filter');
filter.addEventListener('click', () => {
  filter.setAttribute('aria-expanded', 'true');
  for (const name of ['Today', 'This week']) filterMenu.appendChild(h('DIV', { role: 'menuitemradio' }, name));
});
// A card whose hover menu (no aria-expanded) opens in another unpainted layer.
const hoverLayer = h('DIV', { class: 'layer' });
hoverLayer.display = 'none';
const card = h('DIV', { id: 'card' }, 'Owl video');
card.addEventListener('mouseover', () => {
  if (!hoverLayer.firstElementChild) {
    hoverLayer.appendChild(h('DIV', { role: 'menu', 'aria-label': 'Card menu' }, h('A', { href: '#' }, 'Open')));
  }
});
const main = h('MAIN', {}, h('H1', {}, 'Videos'), h('DIV', {}, 'Fox video', more), sort, sortList, like, filter, card);
const document = { body: h('BODY', {}, main, filterLayer, hoverLayer, popupLayer), title: 'Videos',
  getElementById: id => document.body.querySelector(`#${id}`),
  querySelector: selector => document.body.querySelector(selector),
  querySelectorAll: selector => document.body.querySelectorAll(selector),
};

let listener;
const content = vm.createContext({
  console, URL, crypto: webcrypto, CSS: { escape: x => x }, setTimeout, queueMicrotask,
  Event, MouseEvent, MutationObserver: FakeMutationObserver, navigator: { platform: 'Linux x86_64' },
  HTMLInputElement: class {}, HTMLTextAreaElement: class {},
  location: { href: 'https://videos.test/results' },
  window: { getComputedStyle: el => ({ display: el.display, visibility: 'visible', getPropertyValue: () => '' }) },
  document,
  browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
    sendMessage: () => Promise.resolve(null) } },
});
vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), content);

// The background's command path and public short refs, in front of that page.
const source = fs.readFileSync('extension/background/background.js', 'utf8');
const background = vm.createContext({
  console, URL, Map, Set, Promise, setTimeout,
  browser: { storage: { local: { get: async () => ({}), set: async () => {} } },
    tabs: { sendMessage: async (tabId, msg) => new Promise(resolve => listener(msg, {}, resolve)) } },
  getActiveTabId: async () => 1,
  tabState: async tabId => ({ tabId, status: 'complete', url: 'https://videos.test/results' }),
  isRestrictedUrl: () => false, ensureContentScript: async () => {},
  withTabState: async (tabId, response) => ({ ...response, state: { ...response.state, tabId } }),
  tabsCreated: 0, openedTabs: [], actionStarts: new Map(),
  userActiveError: async () => null, guardedTabId: async () => null, touchStart() {}, touchEnd() {},
});
vm.runInContext(fs.readFileSync('extension/background/history.js', 'utf8'), background);
vm.runInContext(source.slice(source.indexOf('const POST_SNAPSHOT_ACTIONS'), source.indexOf('async function dispatchCommand')), background);
vm.runInContext(source.slice(source.indexOf('const MISSING_CONTENT_RECEIVER'), source.indexOf('async function getActiveTabId')), background);
background.dispatchCommand = params => background.forwardToContent(params.action, params);
const send = (action, params = {}) => background.forwardToContent(action, { tabId: 1, ...params })
  .then(result => JSON.parse(JSON.stringify(result)));
const command = params => background.handleCommand({ tabId: 1, ...params })
  .then(result => JSON.parse(JSON.stringify(result)));
const refOf = (snapshot, label) => {
  const line = snapshot.split('\n').find(line => line.includes(label));
  assert.ok(line, `${label} in\n${snapshot}`);
  return line.match(/\[ref=(e\d+)\]/)[1];
};

(async () => {
  const page = await send('getSnapshot');
  assert.doesNotMatch(page.snapshot, /menu "|Stale item/, 'hidden menus are left out');
  const moreRef = refOf(page.snapshot, 'button "Action menu for Fox video"');

  // 1. The menu opens unpainted: the click names its opener and items by ref.
  const opened = await command({ action: 'click', selector: moreRef });
  assert.equal(opened.ok, true);
  const popup = opened.opened_popup;
  assert.ok(popup, JSON.stringify(opened));
  assert.deepEqual({ ...popup, ref: 'e', items: popup.items.map(item => ({ ...item, ref: 'e' })) }, {
    ref: 'e', role: 'menu', name: 'Video actions', opened_by: moreRef, rendered: false,
    items: ['Add to queue', 'Save to Watch later', 'Share'].map(name => ({ ref: 'e', role: 'menuitem', name })),
  });
  assert.ok(popup.ref.match(/^e\d+$/) && popup.items.every(item => /^e\d+$/.test(item.ref)));
  assert.equal(new Set([popup.ref, ...popup.items.map(item => item.ref)]).size, 4);
  assert.doesNotMatch(JSON.stringify(opened), /popupWatch/, 'the content script\'s mark stays internal');
  // The click's delta lists it with the same refs, marked [not_rendered].
  assert.equal(opened.snapshot_delta, true);
  assert.match(opened.snapshot, new RegExp(`- menu "Video actions" \\[ref=${popup.ref}\\] \\[not_rendered\\]`));
  assert.match(opened.snapshot, new RegExp(`- menuitem "Save to Watch later" \\[ref=${popup.items[1].ref}\\]`));

  // 2. Snapshots keep it while the opener stays expanded, but not the other hidden menu.
  const kept = await send('getSnapshot');
  assert.match(kept.snapshot, new RegExp(`button "Action menu for Fox video" \\[ref=${moreRef}\\] \\[expanded=true\\]`));
  const menuLines = kept.snapshot.split('\n').filter(line => /menu|Stale/.test(line) && !line.includes('Action menu'));
  assert.deepEqual(menuLines.map(line => line.trim().replace(/\[ref=e\d+\] /g, '').replace(/ \[ref=e\d+\]$/, '')), [
    '- menu "Video actions" [not_rendered]',
    '- menuitem "Add to queue"', '- menuitem "Save to Watch later"', '- menuitem "Share"',
  ]);
  const scoped = await send('getSnapshot', { ref: popup.ref });
  assert.match(scoped.snapshot, /^- menu "Video actions" \[ref=e\d+\] \[not_rendered\]\n {2}- menuitem "Add to queue"/);

  // 3. An item's ref acts on the unpainted item (with the usual not_rendered warning).
  const item = await command({ action: 'click', selector: popup.items[1].ref });
  assert.equal(item.ok, true);
  assert.equal(item.warning?.state?.reason, 'not_rendered');
  assert.deepEqual(saved, ['Save to Watch later']);
  assert.equal(item.opened_popup, undefined, 'clicking an item opens nothing');

  // 4. Once the opener's aria-expanded turns false, the menu leaves the snapshot.
  const closed = await command({ action: 'click', selector: moreRef });
  assert.equal(closed.ok, true);
  assert.equal(closed.opened_popup, undefined);
  assert.equal(popupLayer.querySelector('#video-menu').isConnected, true);
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /Video actions|Save to Watch later/);

  // 5. A click that opens nothing has no opened_popup.
  const liked = await command({ action: 'click', selector: '#like' });
  assert.equal(liked.ok, true);
  assert.equal('opened_popup' in liked, false);

  // 6. A painted popup (aria-controls) is reported as rendered, and snapshots list it as usual.
  const sorted = await command({ action: 'click', selector: '#sort' });
  assert.deepEqual({ ...sorted.opened_popup, ref: 'e', items: sorted.opened_popup.items.map(o => o.name) }, {
    ref: 'e', role: 'listbox', name: '', opened_by: refOf(sorted.snapshot, 'button "Sort"'), rendered: true,
    items: ['Newest', 'Oldest'],
  });
  assert.doesNotMatch(sorted.snapshot, /not_rendered/);
  assert.match(sorted.snapshot, /- listbox "[^"]*" \[ref=e\d+\]/);

  // 7. A menu that stays in its hidden container and is only filled in is found by the items added.
  const filtered = await command({ action: 'click', selector: '#filter' });
  assert.deepEqual([filtered.opened_popup.name, filtered.opened_popup.rendered, filtered.opened_popup.items.map(o => o.role)],
    ['Filters', false, ['menuitemradio', 'menuitemradio']]);
  assert.match(filtered.snapshot, /- menu "Filters" \[ref=e\d+\] \[not_rendered\]/);

  // 8. A hover menu without aria-expanded stays in snapshots until the page closes its layer.
  const hovered = await command({ action: 'hover', selector: '#card' });
  assert.equal(hovered.ok, true);
  assert.deepEqual([hovered.opened_popup.name, hovered.opened_popup.rendered, hovered.opened_popup.items],
    ['Card menu', false, [{ ref: hovered.opened_popup.items[0].ref, role: 'link', name: 'Open' }]]);
  assert.match((await send('getSnapshot')).snapshot, /- menu "Card menu" \[ref=e\d+\] \[not_rendered\]/);
  hoverLayer.setAttribute('class', 'layer closed');
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /Card menu/);
  hoverLayer.setAttribute('class', 'layer');
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /Card menu/, 'a closed popup is not kept again');

  console.log('Opened popup checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
