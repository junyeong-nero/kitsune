// Run with: node tests/repeated_snapshot.cjs (no browser or npm dependencies).
// #384: a whole-page snapshot (skipUnchanged) that reads exactly the snapshot the tab's last
// action result carried in full, in the same document at the same URL, answers unchanged_since.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('extension/background/background.js', 'utf8');
const PAGE = '- heading "Orders" [ref=e12] [level=1]\n- list [ref=e13]\n  - link "Order 1" [ref=e20]';

// pages: tabId -> { url, action (an action's compact snapshot), read (a readable one: a getSnapshot's, or
// a navigation's that fits its budget, #401), truncated }.
function harness(pages = { 7: {}, 8: {} }) {
  for (const page of Object.values(pages)) {
    Object.assign(page, { url: 'https://shop.test/orders', action: PAGE, read: PAGE, ...page });
  }
  const reads = [];
  const context = vm.createContext({
    setTimeout, clearTimeout,
    getActiveTabId: async () => 7,
    tabsCreated: 0, openedTabs: [], actionStarts: new Map(), tabLoads: new Map([[7, { complete: true }], [8, { complete: true }]]),
    mainFrameStatus: new Map(),
    userActiveError: async () => null, guardedTabId: async () => null, touchStart() {}, touchEnd() {},
    getOpenedTabs: async () => ({ opened_tabs: [] }),
    takeOpenedPopup: async () => null,
    expandShortRefs: params => ({ params }), targetFrame: () => 0,
    dispatchCommand: async params => {
      // A link click loads another document (#349).
      if (params.action === 'click' && params.selector === 'a') context.tabLoads.set(params.tabId, { complete: true });
      // A new tab is tab 8; a back loads another document unless backLoads is false (#448).
      if (params.action === 'newTab') return { ok: true, tabId: 8, url: pages[8].url, status: 'complete', ...context.newTabResult };
      if (params.action === 'goBack') {
        if (context.backLoads !== false) context.tabLoads.set(params.tabId, { complete: true });
        return { ok: true, tabId: params.tabId };
      }
      if (params.action !== 'getSnapshot') return { ok: true };
      reads.push(params);
      const page = pages[params.tabId];
      return { snapshot: page.read, url: page.url, title: 'Orders', readyState: 'complete' };
    },
    // A post-action snapshot: a delta unless the action loads a page (navigate) or asks for a full one.
    forwardToContent: async (action, params) => {
      const page = pages[params.tabId];
      context.postActionReads.push(params);
      if (params.delta) return { snapshot: '- button "Go" [ref=e30]', delta: true };
      if (params.text === true) {
        return { snapshot: page.read, ...(page.truncated || page.read.length > params.maxChars ? { truncated: true } : {}) };
      }
      return { snapshot: page.action, ...(page.truncated ? { truncated: true } : {}) };
    },
    postActionReads: [],
    browser: { tabs: { get: async id => ({ id, status: 'complete', url: pages[id].url, title: 'Orders' }),
      sendMessage: async () => ({ watching: false }) } },
  });
  vm.runInContext(source.slice(source.indexOf('const POST_SNAPSHOT_ACTIONS'), source.indexOf('async function dispatchCommand')), context);
  context.tabWriteError = async () => null; // Permission policy is tested in user_tabs_read_only.cjs.
  context.handoffs = new Map();
  context.handoffTransportEpoch = 0;
  const snapshot = (params = {}) => context.handleCommand({ action: 'getSnapshot', maxChars: 12000, skipUnchanged: true, ...params });
  return { context, pages, reads, snapshot };
}

async function testASnapshotAfterAFullActionResultIsUnchanged() {
  const h = harness();
  const navigated = await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
  assert.equal(navigated.snapshot, PAGE);
  assert.equal(navigated.snapshot_delta, undefined);
  const repeated = await h.snapshot({ tabId: 7 });
  assert.deepEqual({ ...repeated }, { snapshot: 'unchanged since the browser_navigate result (refs e12–e20)',
    unchanged_since: 'browser_navigate', url: 'https://shop.test/orders', title: 'Orders', readyState: 'complete' });
  assert.equal(h.reads.length, 1, 'the page is still read');
  // Only the first snapshot after the action: the next one returns the page.
  assert.equal((await h.snapshot({ tabId: 7 })).snapshot, PAGE);
  // A click whose result is full (snapshot: 'full'), on the active tab when the snapshot names none.
  await h.context.handleCommand({ action: 'click', selector: '#a', snapshot: 'full', tabId: 7 });
  assert.equal((await h.snapshot()).unchanged_since, 'browser_click');
  // A click that loaded another page returns it whole (#349), the case batched snapshots repeat.
  const loaded = await h.context.handleCommand({ action: 'click', selector: 'a', tabId: 7 });
  assert.equal(loaded.navigated.url, 'https://shop.test/orders');
  assert.equal(loaded.snapshot_delta, undefined);
  assert.equal((await h.snapshot({ tabId: 7 })).snapshot, 'unchanged since the browser_click result (refs e12–e20)');
}

// #401, #400: a page a navigation loaded that fits NAVIGATION_TEXT_LIMIT is snapshotted with its text
// lines, so the readable snapshot that follows is identical and answers unchanged_since.
async function testANavigationCarriesTheTextOfASmallPage() {
  const notice = '- heading "Notice" [ref=e9] [level=1]\n- text "Open 13:00–18:00 on 2027-06-12."';
  const h = harness({ 7: { action: '- heading "Notice" [ref=e9] [level=1]', read: notice } });
  const navigated = await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
  assert.equal(navigated.snapshot, notice);
  assert.equal(h.context.postActionReads.length, 1, 'one read when the page fits');
  assert.equal((await h.snapshot({ tabId: 7 })).unchanged_since, 'browser_navigate');
  // A click that loaded the page (navigated).
  const clicked = await h.context.handleCommand({ action: 'click', selector: 'a', tabId: 7 });
  assert.equal(clicked.snapshot, notice);
  assert.equal((await h.snapshot({ tabId: 7 })).snapshot, 'unchanged since the browser_click result (refs e9)');
  // A delta stays compact.
  assert.equal((await h.context.handleCommand({ action: 'click', selector: '#a', tabId: 7 })).snapshot_delta, true);
  assert.equal(h.context.postActionReads.at(-1).text, undefined);
  // So does a page past the budget, and a full snapshot an action asked for.
  h.pages[7].read = notice + `\n- text "${'x'.repeat(2000)}"`;
  h.context.postActionReads.length = 0;
  assert.equal((await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 })).snapshot,
    h.pages[7].action);
  assert.deepEqual(h.context.postActionReads.map(read => [read.text, read.maxChars]), [[true, 2000], [undefined, 4000]]);
  assert.equal((await h.context.handleCommand({ action: 'click', selector: '#a', snapshot: 'full', tabId: 7 })).snapshot,
    h.pages[7].action);
}

// #448: the core browser_tabs action=new url= (newTab with wait) and browser_navigate_back send pageText,
// and their result carries a small page's text as browser_navigate's does, under their own tool's name.
async function testTabsNewAndBackCarryTheTextOfASmallPage() {
  const notice = '- heading "Notice" [ref=e9] [level=1]\n- text "Open 13:00–18:00 on 2027-06-12."';
  const compact = '- heading "Notice" [ref=e9] [level=1]';
  const h = harness({ 7: { action: compact, read: notice }, 8: { action: compact, read: notice } });
  const a = { ownerId: 'claude-101' };
  const opened = await h.context.handleCommand({ action: 'newTab', url: 'https://shop.test/orders', wait: true, pageText: true, ...a });
  assert.deepEqual({ ...opened }, { ok: true, tabId: 8, url: 'https://shop.test/orders', status: 'complete', snapshot: notice });
  assert.deepEqual(h.context.postActionReads.map(read => [read.tabId, read.text, read.maxChars, read.observation]),
    [[8, true, 2000, 'browser_tabs']]);
  assert.equal((await h.snapshot({ tabId: 8 })).snapshot, notice, 'another caller gets the page');
  assert.deepEqual({ ...await h.snapshot({ tabId: 8, ...a }) }, { snapshot: 'unchanged since the browser_tabs result (refs e9)',
    unchanged_since: 'browser_tabs', url: 'https://shop.test/orders', title: 'Orders', readyState: 'complete' });
  const back = await h.context.handleCommand({ action: 'goBack', tabId: 7, pageText: true, ...a });
  assert.deepEqual({ ...back }, { ok: true, tabId: 7, snapshot: notice });
  assert.equal((await h.snapshot({ tabId: 7, ...a })).snapshot, 'unchanged since the browser_navigate_back result (refs e9)');
  // A back that starts no load (no history, a same-document entry) reads the page as it is, after
  // ACTION_NAVIGATION_START_MS.
  h.context.backLoads = false;
  const started = Date.now();
  assert.equal((await h.context.handleCommand({ action: 'goBack', tabId: 7, pageText: true })).snapshot, notice);
  assert.ok(Date.now() - started >= 1000, 'waited for a load to start');
  assert.equal((await h.snapshot({ tabId: 7 })).unchanged_since, 'browser_navigate_back');
}

// Without pageText, without a load wait, for a page past NAVIGATION_TEXT_LIMIT, for a back still loading,
// or with post-action snapshots off, the result is as before #448, and nothing answers unchanged_since.
async function testTabsNewAndBackOtherwiseKeepTheirResult() {
  const h = harness();
  const asBefore = async (params, expected, label) => {
    // A back drops the record of the result before it, as any page action does.
    if (params.action === 'goBack') await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
    h.context.postActionReads.length = 0;
    assert.deepEqual({ ...await h.context.handleCommand(params) }, expected, label);
    if (!label.startsWith('too large')) assert.equal(h.context.postActionReads.length, 0, label);
    const next = await h.snapshot({ tabId: params.tabId ?? 8 });
    assert.equal(next.snapshot, h.pages[params.tabId ?? 8].read, label);
    assert.equal(next.unchanged_since, undefined, label);
  };
  const opened = { ok: true, tabId: 8, url: 'https://shop.test/orders', status: 'complete' };
  const newTab = { action: 'newTab', url: 'https://shop.test/orders', wait: true };
  await asBefore({ ...newTab }, opened, 'newTab without pageText');
  await asBefore({ ...newTab, wait: false, pageText: true }, opened, 'newTab without wait');
  await asBefore({ ...newTab, pageText: true, includeSnapshot: false }, opened, 'newTab, snapshots off');
  h.context.newTabResult = { ok: false, timedOut: true };
  await asBefore({ ...newTab, pageText: true }, { ...opened, ok: false, timedOut: true }, 'newTab timed out');
  delete h.context.newTabResult;
  h.context.backLoads = false;
  const started = Date.now();
  await asBefore({ action: 'goBack', tabId: 7 }, { ok: true, tabId: 7 }, 'goBack without pageText');
  assert.ok(Date.now() - started < 500, 'a back without pageText does not wait for a load');
  await asBefore({ action: 'goBack', tabId: 7, pageText: true, includeSnapshot: false }, { ok: true, tabId: 7 },
    'goBack, snapshots off');
  h.context.backLoads = true;
  h.pages[7].read = PAGE + `\n- text "${'x'.repeat(2000)}"`;
  h.pages[8].read = h.pages[7].read;
  await asBefore({ ...newTab, pageText: true }, opened, 'too large newTab');
  assert.deepEqual(h.context.postActionReads.map(read => [read.text, read.maxChars]), [[true, 2000]]);
  await asBefore({ action: 'goBack', tabId: 7, pageText: true }, { ok: true, tabId: 7 }, 'too large goBack');
  assert.deepEqual(h.context.postActionReads.map(read => [read.text, read.maxChars]), [[true, 2000]]);
  h.pages[7].read = h.pages[8].read = PAGE;
  const wait = h.context.waitForActionNavigation;
  h.context.waitForActionNavigation = async () => 'pending';
  await asBefore({ action: 'goBack', tabId: 7, pageText: true }, { ok: true, tabId: 7 }, 'goBack still loading');
  h.context.waitForActionNavigation = wait;
}

async function testOnlyAfterAFullUntruncatedResult() {
  const h = harness();
  // A delta-only result.
  await h.context.handleCommand({ action: 'click', selector: '#a', tabId: 7 });
  assert.equal((await h.snapshot({ tabId: 7 })).snapshot, PAGE);
  // A delta after a full result replaces it.
  await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
  await h.context.handleCommand({ action: 'click', selector: '#a', tabId: 7 });
  assert.equal((await h.snapshot({ tabId: 7 })).snapshot, PAGE);
  // So does a page action without a snapshot.
  await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
  await h.context.handleCommand({ action: 'scroll', direction: 'down', tabId: 7 });
  assert.equal((await h.snapshot({ tabId: 7 })).snapshot, PAGE);
  // A truncated full result showed part of the page.
  h.pages[7].truncated = true;
  await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
  assert.equal((await h.snapshot({ tabId: 7 })).snapshot, PAGE);
}

async function testOnlyTheSameTabDocumentAndText() {
  const h = harness();
  // Another tab.
  await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
  assert.equal((await h.snapshot({ tabId: 8 })).snapshot, PAGE);
  assert.equal((await h.snapshot({ tabId: 7 })).unchanged_since, 'browser_navigate');
  // Text the action result did not show (a readable snapshot's prose, or a change since).
  await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
  h.pages[7].read = PAGE + '\n- text "Shipped today"';
  assert.equal((await h.snapshot({ tabId: 7 })).snapshot, h.pages[7].read);
  h.pages[7].read = PAGE;
  // Another document with the same text.
  await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
  h.context.tabLoads.set(7, { complete: true });
  assert.equal((await h.snapshot({ tabId: 7 })).snapshot, PAGE);
  // Another URL in the same document (history.pushState).
  await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
  h.pages[7].url = 'https://shop.test/orders?page=2';
  assert.equal((await h.snapshot({ tabId: 7 })).snapshot, PAGE);
}

// #442: only the caller whose action result carried the snapshot saw it; another client, or another
// handoff session of the same client, gets the page, and leaves the record to that caller.
async function testOnlyTheCallerThatGotTheActionResult() {
  const h = harness();
  const a = { ownerId: 'claude-101', sessionId: 'a'.repeat(64) };
  await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7, ...a });
  for (const caller of [{}, { ownerId: 'codex-202' }, { ownerId: 'claude-101' }, { ...a, sessionId: 'b'.repeat(64) }]) {
    assert.equal((await h.snapshot({ tabId: 7, ...caller })).snapshot, PAGE, JSON.stringify(caller));
  }
  assert.equal((await h.snapshot({ tabId: 7, ...a })).unchanged_since, 'browser_navigate');
  // A caller without an owner id (an HTTP client) is one caller too.
  await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
  assert.equal((await h.snapshot({ tabId: 7, ...a })).snapshot, PAGE);
  assert.equal((await h.snapshot({ tabId: 7 })).unchanged_since, 'browser_navigate');
}

async function testScopedSnapshotsReturnThePage() {
  const h = harness();
  await h.context.handleCommand({ action: 'navigate', url: 'https://shop.test/orders', tabId: 7 });
  for (const params of [{ skipUnchanged: undefined }, { ref: 'e13' }, { selector: 'main' }, { maxDepth: 1 },
    { boxes: true }, { collapse: false }, { sinceObservation: true }]) {
    assert.equal((await h.snapshot({ tabId: 7, ...params })).snapshot, PAGE, JSON.stringify(params));
  }
  // They leave the whole-page snapshot's answer as it was.
  assert.equal((await h.snapshot({ tabId: 7 })).unchanged_since, 'browser_navigate');
}

(async () => {
  await testASnapshotAfterAFullActionResultIsUnchanged();
  await testANavigationCarriesTheTextOfASmallPage();
  await testTabsNewAndBackCarryTheTextOfASmallPage();
  await testTabsNewAndBackOtherwiseKeepTheirResult();
  await testOnlyAfterAFullUntruncatedResult();
  await testOnlyTheSameTabDocumentAndText();
  await testOnlyTheCallerThatGotTheActionResult();
  await testScopedSnapshotsReturnThePage();
  console.log('repeated_snapshot: ok');
})().catch(e => { console.error(e); process.exit(1); });
