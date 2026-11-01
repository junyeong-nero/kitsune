// #395: the real background dispatch gate, two clients, and no side effects on refusal.
const assert = require('node:assert/strict');
const { harness, run } = require('./shared_browser.cjs');

async function main() {
  const h = harness({ userTabsWritable: false });
  for (const tabId of [0, false, true, '', [], [1, 2], {}, -1, 1.5, '0', '1.5', Infinity, NaN, 2 ** 53]) {
    for (const action of ['click', 'moveTab', 'screenPoint']) {
      h.calls.length = h.sent.length = 0;
      const refused = await h.send({ action, ownerId: 'client-a', tabId, selector: '#go', click: true });
      assert.equal(refused.code, 'invalid_tab_id', `${action}: ${JSON.stringify(tabId)}`);
      assert.equal(refused.retryable, false);
      assert.deepEqual(h.calls, []);
      assert.deepEqual(h.sent, []);
    }
  }
  assert.equal((await h.send({ action: 'click', tabId: false, userTabsWritable: true })).code, 'invalid_tab_id');
  const a = (await h.send({ action: 'newTab', ownerId: 'client-a', url: 'https://a.test/' })).tabId;
  const b = (await h.send({ action: 'newTab', ownerId: 'client-b', url: 'https://b.test/' })).tabId;
  h.sent.length = h.calls.length = 0;
  assert.equal((await h.send({ action: 'click', tabId: '1', ownerId: 'client-a' })).code, 'user_tab_read_only');
  assert.deepEqual(h.sent, []);
  assert.equal((await h.send({ action: 'click', tabId: String(a), ownerId: 'client-a', selector: '#go' })).ok, true);
  assert.ok(h.sent.some(([action, tabId]) => action === 'click' && tabId === a));
  h.sent.length = h.calls.length = 0;
  assert.equal((await h.send({ action: 'click', tabId: 999, ownerId: 'client-a' })).code, 'tab_closed');
  assert.deepEqual(h.sent, []);
  assert.deepEqual(h.calls, []);
  const writes = [
    'click', 'doubleClick', 'type', 'fill', 'setEditableContent', 'keypress', 'checkBox', 'selectOption',
    'submitForm', 'formFill', 'drag', 'uploadChunk', 'uploadCommit', 'uploadAbort', 'hover', 'scroll',
    'focusElement', 'blurElement', 'navigate', 'goBack', 'goForward', 'reloadTabBrowser', 'closeTab',
    'switchTab', 'waitForResult', 'clickAndWaitNavigation', 'elementScreenshot', 'fullPageScreenshot', 'executeJS',
    'pinTab', 'muteTab', 'setZoom', 'resizeWindow', 'moveTab', 'detachTab', 'wakeTab', 'highlight',
    'clearHighlight', 'handleDialog', 'media', 'agentInput', 'neuterUnload', 'keepAlive',
    'markScreenshotTarget', 'unmarkScreenshotTarget', 'screenPoint',
  ];
  for (const [ownerId, tabId] of [['client-a', 1], ['client-a', 2], ['client-a', b], ['client-b', a]]) {
    for (const action of writes) {
      h.calls.length = h.sent.length = 0;
      const result = await h.send({ action, ownerId, tabId, selector: '#go', click: true,
        url: 'https://changed.test/', force: true, viewedTabApproved: true, userTabApproved: true,
        holdViewedTab: false, protectUserTabs: false });
      assert.equal(result.code, 'user_tab_read_only', `${ownerId}/${tabId}/${action}`);
      assert.equal(result.retryable, false);
      const affected = action === 'resizeWindow' ? 1 : tabId;
      assert.deepEqual(result.state, { tabId: affected,
        index: Object.keys(h.tabs).map(Number).indexOf(affected), action });
      assert.match(result.hint, /browser_tabs action=new/);
      assert.match(result.hint, /action=request when handoff support is available/);
      assert.equal(result.state.confirmToken, undefined);
      assert.deepEqual(h.calls, [], 'no browser writes');
      assert.deepEqual(h.sent, [], 'not even userActivity or content injection');
    }
  }
  for (const command of [
    { action: 'find', loadMore: true, tabId: 1 },
    { action: 'find', load_more: true, tabId: 2 },
    { action: 'storageOp', op: 'set', tabId: 1 },
    { action: 'focusWindow', windowId: 1 },
    { action: 'closeWindow', windowId: 1 },
    { action: 'click' },
  ]) {
    assert.equal((await h.send({ ...command, ownerId: 'client-a' })).code, 'user_tab_read_only');
  }
  for (const op of ['get', 'list', 'snapshot']) {
    assert.equal((await h.send({ action: 'storageOp', tabId: 1, op })).error, undefined);
  }
  // Reads must not activate a tab, scroll, focus or change ownership.
  for (const tabId of [1, 2, b]) {
    for (const action of ['getSnapshot', 'getPageInfo', 'getPageText', 'getHTML', 'getBounds', 'query',
      'find', 'getAccessibilityTree', 'getFormFields', 'consoleLogs', 'captureNetwork', 'screenshot']) {
      h.calls.length = h.sent.length = 0;
      const result = await h.send({ action, ownerId: 'client-a', tabId });
      assert.equal(result.error, undefined, action);
      assert.deepEqual(h.shown(), [1]);
      assert.ok(h.calls.every(([name]) => name === 'tabs.captureTab'), JSON.stringify(h.calls));
      assert.ok(h.sent.every(([name]) => !['scroll', 'focusElement', 'screenPoint'].includes(name)));
    }
  }
  for (const [ownerId, tabId] of [['client-a', a], ['client-b', b]]) {
    assert.equal((await h.send({ action: 'click', ownerId, tabId, selector: '#go' })).ok, true);
    const tabs = (await h.send({ action: 'getTabs', ownerId })).tabs;
    assert.deepEqual(tabs.filter(t => t.owned).map(t => t.id), [tabId]);
    h.activity[tabId] = { reason: 'input', input: 'keydown' };
    assert.equal((await h.send({ action: 'click', ownerId, tabId, selector: '#go' })).code, 'user_active');
    delete h.activity[tabId];
  }
  // A view change between ownership admission and dispatch must not retarget an implicit write.
  h.activate(a);
  h.afterQuery = query => {
    if (query.active) {
      h.afterQuery = null;
      h.activate(1);
    }
  };
  h.sent.length = 0;
  assert.equal((await h.send({ action: 'click', ownerId: 'client-a', selector: '#go' })).ok, true);
  assert.ok(h.sent.some(([action, tabId]) => action === 'click' && tabId === a));
  assert.ok(!h.sent.some(([action, tabId]) => action === 'click' && tabId === 1));
  assert.deepEqual(h.shown(), [1]);
  // Trusted OS input on our own tab may temporarily show it, then restore the user's view.
  const point = await run(h, { action: 'screenPoint', ownerId: 'client-a', tabId: a,
    selector: '#go', click: true }, 200);
  assert.equal(point.previousTabId, 1);
  assert.deepEqual(h.shown(), [a]);
  assert.equal((await h.send({ action: 'restoreActiveTab', ownerId: 'client-b', tabId: 1, from: a })).restored, false);
  assert.equal((await h.send({ action: 'restoreActiveTab', ownerId: 'client-a', tabId: 2, from: a })).restored, false);
  assert.equal((await h.send({ action: 'restoreActiveTab', ownerId: 'client-a', tabId: 1, from: a })).restored, true);
  assert.deepEqual(h.shown(), [1]);
  assert.equal((await h.send({ action: 'restoreActiveTab', ownerId: 'client-a', tabId: 2, from: a })).restored, false);
  // Only the bridge-authoritative compatibility setting permits writes, not old approval fields.
  assert.equal((await h.send({ action: 'click', ownerId: 'client-a', tabId: 1,
    userTabsWritable: true, selector: '#go' })).ok, true);
  // #398: writable lifts ownership protection, never the trusted-input pause; it is per command.
  h.activity[1] = { reason: 'input', input: 'keydown' };
  assert.equal((await h.send({ action: 'click', ownerId: 'client-a', tabId: 1,
    userTabsWritable: true, holdViewedTab: false, selector: '#go' })).code, 'user_active');
  delete h.activity[1];
  assert.equal((await h.send({ action: 'click', ownerId: 'client-a', tabId: 1,
    selector: '#go' })).code, 'user_tab_read_only');
  assert.equal((await h.send({ action: 'getTabs', ownerId: 'client-a' })).tabs.find(t => t.id === 1).owned,
    false, 'writable never transfers ownership');
  console.log('user_tabs_read_only: passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
