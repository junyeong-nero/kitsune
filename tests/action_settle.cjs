// Run with: node tests/action_settle.cjs (no browser or npm dependencies).
// #348: an action's delta snapshot waits for the page's reaction. At least SETTLE_MIN_MS after
// the action, until the page has been quiet for SETTLE_QUIET_MS and the requests the action
// started are done, capped. Real time, scaled by the background's own constants.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('extension/background/background.js', 'utf8');
const constant = name => Number(new RegExp(`const ${name} = (\\d+);`).exec(source)[1]);
const MIN = constant('SETTLE_MIN_MS');
const HIDDEN_MIN = constant('SETTLE_HIDDEN_MIN_MS');
const QUIET = constant('SETTLE_QUIET_MS');
const CAP = constant('SETTLE_CAP_MS');
const REQUEST_CAP = constant('SETTLE_REQUEST_CAP_MS');

// A page whose last change is given by mutatesUntil (ms after the action, Infinity: never quiet),
// and a request the action starts that ends after requestMs.
function harness({ mutatesUntil = 0, requestMs = null, watching = true, unchanged = false, hidden = false } = {}) {
  let actionAt = null;
  const sent = [];
  const context = vm.createContext({
    setTimeout, clearTimeout,
    getActiveTabId: async () => 7,
    tabsCreated: 0, openedTabs: [], actionStarts: new Map(),
    userActiveError: async () => null, guardedTabId: async () => null, touchStart() {}, touchEnd() {},
    protectedTabError: async () => null,
    getOpenedTabs: async () => ({ opened_tabs: [] }),
    takeOpenedPopup: async () => null,
    // The top frame (#343): no ref here belongs to a cross-origin frame.
    expandShortRefs: params => ({ params }), targetFrame: () => 0,
    dispatchCommand: async params => {
      sent.push({ action: params.action, deltaBaseline: params.deltaBaseline, settleWatch: params.settleWatch });
      actionAt = Date.now();
      if (requestMs != null) {
        context.settleRequests.get(7)?.add('r1');
        setTimeout(() => context.settleRequests.get(7)?.delete('r1'), requestMs);
      }
      return { ok: true };
    },
    forwardToContent: async (action, params) => {
      sent.push({ action, at: Date.now() - actionAt, delta: params.delta });
      return { snapshot: '- button "Go" [ref=s1e1]', delta: true, ...(unchanged ? { unchanged: true } : {}) };
    },
    browser: {
      tabs: {
        sendMessage: async (tabId, msg, options) => {
          assert.equal(msg.action, 'settleState');
          assert.equal(options.frameId, 0);
          sent.push({ action: 'settleState' });
          if (!watching) return { watching: false };
          const elapsedMs = Date.now() - actionAt;
          const last = Math.min(elapsedMs, mutatesUntil);
          return { watching: true, elapsedMs, quietMs: elapsedMs - last, mutations: 1, hidden };
        },
      },
    },
  });
  vm.runInContext(source.slice(source.indexOf('const POST_SNAPSHOT_ACTIONS'), source.indexOf('async function dispatchCommand')) +
    '\nthis.settleRequests = settleRequests;', context);
  return { context, sent, snapshotAt: () => sent.find(m => m.action === 'getSnapshot')?.at };
}

const within = (value, low, high, what) => assert.ok(value >= low && value <= high, `${what}: ${value} not in [${low}, ${high}]`);

async function testQuietPageWaitsTheMinimum() {
  const h = harness();
  const result = await h.context.handleCommand({ action: 'click', selector: '#go', tabId: 7 });
  assert.equal(result.snapshot_delta, true);
  assert.equal(h.sent[0].deltaBaseline, true);
  within(h.snapshotAt(), MIN, MIN + 150, 'quiet page');
}

async function testHiddenTabsWaitForThrottledTimers() {
  // Firefox delays a hidden tab's 250 ms timer to about 1 s.
  const h = harness({ hidden: true });
  await h.context.handleCommand({ action: 'click', selector: '#go', tabId: 7 });
  within(h.snapshotAt(), HIDDEN_MIN, HIDDEN_MIN + 150, 'hidden tab');
}

async function testLateChangeIsWaitedFor() {
  // A debounced reaction that renders after the minimum: the snapshot follows it by QUIET.
  const h = harness({ mutatesUntil: MIN + 100 });
  await h.context.handleCommand({ action: 'type', selector: '#q', text: 'Al', tabId: 7 });
  within(h.snapshotAt(), MIN + 100 + QUIET, MIN + 100 + QUIET + 150, 'late change');
}

async function testRequestsTheActionStartedAreWaitedFor() {
  const h = harness({ requestMs: CAP + 300 });
  await h.context.handleCommand({ action: 'click', selector: '#save', tabId: 7 });
  within(h.snapshotAt(), CAP + 300, CAP + 300 + 150, 'pending request');
  assert.equal(h.context.settleRequests.has(7), false, 'the request watch ends with the action');
}

async function testCaps() {
  const busy = harness({ mutatesUntil: Infinity });
  await busy.context.handleCommand({ action: 'click', selector: '#go', tabId: 7 });
  within(busy.snapshotAt(), CAP, CAP + 150, 'never quiet');
  const hanging = harness({ requestMs: REQUEST_CAP * 2 });
  await hanging.context.handleCommand({ action: 'click', selector: '#go', tabId: 7 });
  within(hanging.snapshotAt(), REQUEST_CAP, REQUEST_CAP + 150, 'request never ends');
}

async function testNoWatchOrNoSnapshotDoesNotWait() {
  // A page that navigated away has no watch.
  const gone = harness({ watching: false });
  await gone.context.handleCommand({ action: 'click', selector: 'a', tabId: 7 });
  within(gone.snapshotAt(), 0, 50, 'navigated away');
  // Without a snapshot, or with a full one, nothing waits.
  for (const params of [{ includeSnapshot: false }, { snapshot: 'full' }]) {
    const h = harness();
    const started = Date.now();
    await h.context.handleCommand({ action: 'click', selector: '#go', tabId: 7, ...params });
    within(Date.now() - started, 0, 50, JSON.stringify(params));
    assert.equal(h.sent.some(m => m.action === 'settleState'), false);
  }
}

async function testNoVisibleChangeAndKeepBaseline() {
  const h = harness({ unchanged: true });
  const result = await h.context.handleCommand({ action: 'checkBox', selector: '#a', tabId: 7, keepBaseline: true });
  assert.equal(result.no_visible_change, true);
  // keepBaseline diffs against an earlier action's baseline: it only starts the settle watch.
  assert.deepEqual([h.sent[0].deltaBaseline, h.sent[0].settleWatch], [undefined, true]);
}

(async () => {
  await testQuietPageWaitsTheMinimum();
  await testHiddenTabsWaitForThrottledTimers();
  await testLateChangeIsWaitedFor();
  await testRequestsTheActionStartedAreWaitedFor();
  await testCaps();
  await testNoWatchOrNoSnapshotDoesNotWait();
  await testNoVisibleChangeAndKeepBaseline();
  console.log('action_settle: ok');
})().catch(e => { console.error(e); process.exit(1); });
