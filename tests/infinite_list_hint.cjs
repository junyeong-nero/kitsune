// Run with: node tests/infinite_list_hint.cjs (no browser or npm dependencies).
// #486: an action's delta that shows similar items appended at the end of a list that ends in a
// loading indicator names browser_find's load_more in snapshot_hint, so an agent does not load
// the list one hover per batch. The first page follows the gallery's ui-infinite: 10 more rows
// each time its sentinel scrolls into view, and a sentinel that says so until every row is loaded.
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
    this.append(...nodes);
  }
  // Adds nodes at the end, or before the child `before`.
  append(...nodes) { this.insert(nodes); }
  insert(nodes, before = null) {
    const children = nodes.map(node => typeof node === 'string' ? { nodeType: 3, textContent: node } : node);
    children.forEach(child => { child.parentElement = this; });
    this.childNodes.splice(before ? this.childNodes.indexOf(before) : this.childNodes.length, 0, ...children);
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
  getClientRects() { return [{}]; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
  scrollIntoView() { this.onScrollIntoView?.(); }
  focus() {}
  click() {}
}
const h = (tagName, attrs, ...nodes) => new Element(tagName, attrs, nodes);
const button = (label, onClick = () => {}, attrs = {}) => {
  const el = new Element('BUTTON', attrs, [label], { type: 'button', value: '' });
  el.addEventListener('click', onClick);
  return el;
};
// A ui-infinite row: `<li>name <button aria-label="name 저장">저장</button></li>`.
const row = name => h('LI', {}, `${name} `, button('저장', () => {}, { 'aria-label': `${name} 저장` }));
const rows = (label, from, count) => Array.from({ length: count }, (_, i) => row(`${label} #${from + i}`));
// A button that appends the next `count` rows to `list`, before its child `before` if given.
function grower(label, list, count = 10, before = null, make = rows) {
  let next = list.children.length + (before ? 0 : 1);
  return button(`${label} 더하기`, () => {
    list.insert(make(label, next, count), before);
    next += count;
  });
}

// 1. ui-infinite: hovering the last row's button scrolls the sentinel into view, which loads 10
// more rows; after the last batch the sentinel no longer says it is loading.
const NAMES = Array.from({ length: 40 }, (_, i) => `계약서 #${i + 1}`);
const documents = h('UL', {});
const sentinel = h('DIV', { class: 'sentinel' }, '불러오는 중…');
function loadDocuments() {
  const shown = documents.children.length;
  const batch = NAMES.slice(shown, shown + 10).map(row);
  for (const item of batch) {
    item.children[0].onScrollIntoView = () => { if (item === documents.children.at(-1)) loadDocuments(); };
  }
  documents.append(...batch);
  sentinel.childNodes[0].textContent = shown + 10 >= NAMES.length ? '모든 항목을 불러왔습니다.' : '불러오는 중…';
}
loadDocuments();
const still = button('아무 일 없음');

// 2. The indicator is the list's own last child, a row unlike the others.
const loaderRow = h('LI', {}, 'Loading…');
const mail = h('UL', {}, ...rows('메일', 1, 6), loaderRow);
const moreMail = grower('메일', mail, 10, loaderRow);
// The minimum qualifying batch: one old matching sibling, two appended items, ASCII ellipsis.
const minimal = h('UL', {}, ...rows('최소', 1, 1));
const moreMinimal = grower('최소', minimal, 2);
const boundaryIndicator = 'x'.repeat(37) + '...'; // Exactly 40 normalized characters.

// 3. An indicator without text, which a snapshot does not list: a progress bar or a busy element.
const SPINNERS = [h('DIV', { role: 'progressbar' }), h('DIV', { 'aria-busy': 'true' }), h('PROGRESS', {})];
const spun = SPINNERS.map((spinner, i) => {
  const list = h('UL', {}, ...rows(`작업${i}`, 1, 6));
  return { list, spinner, more: grower(`작업${i}`, list) };
});

// Lists that grow without a hint.
// Ordinary appended content: one more row, also above a status line that ends in an ellipsis.
const todos = h('UL', {}, ...rows('할 일', 1, 6));
const moreTodos = grower('할 일', todos, 1);
// A list followed by ordinary text.
const plain = h('UL', {}, ...rows('메모', 1, 6));
const morePlain = grower('메모', plain);
// A list with a sentinel that shows nothing.
const quiet = h('UL', {}, ...rows('알림', 1, 6));
const moreQuiet = grower('알림', quiet);
// Growth that is not a list's: two blocks unlike the one before them, above a status line.
const record = h('DIV', {}, h('SECTION', {}, h('H3', {}, '첫 기록'), h('P', {}, '내용')));
const moreRecord = button('기록 더하기', () => record.append(
  h('DIV', {}, h('H3', {}, '상세'), button('닫기')), h('DIV', {}, h('H3', {}, '이력'), button('접기'))));
// The last row's own text is cut with an ellipsis: it is an item, not an indicator.
const cutTitle = (label, from, count) => Array.from({ length: count }, (_, i) => h('LI', {}, `${label} ${from + i} 긴 제목…`));
const titles = h('UL', {}, ...cutTitle('글', 1, 6));
const moreTitles = grower('글', titles, 10, null, cutTitle);
// So is a last row shaped like the rows added before it.
const lastNote = h('LI', {}, '마지막 노트 긴 제목…');
const notes = h('UL', {}, ...cutTitle('노트', 1, 5), lastNote);
const moreNotes = grower('노트', notes, 10, lastNote, cutTitle);
// A control loads more: scrolling to the end would not press it.
const paged = h('UL', {}, ...rows('주문', 1, 6));
const loadMore = button('더 보기…', () => paged.append(...rows('주문', paged.children.length + 1, 10)));
// Every row is replaced: nothing of the list the agent saw remains.
const results = h('UL', {}, ...rows('결과', 1, 6));
const replace = button('결과 다시 검색', () => {
  results.children.forEach(item => item.remove());
  results.append(...rows('새 결과', 1, 10));
});
// A sentence that ends in an ellipsis is not a loading indicator.
const archive = h('UL', {}, ...rows('보관', 1, 6));
const moreArchive = grower('보관', archive);
// A fresh list has no matching old sibling; prepends and middle insertions are not trailing growth.
const fresh = h('UL', {});
const moreFresh = grower('처음', fresh);
const earlier = h('UL', {}, ...rows('앞쪽', 1, 6));
const moreEarlier = grower('앞쪽', earlier, 2, earlier.children[0]);
const middle = h('UL', {}, ...rows('중간', 1, 6));
const moreMiddle = grower('중간', middle, 2, middle.children[3]);
// An indicator's descendant control also disqualifies it, even when the wrapper is aria-busy.
const controlled = h('UL', {}, ...rows('수동', 1, 6));
const moreControlled = grower('수동', controlled);
const controlledIndicator = h('DIV', { 'aria-busy': 'true' }, button('Load more...'));
// 4. A delta too large for its budget keeps the truncation hint.
const longRows = (label, from, count) => rows(`${label} ${'아주 긴 문서 이름 '.repeat(25)}`, from, count);
const long = h('UL', {}, ...longRows('장문', 1, 6));
const moreLong = grower('장문', long, 20, null, longRows);

const document = { body: h('BODY', {}, h('MAIN', {}, h('H1', {}, '문서 목록'),
  h('DIV', { class: 'infinite' }, documents, sentinel), still,
  mail, moreMail,
  h('DIV', {}, minimal, h('P', {}, boundaryIndicator)), moreMinimal,
  ...spun.flatMap(({ list, spinner, more }) => [h('DIV', {}, list, spinner), more]),
  h('DIV', {}, todos, h('P', {}, '저장 중…')), moreTodos,
  h('DIV', {}, plain, h('P', {}, '메모 목록의 끝')), morePlain,
  h('DIV', {}, quiet, h('DIV', { class: 'sentinel' })), moreQuiet,
  h('DIV', {}, record, h('P', {}, '저장 중…')), moreRecord,
  h('DIV', {}, titles), moreTitles,
  h('DIV', {}, notes), moreNotes,
  h('DIV', {}, paged, loadMore),
  h('DIV', {}, results, h('P', {}, '불러오는 중…')), replace,
  h('DIV', {}, archive, h('P', {}, '최근 30일 동안 등록된 문서만 보여 주며, 더 오래된 문서는 보관함에서 찾을 수 있습니다…')), moreArchive,
  h('DIV', {}, fresh, h('P', {}, 'Loading...')), moreFresh,
  h('DIV', {}, earlier, h('P', {}, 'Loading...')), moreEarlier,
  h('DIV', {}, middle, h('P', {}, 'Loading...')), moreMiddle,
  h('DIV', {}, controlled, controlledIndicator), moreControlled,
  h('DIV', {}, long, h('P', {}, '불러오는 중…')), moreLong)),
  title: 'gallery', getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };

let listener;
const content = vm.createContext({
  console, URL, crypto: webcrypto, CSS: { escape: x => x }, setTimeout, queueMicrotask,
  Event, MouseEvent, navigator: { platform: 'Linux x86_64' },
  HTMLInputElement: class {}, HTMLTextAreaElement: class {},
  location: { href: 'https://gallery.test/ui-infinite' },
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
  tabState: async tabId => ({ tabId, status: 'complete', url: 'https://gallery.test/ui-infinite' }),
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
const HINT = 'This list loads more as it scrolls; browser_find text=… load_more=true scrolls until a match appears.';

(async () => {
  // Every row's ref, as browser_find reads them (a snapshot collapses a long run).
  const page = (await send('getSnapshot', { collapse: false })).snapshot;
  const click = label => command({ action: 'click', selector: refOf(page, `button "${label}"`) });

  // 1. ui-infinite: each hover that loads a batch says how to load until a match, as long as the
  // sentinel says more is loading.
  const first = await command({ action: 'hover', selector: refOf(page, 'button "계약서 #10 저장"') });
  assert.equal(first.ok, true, JSON.stringify(first));
  assert.equal(first.snapshot_delta, true);
  assert.equal(documents.children.length, 20);
  assert.match(first.snapshot, /button "계약서 #20 저장"/);
  assert.doesNotMatch(first.snapshot, /계약서 #10 저장/, 'only what changed');
  assert.equal(first.snapshot_hint, HINT);
  // The hint costs its text, its key and a comma in the result's JSON.
  const { snapshot_hint: hint, ...withoutHint } = first;
  assert.equal(hint.length, 101);
  assert.equal(JSON.stringify(first).length - JSON.stringify(withoutHint).length, 120);
  const second = await command({ action: 'hover', selector: refOf(first.snapshot, 'button "계약서 #20 저장"') });
  assert.match(second.snapshot, /button "계약서 #30 저장"/);
  assert.equal(second.snapshot_hint, HINT);
  // Only a delta shows growth: a snapshot of the grown list has no hint.
  const full = await send('getSnapshot');
  assert.equal('hint' in full, false);
  assert.equal('snapshot_hint' in full, false);
  assert.equal('delta' in full, false);
  assert.match(full.snapshot, /계약서 #1 저장/);
  // An action that loads nothing has no hint.
  const nothing = await click('아무 일 없음');
  assert.equal(nothing.no_visible_change, true, JSON.stringify(nothing));
  assert.equal('snapshot_hint' in nothing, false, JSON.stringify(nothing));
  // The last batch: the sentinel reads "모든 항목을 불러왔습니다.", so nothing more would load.
  const last = await command({ action: 'hover', selector: refOf(second.snapshot, 'button "계약서 #30 저장"') });
  assert.match(last.snapshot, /button "계약서 #40 저장"/);
  assert.match(last.snapshot, /모든 항목을 불러왔습니다\./);
  assert.equal('snapshot_hint' in last, false, JSON.stringify(last));

  // 2. The indicator as the list's own last row.
  const mailed = await click('메일 더하기');
  assert.match(mailed.snapshot, /button "메일 #16 저장"/);
  assert.equal(mailed.snapshot_hint, HINT);
  const minimallyGrown = await click('최소 더하기');
  assert.match(minimallyGrown.snapshot, /button "최소 #3 저장"/);
  assert.equal(minimallyGrown.snapshot_hint, HINT);

  // 3. A progress bar or a busy element without text.
  for (const [i, { spinner }] of spun.entries()) {
    const grown = await click(`작업${i} 더하기`);
    assert.match(grown.snapshot, new RegExp(`button "작업${i} #16 저장"`));
    assert.equal(grown.snapshot_hint, HINT, spinner.tagName + JSON.stringify(spinner.attrs));
  }

  // No hint: growth that is not a list loading more as it scrolls.
  for (const [label, shown, why] of [
    ['할 일 더하기', /button "할 일 #7 저장"/, 'one row added'],
    ['메모 더하기', /button "메모 #16 저장"/, 'a list followed by ordinary text'],
    ['알림 더하기', /button "알림 #16 저장"/, 'a sentinel that shows nothing'],
    ['기록 더하기', /heading "이력"/, 'elements unlike the sibling before them, above a status line'],
    ['글 더하기', /글 16 긴 제목…/, 'rows whose own text ends in an ellipsis'],
    ['노트 더하기', /노트 15 긴 제목…/, 'rows added before a last row of their shape that ends in an ellipsis'],
    ['더 보기…', /button "주문 #16 저장"/, 'a button loads more'],
    ['결과 다시 검색', /button "새 결과 #10 저장"/, 'every row was replaced'],
    ['보관 더하기', /button "보관 #16 저장"/, 'a sentence that ends in an ellipsis'],
    ['처음 더하기', /button "처음 #10 저장"/, 'a fresh list without old matching siblings'],
    ['앞쪽 더하기', /button "앞쪽 #7 저장"/, 'prepended items'],
    ['중간 더하기', /button "중간 #7 저장"/, 'items inserted before an old trailing sibling'],
    ['수동 더하기', /button "수동 #16 저장"/, 'a busy indicator containing an interactive control'],
  ]) {
    const grown = await click(label);
    assert.equal(grown.snapshot_delta, true, why);
    assert.match(grown.snapshot, shown, why);
    assert.equal('snapshot_hint' in grown, false, `${why}: ${grown.snapshot_hint}`);
  }

  // 4. A truncated delta keeps the truncation hint: the rows it lists may not be the batch's last.
  const cut = await click('장문 더하기');
  assert.equal(long.children.length, 26);
  assert.equal(cut.snapshot_truncated, true, JSON.stringify(cut).length);
  assert.match(cut.snapshot_hint, /^Snapshot truncated; /);
  assert.doesNotMatch(cut.snapshot_hint, /load_more/);

  console.log('Infinite list hint checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
