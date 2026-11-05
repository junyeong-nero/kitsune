// Run with: node tests/delta_removals.cjs (no browser or npm dependencies).
// #457: a delta counts a line whose element is gone or no longer shown as a change, and names
// the top element of each removed part in one line, so closing a dialog or a listbox is not
// no_visible_change. The pages follow the gallery's ui-modal and ui-autocomplete.
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

class Element extends EventTarget {
  constructor(tagName, attrs = {}, nodes = [], props = {}) {
    super();
    Object.assign(this, { tagName, attrs, nodeType: 1, childNodes: [], parentElement: null,
      isContentEditable: false, onclick: null }, props);
    for (const node of nodes) {
      const child = typeof node === 'string' ? { nodeType: 3, textContent: node } : node;
      child.parentElement = this;
      this.childNodes.push(child);
    }
  }
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  get textContent() { return this.childNodes.map(node => node.textContent).join(''); }
  get innerText() { return this.textContent; }
  get isConnected() {
    for (let el = this; el; el = el.parentElement) if (el === document.body) return true;
    return false;
  }
  remove() {
    const siblings = this.parentElement.childNodes;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentElement = null;
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  get id() { return this.attrs.id || ''; }
  get hidden() { return this.hasAttribute('hidden'); }
  matches() { return false; }
  closest() { return null; }
  contains(other) {
    for (let el = other; el; el = el.parentElement) if (el === this) return true;
    return false;
  }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  getClientRects() {
    for (let el = this; el; el = el.parentElement) if (el.hidden) return [];
    return [{}];
  }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
  scrollIntoView() {}
  focus() {}
  click() {}
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);
const button = (label, onClick) => {
  const el = new Element('BUTTON', {}, [label], { type: 'button', value: '' });
  el.addEventListener('click', onClick);
  return el;
};

// ui-modal: "저장" closes the dialog by setting hidden on its backdrop.
const backdrop = h('DIV', { class: 'dialog-backdrop' },
  h('DIV', { role: 'dialog', 'aria-label': '알림 설정' }, h('H2', {}, '알림 설정'),
    new Element('INPUT', { role: 'switch', 'aria-label': '이메일 알림' }, [], { type: 'checkbox', checked: false, value: 'on' }),
    button('저장', () => backdrop.setAttribute('hidden', '')), button('취소', () => backdrop.setAttribute('hidden', ''))));
// ui-autocomplete: an option sets the input's value and hides the listbox.
const city = new Element('INPUT', { 'aria-label': '도시' }, [], { type: 'text', value: '부' });
const listbox = h('UL', { role: 'listbox', 'aria-label': '도시 목록' }, ...['부천', '부평', '부산'].map(name => {
  const option = h('LI', { role: 'option' }, name);
  option.addEventListener('click', () => {
    city.value = name;
    listbox.setAttribute('hidden', '');
  });
  return option;
}));
// A cookie notice whose button detaches it from the page.
const notice = h('DIV', { role: 'dialog', 'aria-label': '쿠키 안내' }, '쿠키를 사용합니다.',
  button('확인', () => notice.remove()));
const still = button('아무 일 없음', () => {});
const document = { body: h('BODY', {}, h('MAIN', {}, h('H1', {}, '설정'), city, listbox, still), backdrop, notice),
  title: 'gallery', getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };

let listener;
const content = vm.createContext({
  console, URL, crypto: webcrypto, CSS: { escape: x => x }, setTimeout, queueMicrotask,
  Event, MouseEvent, navigator: { platform: 'Linux x86_64' },
  HTMLInputElement: class {}, HTMLTextAreaElement: class {},
  location: { href: 'https://gallery.test/ui-modal' },
  window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
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
  tabState: async tabId => ({ tabId, status: 'complete', url: 'https://gallery.test/ui-modal' }),
  isRestrictedUrl: () => false, ensureContentScript: async () => {},
  withTabState: async (tabId, response) => ({ ...response, state: { ...response.state, tabId } }),
  tabsCreated: 0, openedTabs: [], actionStarts: new Map(), tabLoads: new Map(), mainFrameStatus: new Map(),
  userActiveError: async () => null, guardedTabId: async () => null, touchStart() {}, touchEnd() {},
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
const removedLines = snapshot => snapshot.split('\n').filter(line => line.startsWith('- removed: '));

(async () => {
  const page = await send('getSnapshot');
  const dialogRef = refOf(page.snapshot, 'dialog "알림 설정"');
  const noticeRef = refOf(page.snapshot, 'dialog "쿠키 안내"');

  // 1. An action that changes nothing is still no_visible_change.
  const nothing = await command({ action: 'click', selector: refOf(page.snapshot, 'button "아무 일 없음"') });
  assert.equal(nothing.snapshot_delta, true);
  assert.equal(nothing.no_visible_change, true, JSON.stringify(nothing));
  assert.deepEqual(removedLines(nothing.snapshot), []);

  // 2. ui-modal: "저장" hides the backdrop. The delta names the dialog by the ref the agent has,
  // and not its heading, switch or buttons.
  const saved = await command({ action: 'click', selector: refOf(page.snapshot, 'button "저장"') });
  assert.equal(saved.ok, true);
  assert.equal(saved.snapshot_delta, true);
  assert.equal('no_visible_change' in saved, false, JSON.stringify(saved));
  assert.deepEqual(removedLines(saved.snapshot), [`- removed: dialog "알림 설정" [ref=${dialogRef}]`]);

  // 3. ui-autocomplete: an option sets the input's value and hides the listbox.
  const picked = await command({ action: 'click', selector: refOf(page.snapshot, 'option "부천"') });
  assert.equal('no_visible_change' in picked, false, JSON.stringify(picked));
  assert.match(picked.snapshot, /- textbox "도시" \[ref=e\d+\] \[value="부천"\]/);
  assert.deepEqual(removedLines(picked.snapshot), [`- removed: listbox "도시 목록" [ref=${refOf(page.snapshot, 'listbox "도시 목록"')}]`]);

  // 4. A dialog the page detaches keeps the ref the agent saw it with, which pruning drops.
  const accepted = await command({ action: 'click', selector: refOf(page.snapshot, 'button "확인"') });
  assert.equal(notice.isConnected, false);
  assert.equal('no_visible_change' in accepted, false, JSON.stringify(accepted));
  assert.deepEqual(removedLines(accepted.snapshot), [`- removed: dialog "쿠키 안내" [ref=${noticeRef}]`]);

  // 5. What is gone is not named again by the next action.
  const again = await command({ action: 'click', selector: refOf(page.snapshot, 'button "아무 일 없음"') });
  assert.equal(again.no_visible_change, true, JSON.stringify(again));
  console.log('delta removal tests passed');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
