// Issue #449: every tab is readable, whoever owns it; only writes are gated. Reads that scroll the page
// run on a tab this client cannot write only while it is not shown, and restore its scroll position;
// reading a discarded tab wakes it in the background. Run with: node tests/read_any_tab.cjs
const assert = require('node:assert/strict');
const { harness, run } = require('./shared_browser.cjs');

const MISSING_RECEIVER = 'Could not establish connection. Receiving end does not exist.';

// The shared browser (window 1 shows user tab 1; tab 2 is a background user tab) with pages that
// scroll (h.scroll[tabId], 300 px tall in a 100 px viewport), capture canvases, discarded tabs whose
// page does not answer, and tabs.reload, which wakes a discarded tab as Firefox does.
function scrollingHarness() {
  const h = harness({ userTabsWritable: false });
  h.scroll = {};
  const send = h.browser.tabs.sendMessage;
  h.browser.tabs.sendMessage = async (id, msg, options) => {
    if (h.tabs[id]?.discarded) throw new Error(MISSING_RECEIVER);
    const at = h.scroll[id] ??= { x: 0, y: 0 };
    const answer = {
      captureScroll: () => {
        if (Number.isFinite(msg.to)) Object.assign(at, { x: Number.isFinite(msg.left) ? msg.left : 0, y: msg.to });
        else if (Number.isFinite(msg.by)) at.y = Math.min(at.y + msg.by, 200);
        return { sy: at.y, sx: at.x };
      },
      fullPageMetrics: () => ({ viewportWidth: 100, viewportHeight: 100, docWidth: 100, docHeight: 300 }),
      markScreenshotTarget: () => ({ selector: '[data-kitsune-capture-1]', attribute: 'data-kitsune-capture-1' }),
      captureScrollIntoView: () => { at.y = 150; return { ok: true }; },
      captureMeasure: () => ({ x: 0, y: 200 - at.y, w: 40, h: 30, vw: 100, vh: 100, sy: at.y }),
    }[msg.action];
    if (!answer) return send(id, msg, options);
    h.sent.push([msg.action, id]);
    return answer();
  };
  h.browser.tabs.reload = async id => {
    h.calls.push(['tabs.reload', id]);
    h.tabs[id].discarded = false;
    h.load(id, h.tabs[id].url);
    h.tabs[id].status = 'complete';
    h.browser.tabs.onUpdated.emit(id, { status: 'complete' }, { ...h.tabs[id] });
  };
  h.context.loadDataUrl = async () => ({ width: 100, height: 100 });
  h.context.encodeScreenshotCanvas = async () => 'data:image/png;base64,AA==';
  h.context.OffscreenCanvas = class {
    constructor(width, height) { Object.assign(this, { width, height }); }
    getContext() { return { drawImage() {}, fillRect() {} }; }
  };
  return h;
}

async function testScrollingReadsOnTabsThatAreNotShown() {
  const h = scrollingHarness();
  const other = (await h.send({ action: 'newTab', ownerId: 'client-b', url: 'https://b.test/' })).tabId;
  for (const tabId of [2, other]) {
    for (const command of [{ action: 'fullPageScreenshot' }, { action: 'elementScreenshot', selector: '#chart' }]) {
      h.scroll[tabId] = { x: 7, y: 120 };
      h.calls.length = h.sent.length = 0;
      const shot = await run(h, { ...command, ownerId: 'client-a', tabId }, 5000);
      assert.equal(shot.ok, true, `${tabId}/${command.action}: ${JSON.stringify(shot)}`);
      assert.ok(h.calls.some(([name, id]) => name === 'tabs.captureTab' && id === tabId));
      assert.deepEqual(h.scroll[tabId], { x: 7, y: 120 }, 'the capture scrolled the page back');
      assert.deepEqual(h.shown(), [1]);
      assert.deepEqual(h.activations(), []);
    }
    // The core browser_find load_more: its admission check, then scrolls that say loadMore.
    for (const command of [{ action: 'checkTabWrite', writeAction: 'find', loadMore: true },
      { action: 'find', loadMore: true }, { action: 'scroll', direction: 'bottom', loadMore: true }]) {
      const result = await h.send({ ...command, ownerId: 'client-a', tabId });
      assert.equal(result.code, undefined, `${tabId}/${command.action}: ${JSON.stringify(result)}`);
    }
    // Scroll, hover and JavaScript stay writes, shown or not.
    for (const action of ['scroll', 'hover', 'executeJS']) {
      h.sent.length = 0;
      assert.equal((await h.send({ action, ownerId: 'client-a', tabId, selector: '#go' })).code, 'user_tab_read_only');
      assert.deepEqual(h.sent, []);
    }
  }
  // On the shown user tab they would move the user's view: refused before anything reaches the page.
  for (const command of [{ action: 'fullPageScreenshot' }, { action: 'elementScreenshot', selector: '#chart' },
    { action: 'checkTabWrite', writeAction: 'fullPageScreenshot' },
    { action: 'checkTabWrite', writeAction: 'find', loadMore: true },
    { action: 'find', loadMore: true }, { action: 'scroll', direction: 'bottom', loadMore: true }]) {
    h.calls.length = h.sent.length = 0;
    const refused = await h.send({ ...command, ownerId: 'client-a', tabId: 1 });
    assert.equal(refused.code, 'user_tab_read_only', JSON.stringify(command));
    assert.match(refused.hint, /only while it is not shown/);
    assert.deepEqual(h.calls, []);
    assert.deepEqual(h.sent, []);
  }
  // A tab that becomes shown is refused from then on; the user's activity still pauses its owner.
  h.activate(2);
  assert.equal((await h.send({ action: 'fullPageScreenshot', ownerId: 'client-a', tabId: 2 })).code,
    'user_tab_read_only');
  h.activate(1);
  h.activity[other] = { reason: 'input', input: 'keydown' };
  assert.equal((await h.send({ action: 'fullPageScreenshot', ownerId: 'client-a', tabId: other })).code,
    'user_active');
}

async function testReadingADiscardedTabWakesItInTheBackground() {
  const h = scrollingHarness();
  const own = (await h.send({ action: 'newTab', ownerId: 'client-a', url: 'https://a.test/' })).tabId;
  const other = (await h.send({ action: 'newTab', ownerId: 'client-b', url: 'https://b.test/' })).tabId;
  for (const [tabId, action] of [[2, 'getSnapshot'], [other, 'getSnapshot'], [2, 'screenshot']]) {
    h.tabs[tabId].discarded = true;
    h.calls.length = h.sent.length = 0;
    const read = await run(h, { action, ownerId: 'client-a', tabId }, 1000);
    assert.equal(read.error, undefined, `${tabId}/${action}: ${JSON.stringify(read)}`);
    assert.deepEqual(h.calls.filter(([name]) => name !== 'tabs.captureTab'), [['tabs.reload', tabId]]);
    assert.equal(h.tabs[tabId].discarded, false);
    if (action === 'getSnapshot') assert.ok(h.sent.some(([name, id]) => name === 'getSnapshot' && id === tabId));
    assert.deepEqual(h.shown(), [1], 'waking never shows the tab');
    assert.deepEqual(h.activations(), []);
  }
  // Waking for a write is still a write: refused on a tab this client cannot write, and a write on
  // its own discarded tab still reports tab_unloaded instead of reloading it.
  h.tabs[2].discarded = true;
  h.calls.length = 0;
  for (const action of ['click', 'wakeTab']) {
    assert.equal((await h.send({ action, ownerId: 'client-a', tabId: 2, selector: '#go' })).code, 'user_tab_read_only');
  }
  h.tabs[own].discarded = true;
  assert.equal((await run(h, { action: 'click', ownerId: 'client-a', tabId: own, selector: '#go' }, 1000)).code,
    'tab_unloaded');
  assert.deepEqual(h.calls, []);
}

(async () => {
  await testScrollingReadsOnTabsThatAreNotShown();
  await testReadingADiscardedTabWakesItInTheBackground();
  console.log('read_any_tab: passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
