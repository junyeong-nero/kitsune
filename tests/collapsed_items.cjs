// Run with: node tests/collapsed_items.cjs (no browser or npm dependencies).
// Items a snapshot collapses into "- … N more similar items" stay reachable (#237): the steps
// of the issue on gallery ui-table, a table of 8 rows per page whose 8th row is 마이크.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

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
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  matches() { return false; }
  closest() { return null; }
  focus() { this.focused = true; }
  scrollIntoView() {}
  dispatchEvent(event) { this.onEvent?.(event); return true; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);

const PAGES = [
  [['노트북 거치대', 12], ['무선 마우스', 30], ['기계식 키보드', 8], ['USB 허브', 15], ['모니터 암', 7],
    ['웹캠', 9], ['헤드셋', 11], ['마이크', 3]],
  [['스피커', 2], ['마우스패드', 40], ['케이블 정리함', 6], ['노트북 파우치', 14], ['손목 받침대', 22],
    ['태블릿 펜', 4], ['독 스테이션', 10], ['HDMI 케이블', 25]],
];
const checkboxes = [];
const rows = PAGES[0].map(([name, stock]) => {
  const checkbox = new Element('INPUT', { 'aria-label': `${name} 선택` }, [],
    { type: 'checkbox', checked: false, value: 'on' });
  checkboxes.push(checkbox);
  return h('TR', {}, h('TD', {}, checkbox), h('TD', {}, name), h('TD', {}, '사무용품'), h('TD', {}, '₩10,000'),
    h('TD', {}, String(stock)));
});
function showPage(page) {
  PAGES[page].forEach(([name, stock], i) => {
    checkboxes[i].attrs['aria-label'] = `${name} 선택`;
    rows[i].children[1].childNodes[0].textContent = name;
    rows[i].children[4].childNodes[0].textContent = String(stock);
  });
}
const tbody = h('TBODY', {}, ...rows);
const header = h('TR', {}, ...['선택', '상품', '분류', '가격', '재고'].map(label => h('TH', {}, label)));
const pager = [1, 2, 3].map(page => new Element('BUTTON', {}, [String(page)], {
  value: '', onEvent: event => { if (event.type === 'keydown' && event.key === 'Enter') showPage(page - 1); },
}));
// Adds a 9th row past the five shown: only the run's hidden count changes.
function addRow() {
  const checkbox = new Element('INPUT', { 'aria-label': '추가 상품 선택' }, [], { type: 'checkbox', checked: false, value: 'on' });
  const row = h('TR', {}, h('TD', {}, checkbox), h('TD', {}, '추가 상품'), h('TD', {}, '사무용품'), h('TD', {}, '₩10,000'),
    h('TD', {}, '1'));
  row.parentElement = tbody;
  tbody.children.push(row);
  tbody.childNodes.push(row);
}
const addButton = new Element('BUTTON', {}, ['행 추가'], {
  value: '', onEvent: event => { if (event.type === 'keydown' && event.key === 'Enter') addRow(); },
});
const main = h('MAIN', {}, h('H1', {}, '재고 관리'), h('TABLE', {}, h('THEAD', {}, header), tbody),
  h('NAV', {}, ...pager, addButton));
// A 250-row log, and seven groups of seven items each.
const big = h('TBODY', {}, ...Array.from({ length: 250 }, (_, i) => h('TR', {}, h('TD', {}, `행 ${i + 1}`), h('TD', {}, '기록'))));
const groups = h('SECTION', {}, ...Array.from({ length: 7 }, (_, g) => h('DIV', {}, h('H2', {}, `그룹 ${g + 1}`),
  h('UL', {}, ...Array.from({ length: 7 }, (_, i) => h('LI', {}, h('DIV', {}, `항목 ${g + 1}-${i + 1}`)))))));
const body = h('BODY', {}, main, h('ASIDE', {}, h('TABLE', {}, big), groups));

let listener;
const content = vm.createContext({
  console, URL, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
  Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
  HTMLInputElement: class {}, HTMLTextAreaElement: class {},
  KeyboardEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } },
  location: { href: 'https://gallery.test/ui-table' },
  window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
  document: { body, title: 'ui-table',
    getElementById: () => null,
    querySelector: selector => ({ main, tbody, 'tbody tr:nth-child(8)': rows[7], '#big': big, '#groups': groups })[selector] || null,
    querySelectorAll: () => [],
  },
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
  tabState: async tabId => ({ tabId, status: 'complete', url: 'https://gallery.test/ui-table' }),
  isRestrictedUrl: () => false, ensureContentScript: async () => {},
  withTabState: async (tabId, response) => ({ ...response, state: { ...response.state, tabId } }),
  tabsCreated: 0, openedTabs: [], actionStarts: new Map(), tabLoads: new Map(), mainFrameStatus: new Map(),
  userActiveError: async () => null, viewedTabError: async () => null, guardedTabId: async () => null, touchStart() {}, touchEnd() {},
});
vm.runInContext(fs.readFileSync('extension/background/history.js', 'utf8'), background);
vm.runInContext(source.slice(source.indexOf('const POST_SNAPSHOT_ACTIONS'), source.indexOf('async function dispatchCommand')), background);
vm.runInContext(source.slice(source.indexOf('const MISSING_CONTENT_RECEIVER'), source.indexOf('async function getActiveTabId')), background);
background.dispatchCommand = params => background.forwardToContent(params.action, params);
const send = (action, params = {}) => background.forwardToContent(action, { tabId: 1, ...params })
  .then(result => JSON.parse(JSON.stringify(result)));
const command = params => background.handleCommand({ tabId: 1, ...params })
  .then(result => JSON.parse(JSON.stringify(result)));
const MARKER = /^ *- … (\d+) more similar items; snapshot (e\d+) to list all$/m;
const rowLines = snapshot => snapshot.split('\n').filter(line => line.includes('checkbox "'));

(async () => {
  // 1. The full page shows rows 1–5 and a marker that names the rows' container by ref.
  const page = await send('getSnapshot');
  assert.deepEqual(rowLines(page.snapshot).map(line => line.match(/"(.+) 선택"/)[1]), PAGES[0].slice(0, 5).map(([name]) => name));
  const [, omitted, scopeRef] = page.snapshot.match(MARKER);
  assert.equal(omitted, '3');
  assert.doesNotMatch(page.snapshot, /마이크/);
  assert.match(page.snapshot, /- button "1" \[ref=e\d+\]/, 'the marker is followed by the rest of the page');

  // 2. browser_find reads the snapshot without collapse: 마이크 has refs that actions take.
  const found = await send('getSnapshot', { collapse: false });
  assert.doesNotMatch(found.snapshot, /more similar items/);
  assert.equal(rowLines(found.snapshot).length, 8);
  const matches = found.snapshot.split('\n').filter(line => line.includes('마이크'));
  assert.deepEqual(matches.map(line => line.trim().replace(/\[ref=e\d+\]/, '[ref]')),
    ['- checkbox "마이크 선택" [ref] [value="on" checked=false]', '- text "마이크"']);
  const micRef = matches[0].match(/\[ref=(e\d+)\]/)[1];
  assert.equal((await send('checkBox', { selector: micRef, checked: true })).ok, true);
  assert.equal(checkboxes[7].checked, true);
  checkboxes[7].checked = false;

  // 3. A snapshot scoped to the container lists every row: by the marker's ref, or tbody.
  for (const scope of [{ ref: scopeRef }, { selector: 'tbody' }]) {
    const scoped = await send('getSnapshot', scope);
    assert.doesNotMatch(scoped.snapshot, /more similar items/, JSON.stringify(scope));
    assert.equal(rowLines(scoped.snapshot).length, 8, JSON.stringify(scope));
    assert.ok(scoped.snapshot.includes(`checkbox "마이크 선택" [ref=${micRef}]`), 'refs are kept');
    assert.match(scoped.snapshot, /- text "3"/);
    assert.doesNotMatch(scoped.snapshot, /재고 관리|button "1"/, 'only the container');
  }
  // A scope above the container collapses the rows again, with the same marker.
  const above = await send('getSnapshot', { selector: 'main' });
  assert.equal(above.snapshot.match(MARKER)[2], scopeRef);

  // 4. One row's own selector still shows it.
  const row = await send('getSnapshot', { selector: 'tbody tr:nth-child(8)' });
  assert.ok(row.snapshot.includes(`checkbox "마이크 선택" [ref=${micRef}]`));

  // After paging, the delta lists the five visible rows of the new page and keeps the marker.
  const two = page.snapshot.match(/- button "2" \[ref=(e\d+)\]/)[1];
  const paged = await command({ action: 'keypress', ref: two, key: 'Enter' });
  assert.equal(paged.snapshot_delta, true);
  assert.deepEqual(rowLines(paged.snapshot).map(line => line.match(/"(.+) 선택"/)[1]), PAGES[1].slice(0, 5).map(([name]) => name));
  assert.deepEqual(paged.snapshot.match(MARKER).slice(1), ['3', scopeRef]);
  assert.ok(paged.snapshot.trimEnd().split('\n').findIndex(line => MARKER.test(line)) >
    paged.snapshot.split('\n').findIndex(line => line.includes('손목 받침대')), 'the marker follows its rows');
  assert.equal(rowLines((await send('getSnapshot', { ref: scopeRef })).snapshot).at(-1).match(/"(.+) 선택"/)[1], 'HDMI 케이블');
  // A delta that touches no row of the run leaves its marker out.
  const unchanged = await command({ action: 'keypress', ref: two, key: 'Escape' });
  assert.equal(unchanged.snapshot_delta, true);
  assert.doesNotMatch(unchanged.snapshot, /more similar items/);
  // A row added past the collapse is new since the delta's baseline: the delta lists it (#350),
  // and the run's marker, whose hidden count is unchanged, stays out.
  const add = page.snapshot.match(/- button "행 추가" \[ref=(e\d+)\]/)[1];
  const added = await command({ action: 'keypress', ref: add, key: 'Enter' });
  assert.equal(added.snapshot_delta, true);
  assert.equal(rowLines(added.snapshot).length, 1);
  assert.match(rowLines(added.snapshot)[0], /checkbox "추가 상품 선택" \[ref=e\d+\]/);
  assert.doesNotMatch(added.snapshot, /more similar items/);

  // A scoped snapshot lists at most SCOPED_ITEM_LIMIT (100) items of its own; browser_find
  // (collapse: false) still reaches the rest.
  const ROW = /- text "행 (\d+)"/g;
  const bigRef = page.snapshot.split('\n').filter(line => MARKER.test(line)).map(line => line.match(MARKER))
    .find(([, count]) => count === '245')[2];
  for (const scope of [{ ref: bigRef }, { selector: '#big' }]) {
    const capped = await send('getSnapshot', scope);
    assert.deepEqual([...capped.snapshot.matchAll(ROW)].map(([, n]) => Number(n)),
      Array.from({ length: 100 }, (_, i) => i + 1), JSON.stringify(scope));
    assert.match(capped.snapshot, /^ *- … 150 more similar items; use browser_find to locate a specific one$/m);
    assert.doesNotMatch(capped.snapshot, /to list all/);
  }
  const everything = await send('getSnapshot', { collapse: false });
  assert.equal([...everything.snapshot.matchAll(ROW)].length, 250);
  assert.match(everything.snapshot, /- text "행 250"/);

  // A scope lists all seven of its own groups; each group's run of seven items still collapses.
  const scopedGroups = await send('getSnapshot', { selector: '#groups' });
  assert.equal(scopedGroups.snapshot.match(/heading "그룹 \d+"/g).length, 7);
  const nested = scopedGroups.snapshot.split('\n').filter(line => MARKER.test(line));
  assert.equal(nested.length, 7);
  assert.ok(nested.every(line => line.match(MARKER)[1] === '2'));
  assert.doesNotMatch(scopedGroups.snapshot, /항목 \d-6/);
  assert.equal(new Set(nested.map(line => line.match(MARKER)[2])).size, 7, 'each run names its own list');

  console.log('Collapsed item checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
