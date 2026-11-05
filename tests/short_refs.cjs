// Run with: node tests/short_refs.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element {
  constructor(tagName, textContent = '', attrs = {}, children = []) {
    Object.assign(this, { tagName, textContent, attrs, children, nodeType: 1,
      childNodes: [{ nodeType: 3, textContent }], isConnected: true });
    children.forEach(child => { child.parentElement = this; });
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  querySelectorAll() { return []; }
  closest() { return null; }
  focus() { this.focused = true; }
  dispatchEvent() { return true; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}

// buttons: the names of the page's buttons; the first is `button`. frame: the page also shows
// a cross-origin frame its own content script reads.
function content(buttons = ['Act'], { frame = false } = {}) {
  const [button, ...others] = buttons.map(name => new Element('BUTTON', name));
  const input = Object.assign(new Element('INPUT', '', { 'aria-label': 'Email' }),
    { type: 'text', value: '', id: 'email' });
  const frames = frame ? [Object.assign(new Element('IFRAME'), { contentDocument: null })] : [];
  const body = new Element('BODY', '', {}, [button, ...others, input, ...frames]);
  let listener;
  const context = vm.createContext({
    console, URL, crypto: webcrypto, setTimeout,
    KeyboardEvent: class { constructor(type, options) { Object.assign(this, { type }, options); } }, CSS: { escape: x => x },
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: { body, getElementById: () => null,
      querySelector: selector => selector === '#act' ? button : null,
      querySelectorAll: selector => selector === 'input, select, textarea' ? [input]
        : selector === 'button' || selector === '#act' ? [button] : [],
    },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null), getFrameId: () => 5 } },
  });
  const install = () => vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  install();
  return { button, context, send: msg => new Promise(resolve => listener(msg, {}, resolve)),
    reinject() { context.window.__claudeBridgeVersion = 0; install(); } };
}

function background(documents, stored = {}) {
  const sent = [];
  const context = vm.createContext({
    console, URL, Map, Set, Promise, setTimeout, clearTimeout,
    browser: { storage: { local: {
      get: async () => ({ ...stored }),
      set: async value => { Object.assign(stored, value); },
    } }, tabs: { sendMessage: async (tabId, msg) => {
      sent.push({ tabId, ...msg });
      return documents[tabId].send(msg);
    } } },
    TAB_ADMISSION: Symbol('test admission'), tabAdmissionError: () => null,
    getActiveTabId: async () => 1,
    tabState: async tabId => ({ tabId, status: 'complete', url: `https://example.com/${tabId}` }),
    isRestrictedUrl: () => false, ensureContentScript: async () => {},
    withTabState: async (tabId, response) => ({ ...response, state: { ...response.state, tabId } }),
  });
  const source = fs.readFileSync('extension/background/background.js', 'utf8');
  vm.runInContext(fs.readFileSync('extension/background/history.js', 'utf8'), context);
  vm.runInContext(source.slice(source.indexOf('const MISSING_CONTENT_RECEIVER'), source.indexOf('async function getActiveTabId')), context);
  return { sent, stored, context,
    send: (action, params = {}) => context.forwardToContent(action, { tabId: 1, ...params })
      .then(result => JSON.parse(JSON.stringify(result))) };
}

const buttonRef = result => result.snapshot.match(/button "Act" \[ref=(e\d+)\]/)[1];

// #459: a ref from another document names the element its snapshot line showed. When the
// targeted page lists exactly one element with that role and name, the error offers its ref.
// Nothing is sent to that element, and the error is otherwise unchanged.
async function sameElementOffers() {
  const docs = { 1: content(), 2: content(), 3: content(['Act', 'Act']), 4: content(['Other']),
    5: { send: async () => { throw new Error('Receiving end does not exist.'); } },
    6: { send: () => new Promise(() => {}) }, 7: content(['Act'], { frame: true }) };
  const s = background(docs);
  const act = buttonRef(await s.send('getSnapshot'));
  const otherDocument = (ref, tabId) => ({ error: 'stale ref, call browser_snapshot again', code: 'stale_ref',
    retryable: true, hint: 'The ref belongs to another document: the tab navigated or a different tab is ' +
      'targeted. Take a new browser_snapshot of the intended tab (select it with browser_tabs, or pass tab_id).',
    state: { reason: 'other_document', ref, tabId } });
  let before = s.sent.length;
  const offered = await s.send('focusElement', { selector: act, tabId: 2 });
  const same = offered.state.same_element?.ref;
  assert.match(same, /^e\d+$/);
  assert.notEqual(same, act);
  const plain = otherDocument(act, 2);
  assert.deepEqual(offered, { ...plain, hint: `${plain.hint} The same button "Act" here is ${same}; retry with it.`,
    state: { reason: 'other_document', ref: act, same_element: { ref: same, role: 'button', name: 'Act' }, tabId: 2 } });
  assert.deepEqual(s.sent.slice(before).map(msg => [msg.tabId, msg.action]), [[2, 'sameElement']]);
  assert.equal(docs[2].button.focused, undefined, 'the matched element is not acted on');
  // The offered ref is the one a snapshot of that tab lists, and acts there.
  assert.equal(buttonRef(await s.send('getSnapshot', { tabId: 2 })), same);
  assert.equal((await s.send('focusElement', { selector: same, tabId: 2 })).ok, true);
  assert.equal(docs[2].button.focused, true);
  // Two matches, none, a failed or unanswered lookup, or a page with a readable cross-origin
  // frame: the error stays as it was, and nothing but the lookup reaches the page.
  for (const tabId of [3, 4, 5, 6, 7]) {
    before = s.sent.length;
    assert.deepEqual(await s.send('focusElement', { selector: act, tabId }), otherDocument(act, tabId), `tab ${tabId}`);
    assert.deepEqual(s.sent.slice(before).map(msg => msg.action), ['sameElement']);
    if (docs[tabId].button) assert.equal(docs[tabId].button.focused, undefined);
  }
  // A retired alias keeps its label: the navigated tab's new document has the same button.
  s.context.pruneShortRefs(1);
  docs[1] = content();
  const navigated = await s.send('focusElement', { selector: act, tabId: 1 });
  assert.equal(navigated.state.reason, 'other_document');
  assert.equal(navigated.state.same_element.role, 'button');
  assert.equal(navigated.state.same_element.ref, buttonRef(await s.send('getSnapshot', { tabId: 1 })));
  // A ref no snapshot line showed has no role and name to match: no lookup.
  const unlisted = background({ 1: content(), 2: content() });
  const field = (await unlisted.send('getFormFields')).fields[0].ref;
  assert.deepEqual(await unlisted.send('focusElement', { selector: field, tabId: 2 }), otherDocument(field, 2));
  assert.equal(unlisted.sent.some(msg => msg.action === 'sameElement'), false);
}

async function main() {
  const documents = { 1: content(), 2: content() };
  const h = background(documents);
  const first = await h.send('getSnapshot');
  const old = buttonRef(first);
  assert.match(old, /^e\d+$/);
  assert.doesNotMatch(first.snapshot, /s\d+e\d+/);
  assert.equal((await h.send('focusElement', { selector: old })).ok, true);
  const internalOld = h.sent.at(-1).selector;
  assert.match(internalOld, /^s\d+e\d+$/);

  // Additive sources share the allocator and do not retire the snapshot.
  const found = (await h.send('find', { query: 'button' })).results[0].ref;
  const field = (await h.send('getFormFields')).fields[0].ref;
  const waited = await h.send('waitForElement', { selector: old });
  assert.equal(waited.selector, old);
  assert.equal(new Set([old, found, field, waited.ref]).size, 2);
  for (const ref of [old, found, field, waited.ref]) {
    assert.match(ref, /^e\d+$/);
    assert.equal((await h.send('focusElement', { selector: ref })).ok, true);
  }

  // A newer observation reuses the same public alias and internal node ref.
  const second = await h.send('getSnapshot');
  const fresh = buttonRef(second);
  assert.equal(fresh, old);
  const before = h.sent.length;
  assert.equal((await h.send('focusElement', { selector: old })).ok, true);
  assert.equal(h.sent.length, before + 1);
  assert.equal(h.sent.at(-1).selector, internalOld);

  // Scoped snapshots expand before the content script replaces the generation.
  const scoped = await h.send('getSnapshot', { ref: fresh });
  assert.match(scoped.snapshot, /button "Act" \[ref=e\d+\]/);
  // #240: a short ref may start a >> chain; the rest reaches the page unchanged, and
  // echoes keep the public spelling.
  assert.equal((await h.send('focusElement', { selector: `${fresh} >> xpath=..` })).ok, true);
  assert.equal(h.sent.at(-1).selector, `${internalOld} >> xpath=..`);
  // Surrounding whitespace does not hide the ref from expansion, alone or in a chain.
  assert.equal((await h.send('focusElement', { selector: ` ${fresh} >> xpath=..` })).ok, true);
  assert.equal(h.sent.at(-1).selector, `${internalOld} >> xpath=..`);
  assert.equal((await h.send('focusElement', { selector: ` ${fresh} ` })).ok, true);
  assert.equal(h.sent.at(-1).selector, internalOld);
  const refused = await h.send('focusElement', { selector: `${fresh} >> id=x` });
  assert.equal(refused.code, 'unsupported_selector');
  assert.equal(refused.state.selector, `${fresh} >> id=x`);
  const unknownChain = await h.send('focusElement', { selector: 'e999999999 >> xpath=..' });
  assert.equal(unknownChain.code, 'stale_ref');
  assert.equal(unknownChain.state.ref, 'e999999999');
  // Only a leading ref is expanded; CSS that merely starts with one is not a chain.
  await h.send('focusElement', { selector: `${fresh}>button` });
  assert.equal(h.sent.at(-1).selector, `${fresh}>button`);
  const otherTab = await h.send('focusElement', { selector: old, tabId: 2 });
  assert.equal(otherTab.state.reason, 'other_document');
  assert.equal(otherTab.state.ref, old);
  documents[1] = content();
  const navigated = await h.send('focusElement', { selector: old });
  assert.equal(navigated.state.reason, 'other_document');
  assert.equal(navigated.state.ref, old);
  const navigatedChain = await h.send('focusElement', { selector: `${old} >> xpath=..` });
  assert.equal(navigatedChain.state.reason, 'other_document');
  assert.equal(navigatedChain.state.ref, old);

  const unknown = await h.send('focusElement', { selector: 'e999999999' });
  assert.equal(unknown.code, 'stale_ref');
  assert.equal(unknown.state.reason, 'not_in_snapshot');
  assert.equal(unknown.state.ref, 'e999999999');
  // Legacy refs are still parsed and echoed, not treated as CSS or shortened on input.
  const legacy = await h.send('focusElement', { selector: internalOld });
  assert.equal(legacy.code, 'stale_ref');
  assert.equal(legacy.state.ref, internalOld);

  // Background reload loses aliases but skips the entire persisted reservation.
  const beforeReload = buttonRef(await h.send('getSnapshot'));
  const restarted = background(documents, h.stored);
  const afterReload = buttonRef(await restarted.send('getSnapshot'));
  assert.ok(BigInt(afterReload.slice(1)) > BigInt(beforeReload.slice(1)));
  assert.equal((await restarted.send('focusElement', { selector: beforeReload })).code, 'stale_ref');
  // Content reinjection also cannot reuse a generation, even before another snapshot.
  const additive = (await restarted.send('find', { query: 'button' })).results[0].ref;
  documents[1].reinject();
  await restarted.send('find', { query: 'button' });
  const reinjected = await restarted.send('focusElement', { selector: additive });
  assert.equal(reinjected.code, 'stale_ref');
  assert.equal(reinjected.state.reason, 'other_document');

  // Concurrent tabs reserve once and still get disjoint public indexes.
  const [a, b] = await Promise.all([
    restarted.send('getSnapshot'), restarted.send('getSnapshot', { tabId: 2 }),
  ]);
  assert.notEqual(buttonRef(a), buttonRef(b));
  // Page-owned strings and JS data must not be rewritten just because they resemble refs.
  const data = { result: { ref: internalOld, snapshot: `[ref=${internalOld}]` } };
  documents[1] = { send: async () => data };
  assert.deepEqual(await restarted.send('executeJS'), data);

  // All structured ref surfaces share the same aliases, including action targets
  // and ambiguous candidates. Drags expand both ends and restore their echoes.
  const internalA = 's123e0', internalB = 's123e1';
  documents[1] = { send: async () => ({ snapshot: `- button [ref=${internalA}]`,
    target_ref: internalA, tree: [{ ref: internalB }] }) };
  const surfaces = await restarted.send('getSnapshot');
  assert.match(surfaces.target_ref, /^e\d+$/);
  assert.equal(surfaces.snapshot, `- button [ref=${surfaces.target_ref}]`);
  assert.match(surfaces.tree[0].ref, /^e\d+$/);
  documents[1] = { send: async msg => ({ ok: true, from: msg.from, to: msg.to }) };
  const dragged = await restarted.send('drag', { from: surfaces.target_ref, to: surfaces.tree[0].ref });
  assert.deepEqual(dragged, { ok: true, from: surfaces.target_ref, to: surfaces.tree[0].ref });
  assert.equal(restarted.sent.at(-1).from, internalA);
  assert.equal(restarted.sent.at(-1).to, internalB);
  documents[1] = { send: async () => ({ error: 'Several targets', code: 'ambiguous_target',
    state: { candidates: [{ ref: internalA }, { ref: internalB }] } }) };
  const ambiguous = await restarted.send('click');
  assert.deepEqual(ambiguous.state.candidates.map(item => item.ref),
    [surfaces.target_ref, surfaces.tree[0].ref]);
  documents[1] = { send: async msg => ({ error: `Timed out waiting for "${msg.selector}"`, selector: msg.selector }) };
  const timedOut = await restarted.send('waitForElement', { selector: surfaces.target_ref });
  assert.equal(timedOut.selector, surfaces.target_ref);
  assert.equal(timedOut.error, `Timed out waiting for "${surfaces.target_ref}"`);

  // Serializer-owned URL lines use the existing credential policy, even without refs.
  // Ordinary page text is a JSON string on one line, never a URL protocol line.
  const raw = 'https://user:password@example.test/order/2?token=SECRET&view=summary#/route?password=HASH';
  const pageText = '- text ' + JSON.stringify('quoted " text\n- /url: "' + raw + '"');
  documents[1] = { send: async () => ({ snapshot: `${pageText}\n  - /url: ${JSON.stringify(raw)}`, url: raw, title: 'Ordinary title' }) };
  const metadata = await restarted.send('getSnapshot');
  assert.equal(metadata.url, 'https://example.test/order/2?token=[redacted]&view=summary#/route?password=[redacted]');
  assert.equal(metadata.title, 'Ordinary title');
  const redacted = (await restarted.send('getSnapshot')).snapshot.split('\n');
  assert.equal(redacted[0], pageText);
  assert.equal(redacted[1], '  - /url: "https://example.test/order/2?token=[redacted]&view=summary#/route?password=[redacted]"');
  // #310: a same-origin URL is a path, redacted the same way; a landmark marker names its ref.
  documents[1] = { send: async () => ({ snapshot: [
    '- banner "Site" [ref=s9e1]', '  - … 3 lines (2 links) not shown; snapshot s9e1 to list all',
    '- link "Order" [ref=s9e2]', '  - /url: "/order/2?token=SECRET&view=summary#password=HASH"'].join('\n') }) };
  const paths = (await restarted.send('getSnapshot')).snapshot.split('\n');
  const bannerRef = paths[0].match(/\[ref=(e\d+)\]/)[1];
  assert.equal(paths[1], `  - … 3 lines (2 links) not shown; snapshot ${bannerRef} to list all`);
  assert.equal(paths[3], '  - /url: "/order/2?token=[redacted]&view=summary#password=[redacted]"');
  for (const url of ['file:///private/path', 'data:text/plain,secret', 'about:blank']) {
    documents[1] = { send: async () => ({ snapshot: '', url }) };
    assert.equal((await restarted.send('getSnapshot')).url, undefined);
  }
  documents[1] = { send: async () => ({ result: { snapshot: `  - /url: ${JSON.stringify(raw)}` } }) };
  assert.equal((await restarted.send('executeJS')).result.snapshot, `  - /url: ${JSON.stringify(raw)}`);

  // Public budgets apply after URL redaction expands token=x, not to raw DOM strings.
  const expandedDoc = content();
  const expandingUrl = 'https://example.test/?' + Array(1000).fill('token=x').join('&');
  const link = Object.assign(new Element('A', 'Expanding link'), { href: expandingUrl });
  const main = new Element('MAIN', '', {}, [expandedDoc.button]);
  expandedDoc.context.document.body.children = [link, main];
  documents[1] = expandedDoc;
  const rawSnapshot = await expandedDoc.send({ action: 'getSnapshot' });
  assert.ok(rawSnapshot.snapshot.length < 12000);
  const unbounded = await restarted.send('getSnapshot');
  assert.ok(unbounded.snapshot.length > 17000);
  assert.equal(unbounded.truncated, undefined, 'omitted text has no new default cutoff');
  assert.match(unbounded.snapshot, /token=\[redacted\]/);
  const bounded = await restarted.send('getSnapshot', { text: true });
  assert.ok(bounded.snapshot.length <= 12000);
  assert.equal(bounded.truncated, true);
  assert.match(bounded.hint, /scope/);
  assert.doesNotMatch(bounded.snapshot, /token=x/);
  assert.match(bounded.snapshot, /truncated; scope/);
  assert.match(bounded.snapshot, /button "Act" \[ref=e\d+\]/, 'skip oversized URL, keep later complete controls');
  const targetRef = buttonRef(unbounded);
  await restarted.send('keypress', { selector: targetRef, key: 'Escape' });
  const targeted = await restarted.send('getSnapshot', { text: true, focusLastAction: true });
  assert.ok(targeted.snapshot.length <= 12000);
  assert.equal(targeted.target_ref, targetRef);
  assert.match(targeted.snapshot, /- main "" \[ref=e\d+\]\n  - button "Act"/);
  assert.equal(targeted.truncated, true);
  for (const text of [undefined, false, true]) {
    const small = await restarted.send('getSnapshot', { maxChars: 100, ...(text == null ? {} : { text }) });
    assert.ok(small.snapshot.length <= 100, JSON.stringify(small));
    assert.equal(small.truncated, true);
    assert.match(small.hint, /scope/);
    for (const line of small.snapshot.split('\n').filter(line => line.includes('[ref='))) {
      assert.match(line, /\[ref=e\d+\]$/);
    }
    for (const line of small.snapshot.split('\n').filter(line => line.includes('/url:'))) {
      assert.doesNotThrow(() => JSON.parse(line.split('/url: ')[1]));
    }
  }
  await restarted.send('keypress', { selector: targetRef, key: 'Escape' });
  const tooSmall = await restarted.send('getSnapshot', { maxChars: 80, focusLastAction: true });
  assert.ok(tooSmall.snapshot.length <= 80);
  assert.equal(tooSmall.target_ref, undefined, 'never advertise a target without its context');
  assert.doesNotMatch(tooSmall.snapshot, /button "Act"/);
  assert.match(tooSmall.hint, /target and its ancestor context did not fit/);
  await restarted.send('keypress', { selector: targetRef, key: 'Escape' });
  const tiny = await restarted.send('getSnapshot', { maxChars: 5, focusLastAction: true });
  assert.equal(tiny.snapshot, '');
  assert.equal(tiny.target_ref, undefined);
  assert.equal(tiny.truncated, true);
  assert.match(tiny.hint, /target and its ancestor context did not fit/);

  // Ref-like page names/text/values are JSON strings, not serializer-owned ref fields.
  const marker = '- … truncated; scope with selector, ref or max_depth';
  const decoys = [
    '- button ' + JSON.stringify('[ref=e9]') + ' [ref=e1]',
    '- button ' + JSON.stringify('escaped "quote" \\ path [ref=e9] \\"') + ' [ref=e1]',
    '- text ' + JSON.stringify('quoted "[ref=e9]" and \\ text'),
    '- textbox "Decoy" [ref=e1] [value=' + JSON.stringify('" \\ [ref=e9]') + ']',
  ];
  for (const decoy of decoys) {
    const actual = '  - button ' + JSON.stringify('Actual "target" \\ path [ref=e1]') + ' [ref=e9]';
    const block = '- main "" [ref=e2]\n' + actual;
    documents[1] = { send: async () => ({
      snapshot: decoy + '\n' + block + '\n- text ' + JSON.stringify('padding '.repeat(30)), target_ref: 'e9',
    }) };
    const fitting = await restarted.send('getSnapshot', { maxChars: block.length + marker.length + 1 });
    assert.equal(fitting.snapshot, block + '\n' + marker, 'only the owned ref receives target priority');
    assert.equal(fitting.target_ref, 'e9');
    assert.equal(fitting.truncated, true);

    documents[1] = { send: async () => ({
      snapshot: decoy + '\n- main "' + 'x'.repeat(100) + '" [ref=e2]\n' + actual, target_ref: 'e9',
    }) };
    const omitted = await restarted.send('getSnapshot', { maxChars: 100 });
    assert.ok(omitted.snapshot.length <= 100);
    assert.doesNotMatch(omitted.snapshot, /Actual/);
    assert.equal(omitted.target_ref, undefined, 'quoted ref-like text cannot retain an absent target');
    assert.match(omitted.hint, /target and its ancestor context did not fit/);
  }

  // A response larger than a reservation must still reserve every alias before
  // returning it. Invalid/unwritable counters must never fall back to index zero.
  documents[1] = { send: async () => ({ tree: Array.from({ length: 1100 }, (_, i) => ({ ref: `s456e${i}` })) }) };
  const large = await restarted.send('getAccessibilityTree');
  assert.equal(new Set(large.tree.map(item => item.ref)).size, 1100);
  assert.ok(BigInt(restarted.stored.shortRefEnd) > BigInt(large.tree.at(-1).ref.slice(1)));
  await assert.rejects(background(documents, { shortRefEnd: 'broken' }).send('getAccessibilityTree'),
    /Invalid short-ref counter/);
  const unwritable = {};
  Object.defineProperty(unwritable, 'shortRefEnd', { enumerable: true,
    get: () => '0', set: () => { throw new Error('storage unavailable'); } });
  await assert.rejects(background(documents, unwritable).send('getAccessibilityTree'), /storage unavailable/);

  await sameElementOffers();
  console.log('short refs: ok');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
