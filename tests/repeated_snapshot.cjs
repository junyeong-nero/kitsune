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
    userActiveError: async () => null, viewedTabError: async () => null, guardedTabId: async () => null, touchStart() {}, touchEnd() {},
    protectedTabError: async () => null,
    getOpenedTabs: async () => ({ opened_tabs: [] }),
    takeOpenedPopup: async () => null,
    expandShortRefs: params => ({ params }), targetFrame: () => 0,
    dispatchCommand: async params => {
      // A link click loads another document (#349).
      if (params.action === 'click' && params.selector === 'a') context.tabLoads.set(params.tabId, { complete: true });
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
  await testOnlyAfterAFullUntruncatedResult();
  await testOnlyTheSameTabDocumentAndText();
  await testScopedSnapshotsReturnThePage();
  console.log('repeated_snapshot: ok');
})().catch(e => { console.error(e); process.exit(1); });
