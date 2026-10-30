// Run with: node tests/cross_origin_frame_reads.cjs (no browser or npm dependencies).
// #354: browser_find, browser_forms, browser_dom and deltas read cross-origin frames through each
// frame's own content script, as browser_snapshot does since #343. Documents are scripted per
// frame id here; tests/test_real_browser_iframes.py runs the content script in real frames.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const FRAME_READ_TIMEOUT_MS = Number(/const FRAME_READ_TIMEOUT_MS = (\d+);/
  .exec(fs.readFileSync('extension/background/background.js', 'utf8'))[1]);

// documents: frameId -> msg => response. Every message is recorded with its frame id. A document
// that throws (a failed assert) does not answer, as a frame that fails is left unread.
function background(documents) {
  const sent = [];
  const stored = {};
  const context = vm.createContext({
    console, URL, Map, Set, Promise, setTimeout, clearTimeout,
    browser: { storage: { local: {
      get: async () => ({ ...stored }),
      set: async value => { Object.assign(stored, value); },
    } }, tabs: { sendMessage: async (tabId, msg, { frameId }) => {
      sent.push({ frameId, ...msg });
      return documents[frameId](msg);
    } } },
    getActiveTabId: async () => 1,
    tabState: async tabId => ({ tabId, status: 'complete', url: 'https://shop.example/checkout' }),
    isRestrictedUrl: () => false, ensureContentScript: async () => {},
    NoContentReceiver: class NoContentReceiver extends Error {},
    withTabState: async (tabId, response) => ({ ...response, state: { ...response.state, tabId } }),
  });
  const source = fs.readFileSync('extension/background/background.js', 'utf8');
  vm.runInContext(fs.readFileSync('extension/background/history.js', 'utf8'), context);
  vm.runInContext(source.slice(source.indexOf('const MISSING_CONTENT_RECEIVER'),
    source.indexOf('async function getActiveTabId')), context);
  return { sent, send: (action, params = {}) => context.forwardToContent(action, { tabId: 1, ...params })
    .then(result => JSON.parse(JSON.stringify(result))) };
}

// The page lists the widget (frame 7, whose viewport starts at 10,20 in the page's) at s1e5.
const WIDGET = { ref: 's1e5', frameId: 7, offset: [10, 20] };

async function testFindMergesFrameMatchesByScore() {
  const h = background({
    0: () => ({ results: [{ ref: 's1e1', tag: 'h2', text: 'Payment', selector: 'h2', bounds: [0, 0, 50, 10], score: 3 }],
      frames: [WIDGET] }),
    7: msg => (assert.deepEqual({ ...msg }, { action: 'find', query: 'pay' }),
      { results: [{ ref: 's2e1', tag: 'button', text: 'Pay', selector: 'form > button', bounds: [1, 2, 30, 10], score: 9 }] }),
  });
  const found = await h.send('find', { query: 'pay' });
  assert.deepEqual(found.results.map(r => [r.text, r.score]), [['Pay', 9], ['Payment', 3]]);
  const [pay, heading] = found.results;
  // In the page's viewport, and without the frame's selector, which would run in the page.
  assert.deepEqual(pay.bounds, [11, 22, 30, 10]);
  assert.equal('selector' in pay, false);
  assert.equal(heading.selector, 'h2');
  assert.match(pay.ref, /^e\d+$/);
  assert.equal(found.frames, undefined);
}

async function testFrameRefsRouteIntoTheFrame() {
  const clicks = [];
  const h = background({
    0: msg => msg.action === 'find' ? { results: [], hint: 'No element matches', frames: [WIDGET] } : (clicks.push([0, msg]), { ok: true }),
    7: msg => msg.action === 'find'
      ? { results: [{ ref: 's2e1', tag: 'button', text: 'Pay', bounds: [1, 2, 30, 10], score: 9 }] }
      : (clicks.push([7, msg.selector]), { ok: true }),
  });
  const found = await h.send('find', { query: 'pay' });
  // The page's no-match hint does not describe the frame's match.
  assert.equal(found.hint, undefined);
  assert.equal((await h.send('click', { selector: found.results[0].ref })).ok, true);
  assert.deepEqual(clicks, [[7, 's2e1']]);
}

async function testFindSelectorMatchesWinAndTheFrameBudgetIsShared() {
  const frames = Array.from({ length: 8 }, (_, i) => ({ ref: `s1e${10 + i}`, frameId: 10 + i, offset: [0, 0] }));
  const documents = { 0: () => ({ results: [{ ref: 's1e1', tag: 'button', text: 'Top', bounds: [0, 0, 1, 1] }], frames }) };
  for (const { frameId } of frames) {
    documents[frameId] = () => ({ results: [{ ref: `s${frameId}e1`, tag: 'button', text: `In ${frameId}`, bounds: [0, 0, 1, 1],
      ...(frameId === 10 ? {} : { score: 5 }) }] });
  }
  const h = background(documents);
  const found = await h.send('find', { query: 'button' });
  // The query matched as a CSS selector in the page and frame 10: their matches, not scored ones.
  assert.deepEqual(found.results.map(r => r.text), ['Top', 'In 10']);
  assert.match(found.hint, /^2 more cross-origin frames were not read; /);
  assert.deepEqual(h.sent.filter(m => m.action === 'find').map(m => m.frameId), [0, 10, 11, 12, 13, 14, 15]);
}

async function testFormsListFrameFieldsAndDropTheFrameFromUnavailable() {
  const tiny = { ref: 's1e6', title: '', src: 'https://ads.example/pixel', content: 'cross_origin' };
  const h = background({
    0: () => ({ fields: [{ ref: 's1e1', name: 'zip', selector: '#zip', bounds: [0, 0, 10, 10] }],
      unavailableFrames: [{ ref: 's1e5', title: 'Payment', src: 'https://pay.example/widget', content: 'cross_origin' }, tiny],
      frames: [WIDGET] }),
    7: () => ({ fields: [{ ref: 's2e1', name: 'card', redacted: true, hasValue: true, selector: '#card', bounds: [0, 0, 100, 20] },
      { ref: 's2e2', name: 'otp', frame: 'Verify', selector: '#otp', bounds: [5, 5, 10, 10] }] }),
  });
  const forms = await h.send('getFormFields');
  assert.deepEqual(forms.fields.map(f => [f.name, f.frame, f.bounds, 'selector' in f]), [
    ['zip', undefined, [0, 0, 10, 10], true],
    ['card', 'Payment', [10, 20, 100, 20], false],
    // A frame inside the widget keeps its own name.
    ['otp', 'Verify', [15, 25, 10, 10], false],
  ]);
  assert.deepEqual(forms.unavailableFrames, [{ ...tiny, ref: forms.unavailableFrames[0].ref }]);
}

async function testDomAttachesFrameTreesWithinTheCap() {
  const node = (ref, extra = {}) => ({ ref, tag: 'iframe', content: 'cross_origin', src: 'https://w.example/', bounds: [0, 0, 300, 100], ...extra });
  const big = { ref: 's8e0', tag: 'body', text: 'x'.repeat(19650) };
  const h = background({
    0: () => ({ tree: { ref: 's1e0', tag: 'body', children: [node('s1e5', { title: 'Payment' }), node('s1e6')] }, refs: 3,
      frames: [{ ...WIDGET, depth: 1 }, { ref: 's1e6', frameId: 8, offset: [0, 0], depth: 1 }] }),
    7: msg => (assert.deepEqual([msg.depth, msg.limit > 19000], [4, true]),
      { tree: { ref: 's2e0', tag: 'body', children: [{ ref: 's2e1', tag: 'button', text: 'Pay', bounds: [1, 2, 30, 10] }] }, refs: 2 }),
    // It fits what the page left, as the content script reads within that.
    8: msg => (assert.ok(msg.limit >= JSON.stringify(big).length), { tree: big, refs: 1 }),
  });
  const dom = await h.send('getAccessibilityTree', { depth: 6 });
  const [widget, other] = dom.tree.children;
  assert.equal(widget.frame, 'cross_origin');
  assert.equal('content' in widget, false);
  assert.deepEqual(widget.children[0].children[0].bounds, [11, 22, 30, 10]);
  // The second frame's tree no longer fits what the first left of the cap.
  assert.deepEqual([other.content, other.children], ['cross_origin', undefined]);
  assert.deepEqual([dom.truncated, dom.refs], [true, 5]);
  assert.ok(JSON.stringify(dom.tree).length <= 20000);
}

async function testDomFrameWithoutRoomIsTruncated() {
  // The page's tree leaves no room for the frame's root node.
  const h = background({
    0: () => ({ tree: { ref: 's1e0', tag: 'body', text: 'x'.repeat(19990), children: [{ ref: 's1e5', tag: 'iframe',
      content: 'cross_origin' }] }, refs: 2, frames: [{ ...WIDGET, depth: 1 }] }),
    7: () => ({ tree: null, refs: 0, truncated: true, hint: 'Tree cut' }),
  });
  const dom = await h.send('getAccessibilityTree', { depth: 6 });
  assert.equal(dom.truncated, true);
  assert.match(dom.hint, /^Tree cut at 20,000 chars/);
  assert.equal(dom.tree.children[0].content, 'cross_origin');
}

async function testFrameLinksAreRedactedAndTiesPreferShorterText() {
  const h = background({
    0: msg => msg.action === 'find'
      ? { results: [{ ref: 's1e1', tag: 'div', text: 'Pay with card or bank', bounds: [0, 0, 1, 1], score: 4 }], frames: [WIDGET] }
      : { tree: { ref: 's1e0', tag: 'body', children: [{ ref: 's1e5', tag: 'iframe', content: 'cross_origin' }] }, refs: 2,
        frames: [{ ...WIDGET, depth: 1 }] },
    7: msg => msg.action === 'find'
      ? { results: [{ ref: 's2e1', tag: 'button', text: 'Pay', bounds: [0, 0, 1, 1], score: 4 }] }
      : { tree: { ref: 's2e0', tag: 'a', href: 'https://pay.example/done?token=KITSUNE_FAKE_SECRET' }, refs: 1 },
  });
  assert.deepEqual((await h.send('find', { query: 'pay' })).results.map(r => r.text), ['Pay', 'Pay with card or bank']);
  const link = (await h.send('getAccessibilityTree', { depth: 6 })).tree.children[0].children[0];
  assert.doesNotMatch(link.href, /KITSUNE_FAKE_SECRET/);
  assert.match(link.href, /^https:\/\/pay\.example\/done\?token=/);
}

// The background merges frame results under the content script's own caps.
function testBackgroundCapsMatchTheContentScript() {
  const background = fs.readFileSync('extension/background/background.js', 'utf8');
  const content = fs.readFileSync('extension/content/content.js', 'utf8');
  for (const name of ['FIND_LIMIT', 'DOM_TREE_LIMIT']) {
    const value = source => new RegExp(`const ${name} = (\\d+);`).exec(source)?.[1];
    assert.equal(value(background), value(content), name);
  }
  const hint = source => /const DOM_TRUNCATED_HINT = ([^;]+);/.exec(source)[1].replace(/'\s*\+\s*'/g, '');
  assert.equal(hint(background), hint(content), 'DOM_TRUNCATED_HINT');
}

// A page whose widget (frame 7) was read by a snapshot, then a delta.
function deltaPage({ widgetDelta, pageDelta }) {
  const frameLine = '- iframe "Payment" [ref=s1e5] [content=cross_origin src="https://pay.example/widget"]';
  return background({
    0: msg => msg.delta || msg.sinceObservation ? pageDelta(msg, frameLine)
      : { snapshot: `- heading "Checkout" [ref=s1e1]\n${frameLine}`, frames: [WIDGET] },
    7: msg => msg.delta || msg.sinceObservation ? widgetDelta(msg) : { snapshot: '- button "Pay" [ref=s2e1]' },
  });
}

async function testPageActionDeltaListsWhatChangedInTheFrame() {
  const h = deltaPage({
    widgetDelta: msg => (assert.deepEqual([msg.sinceObservation, msg.delta, [...msg.keepRefs]], [true, false, []]),
      { snapshot: '- button "Remove coupon" [ref=s2e2]', delta: true }),
    // The page keeps the widget's line for its changes (keepRefs).
    pageDelta: (msg, frameLine) => (assert.deepEqual([msg.delta, [...msg.keepRefs]], [true, ['s1e5']]),
      { snapshot: `- button "Apply coupon" [ref=s1e9]\n${frameLine}`, delta: true, target_ref: 's1e9',
        frames: [{ ref: 's1e5', frameId: 7 }] }),
  });
  await h.send('getSnapshot');
  const delta = await h.send('getSnapshot', { delta: true, actionFrame: 0, focusLastAction: true, maxChars: 4000 });
  assert.match(delta.snapshot,
    /^- button "Apply coupon" \[ref=(e\d+)\]\n- iframe "Payment" \[ref=e\d+\] \[frame=cross_origin [^\n]+\n {2}- button "Remove coupon" \[ref=e\d+\]$/);
  assert.equal(delta.unchanged, undefined);
  // The widget's delta is read once, before the page's.
  assert.deepEqual(h.sent.slice(2).map(m => m.frameId), [7, 0]);
}

async function testActionInsideTheFrameNamesItsTarget() {
  const h = deltaPage({
    // The action ran in the widget: its baseline and target; it changed nothing.
    widgetDelta: msg => (assert.deepEqual([msg.delta, msg.sinceObservation, msg.focusLastAction], [true, undefined, true]),
      { snapshot: '- button "Pay" [ref=s2e1]', delta: true, unchanged: true, target_ref: 's2e1' }),
    pageDelta: (msg, frameLine) => (assert.deepEqual([msg.delta, msg.sinceObservation, msg.focusLastAction], [false, true, false]),
      { snapshot: frameLine, delta: true, unchanged: true, frames: [{ ref: 's1e5', frameId: 7 }] }),
  });
  const pay = (await h.send('getSnapshot')).snapshot.match(/button "Pay" \[ref=(e\d+)\]/)[1];
  const delta = await h.send('getSnapshot', { delta: true, actionFrame: 7, focusLastAction: true });
  assert.equal(delta.target_ref, pay);
  assert.equal(delta.unchanged, true);
  assert.match(delta.snapshot, /\n {2}- button "Pay"/);
}

async function testAFrameOnlyFindReadIsNotAChangeButANewDocumentIs() {
  // browser_find read the widget, so no snapshot showed it: its full answer is not a change.
  const findOnly = background({
    0: msg => msg.action === 'find' ? { results: [], frames: [WIDGET] }
      : (assert.deepEqual([...msg.keepRefs], []), { snapshot: '', delta: true, unchanged: true }),
    7: msg => msg.action === 'find' ? { results: [{ ref: 's2e1', tag: 'button', text: 'Pay', bounds: [0, 0, 1, 1], score: 9 }] }
      : (assert.equal(msg.textWhenFull, true), { snapshot: '- button "Pay" [ref=s2e1]' }),
  });
  await findOnly.send('find', { query: 'pay' });
  const quiet = await findOnly.send('getSnapshot', { sinceObservation: true });
  assert.deepEqual([quiet.snapshot, quiet.unchanged], ['', true]);

  // The widget now holds another document (its refs are of a new generation): listed in full.
  const replaced = deltaPage({
    widgetDelta: () => ({ snapshot: '- button "Pay again" [ref=s3e1]' }),
    pageDelta: (msg, frameLine) => (assert.deepEqual([...msg.keepRefs], ['s1e5']),
      { snapshot: frameLine, delta: true, unchanged: true, frames: [{ ref: 's1e5', frameId: 7 }] }),
  });
  await replaced.send('getSnapshot');
  const delta = await replaced.send('getSnapshot', { sinceObservation: true });
  assert.match(delta.snapshot, /\n {2}- button "Pay again"/);
  assert.equal(delta.unchanged, undefined);
}

async function testAnActionThatLoadsAnotherDocumentInItsFrameIsAChange() {
  // Pay submitted the widget's form: its new page has no baseline, no target and no refs.
  const h = deltaPage({
    widgetDelta: msg => (assert.deepEqual([msg.delta, msg.textWhenFull], [true, true]), { snapshot: '- text "Paid."' }),
    pageDelta: (msg, frameLine) => (assert.deepEqual([...msg.keepRefs], ['s1e5']),
      { snapshot: frameLine, delta: true, unchanged: true, frames: [{ ref: 's1e5', frameId: 7 }] }),
  });
  await h.send('getSnapshot');
  const delta = await h.send('getSnapshot', { delta: true, actionFrame: 7, focusLastAction: true });
  assert.match(delta.snapshot, /\[frame=cross_origin [^\n]+\n {2}- text "Paid\."$/);
  assert.equal(delta.unchanged, undefined);
}

async function testAnActionFrameThatDoesNotAnswerIsNotUnchanged() {
  const h = deltaPage({
    widgetDelta: () => new Promise(() => {}),
    pageDelta: () => ({ snapshot: '', delta: true, unchanged: true }),
  });
  await h.send('getSnapshot');
  const started = Date.now();
  const delta = await h.send('getSnapshot', { delta: true, actionFrame: 7, focusLastAction: true });
  assert.ok(Date.now() - started < FRAME_READ_TIMEOUT_MS + 500);
  assert.deepEqual([delta.snapshot, delta.unchanged], ['', undefined]);
}

async function testAFrameTheBrowserNoLongerFindsIsForgotten() {
  let widgetGone = false;
  const h = deltaPage({
    widgetDelta: () => { if (widgetGone) throw new Error('Frame not found'); return { snapshot: '', delta: true, unchanged: true }; },
    pageDelta: () => ({ snapshot: '', delta: true, unchanged: true }),
  });
  await h.send('getSnapshot');
  widgetGone = true;
  await h.send('getSnapshot', { sinceObservation: true });
  h.sent.length = 0;
  await h.send('getSnapshot', { sinceObservation: true });
  assert.deepEqual(h.sent.map(m => m.frameId), [0]);
}

(async () => {
  await testFindMergesFrameMatchesByScore();
  await testFrameRefsRouteIntoTheFrame();
  await testFindSelectorMatchesWinAndTheFrameBudgetIsShared();
  await testFormsListFrameFieldsAndDropTheFrameFromUnavailable();
  await testDomAttachesFrameTreesWithinTheCap();
  await testFrameLinksAreRedactedAndTiesPreferShorterText();
  await testPageActionDeltaListsWhatChangedInTheFrame();
  await testActionInsideTheFrameNamesItsTarget();
  await testAFrameOnlyFindReadIsNotAChangeButANewDocumentIs();
  await testAnActionThatLoadsAnotherDocumentInItsFrameIsAChange();
  await testAnActionFrameThatDoesNotAnswerIsNotUnchanged();
  await testAFrameTheBrowserNoLongerFindsIsForgotten();
  await testDomFrameWithoutRoomIsTruncated();
  testBackgroundCapsMatchTheContentScript();
  console.log('cross_origin_frame_reads: ok');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
