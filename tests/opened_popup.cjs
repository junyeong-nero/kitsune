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
// A <dialog> without open is display: none, as in the UA stylesheet.
const displayOf = el => (el.tagName === 'DIALOG' && !el.hasAttribute('open') ? 'none' : el.display);
// visibility is inherited unless an element sets its own.
const visibilityOf = el => el.visibility || (el.parentElement ? visibilityOf(el.parentElement) : 'visible');
// A background tab does not run animation frames.
const frames = [];
const requestAnimationFrame = callback => frames.push(callback);

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
    for (let el = this; el; el = el.parentElement) if (displayOf(el) === 'none' || el.hidden) return [];
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
// A second row's "Action menu" (aria-haspopup, no aria-expanded) refills the same menu.
const more2 = h('BUTTON', { id: 'more-2', 'aria-label': 'Action menu for Owl video', 'aria-haspopup': 'menu' });
more2.addEventListener('click', () => {
  const menu = popupLayer.querySelector('#video-menu');
  menu.childNodes.forEach(node => { node.parentElement = null; });
  menu.childNodes = [];
  for (const name of ['Add to queue', 'Not interested']) menu.appendChild(h('DIV', { role: 'menuitem' }, name));
});
// A sort control whose listbox (aria-controls) waits, already open, in a display: none wrapper
// that the click shows.
const sortList = h('UL', { id: 'sort-list', role: 'listbox' },
  h('LI', { role: 'option' }, 'Newest'), h('LI', { role: 'option' }, 'Oldest'));
const sortLayer = h('DIV', {}, sortList);
sortLayer.display = 'none';
const sort = h('BUTTON', { id: 'sort', 'aria-expanded': 'false', 'aria-controls': 'sort-list' }, 'Sort');
sort.addEventListener('click', () => {
  sort.setAttribute('aria-expanded', 'true');
  sortLayer.display = 'block';
});
// A language control that mounts its listbox closed, under an aria-hidden wrapper.
const lang = h('BUTTON', { id: 'lang' }, 'Language');
lang.addEventListener('click', () => {
  const wrapper = h('DIV', { 'aria-hidden': 'true' },
    h('DIV', { role: 'listbox', 'aria-label': 'Languages' }, h('DIV', { role: 'option' }, 'English')));
  wrapper.display = 'none';
  document.body.appendChild(wrapper);
});
// A share button whose dropdown drops aria-hidden and fills in at once, but is shown only on the
// next animation frame, which a background tab never runs.
const shareDropdown = h('DIV', { 'aria-hidden': 'true' });
shareDropdown.display = 'none';
const share = h('BUTTON', { id: 'share' }, 'Share');
share.addEventListener('click', () => {
  shareDropdown.removeAttribute('aria-hidden');
  shareDropdown.appendChild(h('DIV', { role: 'menu', 'aria-label': 'Share options' },
    h('DIV', { role: 'menuitem' }, 'Copy link'), h('DIV', { role: 'menuitem' }, 'Embed')));
  requestAnimationFrame(() => { shareDropdown.display = 'block'; });
});
// A row whose double-click opens a rename <dialog> in an unpainted layer (display: none and
// visibility: hidden, which its items inherit). The dialog holds a hidden input, items hidden
// themselves and a field hidden with its own visibility: hidden, which stay out of the snapshot
// and items.
const renameDialog = h('DIALOG', { 'aria-label': 'Rename' },
  new Element('INPUT', { type: 'hidden', name: 'csrf' }, [], { type: 'hidden', name: 'csrf', value: 'csrf-token' }),
  h('BUTTON', {}, 'Save'), h('BUTTON', { hidden: '' }, 'Delete'), h('BUTTON', {}, 'Archive'),
  new Element('DIV', {}, [new Element('TEXTAREA', { name: 'notes' }, [], { name: 'notes', value: 'api-token-123',
    visibility: 'hidden' })], { visibility: 'visible' }));
renameDialog.children[3].display = 'none';
const dialogLayer = h('DIV', {}, renameDialog);
dialogLayer.display = 'none';
dialogLayer.visibility = 'hidden';
const rename = h('BUTTON', { id: 'rename' }, 'Rename Fox video');
rename.addEventListener('dblclick', () => renameDialog.setAttribute('open', ''));
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
hoverLayer.style = { display: '', visibility: '' };
const card = h('DIV', { id: 'card' }, 'Owl video');
card.addEventListener('mouseover', () => {
  if (!hoverLayer.firstElementChild) {
    hoverLayer.appendChild(h('DIV', { role: 'menu', 'aria-label': 'Card menu' }, h('A', { href: '#' }, 'Open')));
  }
});
// A "Show more" that mounts comments with a closed sort dropdown: its listbox has no hidden
// attribute, but sits in its own new display: none host, and nothing signals a popup.
const commentSection = h('DIV', {});
const showMore = h('BUTTON', { id: 'show-more' }, 'Show more');
showMore.addEventListener('click', () => {
  const host = h('DIV', { class: 'dropdown' }, h('DIV', { role: 'listbox', 'aria-label': 'Sort comments' },
    h('DIV', { role: 'option' }, 'Top comments'), h('DIV', { role: 'option' }, 'Newest first')));
  host.display = 'none';
  commentSection.appendChild(h('DIV', {}, h('P', {}, 'More comments'), host));
});
// Openers that mount their menu, the first time, in its own new display: none host at the end
// of <body>: one with aria-haspopup, one whose aria-expanded turns "true" (no aria-controls).
const mountMenu = (label, item) => {
  const host = h('DIV', {}, h('DIV', { role: 'menu', 'aria-label': label }, h('DIV', { role: 'menuitem' }, item)));
  host.display = 'none';
  document.body.appendChild(host);
};
const playlist = h('BUTTON', { id: 'playlist', 'aria-haspopup': 'true' }, 'Playlist');
playlist.addEventListener('click', () => mountMenu('Playlist actions', 'Shuffle'));
const speed = h('BUTTON', { id: 'speed', 'aria-expanded': 'false' }, 'Speed');
speed.addEventListener('click', () => {
  speed.setAttribute('aria-expanded', 'true');
  mountMenu('Playback speed', 'Normal');
});
// An account control whose menu (aria-controls) waits, open, in a display: none layer. The page
// later hides it with hidden but leaves aria-expanded="true".
const accountMenu = h('DIV', { id: 'account-menu', role: 'menu', 'aria-label': 'Account options' },
  h('DIV', { role: 'menuitem' }, 'Delete account'));
const accountLayer = h('DIV', {}, accountMenu);
accountLayer.display = 'none';
const account = h('BUTTON', { id: 'account', 'aria-expanded': 'false', 'aria-controls': 'account-menu' }, 'Account');
account.addEventListener('click', () => account.setAttribute('aria-expanded', 'true'));
// A notifications control whose panel (aria-controls) stays aria-hidden after it expands.
const notifyPanel = h('DIV', { id: 'notify-panel', role: 'menu', 'aria-label': 'Notifications panel', 'aria-hidden': 'true' },
  h('DIV', { role: 'menuitem' }, 'Mark all read'));
const notifyLayer = h('DIV', {}, notifyPanel);
notifyLayer.display = 'none';
const notify = h('BUTTON', { id: 'notify', 'aria-expanded': 'false', 'aria-controls': 'notify-panel' }, 'Notify');
notify.addEventListener('click', () => notify.setAttribute('aria-expanded', 'true'));
const main = h('MAIN', {}, h('H1', {}, 'Videos'), h('DIV', {}, 'Fox video', more), h('DIV', {}, 'Owl video', more2),
  sort, sortLayer, lang, share, rename, like, filter, card, showMore, commentSection, playlist, speed, account, notify);
const document = { body: h('BODY', {}, main, filterLayer, hoverLayer, dialogLayer, shareDropdown, accountLayer, notifyLayer,
  popupLayer), title: 'Videos',
  getElementById: id => document.body.querySelector(`#${id}`),
  querySelector: selector => document.body.querySelector(selector),
  querySelectorAll: selector => document.body.querySelectorAll(selector),
};

let listener;
const content = vm.createContext({
  console, URL, crypto: webcrypto, CSS: { escape: x => x }, setTimeout, queueMicrotask, requestAnimationFrame,
  Event, MouseEvent, MutationObserver: FakeMutationObserver, navigator: { platform: 'Linux x86_64' },
  HTMLInputElement: class {}, HTMLTextAreaElement: class {},
  location: { href: 'https://videos.test/results' },
  window: { getComputedStyle: el => ({ display: displayOf(el), visibility: visibilityOf(el), getPropertyValue: () => '' }) },
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
  tabsCreated: 0, openedTabs: [], actionStarts: new Map(), tabLoads: new Map(), mainFrameStatus: new Map(),
  userActiveError: async () => null, viewedTabError: async () => null, guardedTabId: async () => null, touchStart() {}, touchEnd() {},
});
vm.runInContext(fs.readFileSync('extension/background/history.js', 'utf8'), background);
vm.runInContext(source.slice(source.indexOf('const POST_SNAPSHOT_ACTIONS'), source.indexOf('async function dispatchCommand')), background);
  background.tabWriteError = async () => null; // Permission policy is tested in user_tabs_read_only.cjs.
  background.handoffs = new Map();
  background.handoffTransportEpoch = 0;
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

  // 5. Another row's opener without aria-expanded (aria-haspopup) refills the same menu.
  const refilled = await command({ action: 'click', selector: '#more-2' });
  assert.deepEqual({ ...refilled.opened_popup, items: refilled.opened_popup.items.map(o => o.name) }, {
    ref: popup.ref, role: 'menu', name: 'Video actions', opened_by: refOf(refilled.snapshot, 'Action menu for Owl video'),
    rendered: false, items: ['Add to queue', 'Not interested'],
  });
  assert.match((await send('getSnapshot')).snapshot, /- menu "Video actions" \[ref=e\d+\] \[not_rendered\]\n *- menuitem "Add to queue" \[ref=e\d+\]\n *- menuitem "Not interested"/);
  popupLayer.setAttribute('class', 'closed');
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /Video actions/);

  // 6. A click that opens nothing has no opened_popup.
  const liked = await command({ action: 'click', selector: '#like' });
  assert.equal(liked.ok, true);
  assert.equal('opened_popup' in liked, false);

  // 7. A painted popup (aria-controls, already open in a hidden wrapper) is reported as rendered, and snapshots list it as usual.
  const sorted = await command({ action: 'click', selector: '#sort' });
  assert.deepEqual({ ...sorted.opened_popup, ref: 'e', items: sorted.opened_popup.items.map(o => o.name) }, {
    ref: 'e', role: 'listbox', name: '', opened_by: refOf(sorted.snapshot, 'button "Sort"'), rendered: true,
    items: ['Newest', 'Oldest'],
  });
  assert.doesNotMatch(sorted.snapshot, /not_rendered/);
  assert.match(sorted.snapshot, /- listbox "[^"]*" \[ref=e\d+\]/);

  // 8. A closed listbox mounted under an aria-hidden wrapper is not reported or kept.
  const mounted = await command({ action: 'click', selector: '#lang' });
  assert.equal(mounted.ok, true);
  assert.equal('opened_popup' in mounted, false);
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /Languages|English/);

  // 9. A menu that stays in its hidden container and is only filled in is found by the items added.
  const filtered = await command({ action: 'click', selector: '#filter' });
  assert.deepEqual([filtered.opened_popup.name, filtered.opened_popup.rendered, filtered.opened_popup.items.map(o => o.role)],
    ['Filters', false, ['menuitemradio', 'menuitemradio']]);
  assert.match(filtered.snapshot, /- menu "Filters" \[ref=e\d+\] \[not_rendered\]/);

  // 10. A dropdown whose show step waits on an animation frame a background tab never runs.
  const shared = await command({ action: 'click', selector: '#share' });
  assert.deepEqual([shared.opened_popup.name, shared.opened_popup.rendered, shared.opened_popup.items.map(o => o.name)],
    ['Share options', false, ['Copy link', 'Embed']]);
  assert.equal(frames.length, 1, 'the show step is still waiting');
  assert.match((await send('getSnapshot')).snapshot, /- menu "Share options" \[ref=e\d+\] \[not_rendered\]\n *- menuitem "Copy link"/);
  frames.splice(0).forEach(callback => callback());
  const painted = (await send('getSnapshot')).snapshot;
  assert.match(painted, /- menuitem "Copy link" \[ref=e\d+\]/);
  assert.doesNotMatch(painted, /Share options/, 'once painted, it is listed as usual');

  // 11. A double-click opens an unpainted <dialog>: its hidden input, the items hidden
  // themselves and the field with its own visibility: hidden stay out of items and the
  // snapshot; items that only inherit visibility: hidden stay in.
  const renameRef = refOf((await send('getSnapshot')).snapshot, 'button "Rename Fox video"');
  const renamed = await command({ action: 'doubleClick', selector: renameRef });
  assert.equal(renamed.ok, true);
  assert.deepEqual({ ...renamed.opened_popup, ref: 'e', items: renamed.opened_popup.items.map(o => o.name) }, {
    ref: 'e', role: 'dialog', name: 'Rename', opened_by: renameRef, rendered: false,
    items: ['Save'],
  });
  const dialogSnapshot = (await send('getSnapshot', { ref: renamed.opened_popup.ref })).snapshot;
  assert.match(dialogSnapshot, /^- dialog "Rename" \[ref=e\d+\] \[not_rendered\]\n {2}- button "Save" \[ref=e\d+\]$/);
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /csrf-token|api-token|textbox|Delete|Archive/);
  renameDialog.removeAttribute('open');
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /dialog "Rename"|Save/);

  // 12. A hover menu without aria-expanded stays in snapshots until the page closes its layer.
  const hovered = await command({ action: 'hover', selector: '#card' });
  assert.equal(hovered.ok, true);
  assert.deepEqual([hovered.opened_popup.name, hovered.opened_popup.rendered, hovered.opened_popup.items],
    ['Card menu', false, [{ ref: hovered.opened_popup.items[0].ref, role: 'link', name: 'Open' }]]);
  assert.match((await send('getSnapshot')).snapshot, /- menu "Card menu" \[ref=e\d+\] \[not_rendered\]/);
  // Positioning writes to its inline style keep it.
  hoverLayer.setAttribute('style', 'top: 40px');
  assert.match((await send('getSnapshot')).snapshot, /- menu "Card menu" \[ref=e\d+\] \[not_rendered\]/);
  hoverLayer.setAttribute('class', 'layer closed');
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /Card menu/);
  hoverLayer.setAttribute('class', 'layer');
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /Card menu/, 'a closed popup is not kept again');

  // 13. A click that mounts a closed dropdown in its own new display: none host, with no
  // aria-expanded or aria-haspopup, reports nothing and adds nothing to snapshots.
  const more3 = await command({ action: 'click', selector: '#show-more' });
  assert.equal(more3.ok, true);
  assert.equal('opened_popup' in more3, false);
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /Sort comments|Top comments|Newest first/);

  // 14. A first-time menu in its own new hidden host is reported when the opener has
  // aria-haspopup, or when its aria-expanded turns "true".
  const listed = await command({ action: 'click', selector: '#playlist' });
  assert.deepEqual([listed.opened_popup?.name, listed.opened_popup?.rendered, listed.opened_popup?.items.map(o => o.name)],
    ['Playlist actions', false, ['Shuffle']]);
  const sped = await command({ action: 'click', selector: '#speed' });
  assert.deepEqual([sped.opened_popup?.name, sped.opened_popup?.rendered, sped.opened_popup?.items.map(o => o.name)],
    ['Playback speed', false, ['Normal']]);
  assert.match((await send('getSnapshot')).snapshot, /- menu "Playback speed" \[ref=e\d+\] \[not_rendered\]/);

  // 15. A popup found through aria-controls leaves snapshots once the page hides it with an
  // attribute, even while its opener keeps aria-expanded="true".
  const accounted = await command({ action: 'click', selector: '#account' });
  assert.equal(accounted.opened_popup?.name, 'Account options');
  assert.match((await send('getSnapshot')).snapshot, /- menu "Account options" \[ref=e\d+\] \[not_rendered\]/);
  accountMenu.setAttribute('hidden', '');
  assert.equal(account.getAttribute('aria-expanded'), 'true');
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /Account options|Delete account/);

  // 16. An aria-controls panel that stays aria-hidden after its opener expands is not reported.
  const notified = await command({ action: 'click', selector: '#notify' });
  assert.equal(notified.ok, true);
  assert.equal('opened_popup' in notified, false);
  assert.doesNotMatch((await send('getSnapshot')).snapshot, /Notifications panel|Mark all read/);

  // 17. A click whose popup nobody takes (click-and-wait-navigation) stops watching at once.
  const unwatched = await send('click', { selector: '#more', reportPopup: false });
  assert.deepEqual([unwatched.ok, 'popupWatch' in unwatched, observers.size], [true, false, 0]);

  console.log('Opened popup checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
