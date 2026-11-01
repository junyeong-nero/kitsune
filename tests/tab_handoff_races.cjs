// #396 review regressions: revocation must win before dispatch, even after revoke + regrant.
const assert = require('node:assert/strict');
const { harness } = require('./shared_browser.cjs');
const client = { ownerId: 'owner', sessionId: 'session', clientName: 'Client' };
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return { promise, resolve }; };
async function setup(ids = [1]) {
  const h = harness({ userTabsWritable: false });
  await h.send({ action: 'openHandoffSession', ...client });
  h.grant = async tabId => {
    const request = h.send({ action: 'requestTab', ...client, tabId, reason: 'work' });
    await h.tick(0);
    const record = (await h.popup({ action: 'handoffs' })).requests.find(r => r.tabId === tabId);
    await h.popup({ action: 'handoffDecision', id: record.id, decision: 'approve' });
    assert.equal((await request).owned, true);
  };
  h.revoke = tabId => h.popup({ action: 'takeBackHandoff', tabId });
  for (const id of ids) await h.grant(id);
  h.calls.length = h.sent.length = 0;
  return h;
}
// Hold one actual asynchronous browser boundary; do not replace the production dispatch logic.
function barrier(object, method, matches) {
  const original = object[method];
  const entered = deferred(), release = deferred();
  let used = false;
  object[method] = async (...args) => {
    if (!used && matches(...args)) { used = true; entered.resolve(); await release.promise; }
    return original(...args);
  };
  return { entered: entered.promise, release: () => release.resolve() };
}
async function pingRevocations() {
  for (const how of ['take-back', 'session-end', 'transport', 'regrant']) {
    for (const action of ['click', 'clickAndWaitNavigation',
      'closeTab', 'elementScreenshot', 'fullPageScreenshot']) {
      const h = await setup();
      const gate = barrier(h.browser.tabs, 'sendMessage', (_id, msg) => msg.action === 'ping');
      const pending = h.send({ action, ...client, tabId: 1, selector: '#go', click: true, force: true,
        TAB_ADMISSION: { tabs: [] }, admission: { tabs: [] }, authorization: { tabs: [] } });
      await gate.entered;
      if (how === 'session-end') await h.send({ action: 'endHandoffSession', ...client });
      else if (how === 'transport') h.disconnect();
      else { await h.revoke(1); if (how === 'regrant') await h.grant(1); }
      gate.release();
      const result = await pending;
      assert.equal(result.code, 'user_tab_read_only', `${how}/${action}: ${JSON.stringify(result)}`);
      assert.ok(!h.sent.some(([name]) => ['click', 'screenPoint', 'focusState', 'agentInput', 'neuterUnload',
        'markScreenshotTarget', 'captureScroll', 'hideFixed'].includes(name)), `${how}/${action}`);
      assert.ok(!h.calls.some(([name]) => name === 'tabs.remove'));
    }
  }
}
async function readinessInjectionAndRetry() {
  for (const retry of [false, true]) {
    const h = await setup();
    const original = h.browser.tabs.sendMessage;
    let attempted = false;
    const gate = deferred(), entered = deferred();
    h.browser.tabs.sendMessage = async (id, msg, ...rest) => {
      if (msg.action === 'click' && !attempted) {
        attempted = true;
        throw new Error('Could not establish connection. Receiving end does not exist.');
      }
      if (msg.action === 'ping' && (!retry || attempted)) {
        entered.resolve(); await gate.promise;
        if (!retry) return null; // injection would follow this failed readiness ping
      }
      return original(id, msg, ...rest);
    };
    let injections = 0;
    h.browser.tabs.executeScript = async () => { injections++; };
    const pending = h.send({ action: 'click', ...client, tabId: 1 });
    await entered.promise;
    await h.revoke(1);
    gate.resolve();
    assert.equal((await pending).code, 'user_tab_read_only');
    assert.equal(injections, 0);
    assert.ok(!h.sent.some(([action]) => action === 'click'));
    assert.equal(attempted, retry);
  }
}
async function windowAndBrowserBoundaries() {
  for (const action of ['closeWindow', 'resizeWindow']) {
    const h = await setup([1, 2]);
    const gate = barrier(h.browser.tabs, 'sendMessage', (_id, msg) => msg.action === 'userActivity');
    const pending = h.send({ action, ...client, windowId: 1,
      tabId: action === 'resizeWindow' ? 2 : undefined, width: 900, height: 700 });
    await gate.entered;
    await h.revoke(1); // not resize's primary tab, and close has no primary tab
    gate.release();
    assert.equal((await pending).code, 'user_tab_read_only', `${action}/activity inspection`);
    assert.ok(!h.calls.some(([name]) => ['windows.remove', 'windows.update'].includes(name)));
  }
  for (const action of ['closeWindow', 'resizeWindow', 'focusWindow']) {
    const h = await setup([1, 2]);
    const gate = action === 'closeWindow'
      ? barrier(h.browser.windows, 'get', () => true)
      : barrier(h.browser.tabs, 'query', q => q.windowId === 1 && !q.active);
    // resize's primary target is 2; grant 1 must also be retained and checked.
    const pending = h.send({ action, ...client, tabId: action === 'resizeWindow' ? 2 : undefined,
      windowId: 1, width: 900, height: 700 });
    await gate.entered;
    await h.revoke(1);
    await h.grant(1);
    gate.release();
    assert.equal((await pending).code, 'user_tab_read_only', action);
    assert.ok(!h.calls.some(([name]) => ['windows.remove', 'windows.update'].includes(name)), action);
    assert.deepEqual(Object.keys(h.tabs), ['1', '2']);
  }
  // The final inventory query must not admit newly added user tabs either.
  const h = await setup([1, 2]);
  const gate = barrier(h.browser.windows, 'get', () => true);
  const pending = h.send({ action: 'closeWindow', ...client, windowId: 1 });
  await gate.entered;
  h.add({ url: 'https://user.test/new' });
  gate.release();
  assert.equal((await pending).code, 'user_tab_read_only');
  assert.ok(!h.calls.some(([name]) => name === 'windows.remove'));
  for (const action of ['navigate', 'wakeTab', 'detachTab']) {
    const h = await setup();
    if (action === 'wakeTab') h.tabs[1].discarded = true;
    if (action === 'detachTab') h.activate(2); // active-tab relocation is separately refused
    let reads = 0;
    const gate = action === 'detachTab' ? barrier(h.browser.windows, 'getAll', () => true)
      : barrier(h.browser.tabs, 'get', () => ++reads === 2); // after activity, inside the browser helper
    const pending = h.send({ action, ...client, tabId: 1, url: 'https://user.example/one#new' });
    await gate.entered;
    await h.revoke(1);
    gate.release();
    assert.equal((await pending).code, 'user_tab_read_only', action);
    assert.ok(!h.calls.some(([name]) => ['tabs.update', 'tabs.reload', 'windows.create'].includes(name)), action);
  }
}
async function noLateOSAuthorization() {
  const h = await setup();
  for (const action of ['screenPoint', 'agentInput', 'focusState']) {
    const result = await h.send({ action, ...client, tabId: 1, selector: '#go', click: true });
    assert.equal(result.code, 'unsupported_target');
    assert.equal(result.screenX, undefined, 'handoffs never produce OS authorization');
  }
  assert.deepEqual(h.sent, [], 'no native preparation, not even a readiness/activity query');
  assert.deepEqual(h.calls, []);
}
async function browserDecisionClaimsTimer() {
  for (const cancel of [null, 'navigate', 'session', 'take-back', 'storage-error']) {
    const h = harness({ userTabsWritable: false });
    await h.send({ action: 'openHandoffSession', ...client });
    const storage = deferred(), entered = deferred();
    h.browser.sessions = { removeTabValue: async () => { entered.resolve(); await storage.promise;
      if (cancel === 'storage-error') throw new Error('storage failed'); } };
    let settled = false;
    const pending = h.send({ action: 'requestTab', ...client, tabId: 1, reason: 'work' });
    pending.then(() => settled = true);
    await h.tick(29999);
    const id = (await h.popup({ action: 'handoffs' })).requests[0].id;
    const approval = h.popup({ action: 'handoffDecision', id, decision: 'approve' });
    await entered.promise;
    await h.tick(1);
    assert.equal(settled, false, 'browser decision retired the 30s fallback before storage awaited');
    assert.deepEqual((await h.popup({ action: 'handoffs' })).requests, []);
    assert.equal((await h.popup({ action: 'handoffDecision', id, decision: 'decline' })).code, 'needs_user');
    if (cancel === 'navigate') h.load(1, 'https://changed.test/');
    if (cancel === 'session') await h.send({ action: 'endHandoffSession', ...client });
    if (cancel === 'take-back') await h.popup({ action: 'takeBackHandoff', tabId: 1 });
    storage.resolve();
    const result = await pending, decision = await approval;
    assert.equal(result.state?.confirmToken, undefined);
    assert.equal(result.owned === true, cancel === null);
    assert.equal(decision.owned === true, cancel === null);
  }
}
async function normalPaths() {
  const h = await setup([1, 2]);
  const send = h.browser.tabs.sendMessage;
  h.browser.tabs.sendMessage = (id, message, ...rest) => {
    assert.deepEqual(Object.getOwnPropertySymbols(message), [], 'internal admission never reaches content');
    assert.equal(message.sessionId, undefined);
    return send(id, message, ...rest);
  };
  assert.equal((await h.send({ action: 'click', ...client, tabId: 1, selector: '#go' })).ok, true);
  assert.equal((await h.send({ action: 'resizeWindow', ...client, tabId: 2, width: 800, height: 600 })).ok, true);
  assert.equal((await h.send({ action: 'focusWindow', ...client, windowId: 1 })).ok, true);
  assert.equal((await h.send({ action: 'closeWindow', ...client, windowId: 1 })).ok, true);
}
async function dispatchedPageActionIsNotCancelled() {
  const h = await setup();
  const send = h.browser.tabs.sendMessage;
  h.browser.tabs.sendMessage = async (id, message, ...rest) => {
    const result = await send(id, message, ...rest);
    if (message.action === 'click') await h.revoke(id);
    return result;
  };
  assert.equal((await h.send({ action: 'click', ...client, tabId: 1 })).ok, true);
  assert.equal(h.sent.filter(([name]) => name === 'click').length, 1, 'the already-dispatched action ran');
  assert.equal((await h.send({ action: 'click', ...client, tabId: 1 })).code, 'user_tab_read_only');
}
let completed = false;
process.on('beforeExit', () => assert.ok(completed, 'an authorization-race barrier never completed'));
const tests = { pingRevocations, readinessInjectionAndRetry, windowAndBrowserBoundaries,
  noLateOSAuthorization, browserDecisionClaimsTimer, normalPaths, dispatchedPageActionIsNotCancelled };
(async () => {
  for (const test of process.argv[2] ? [tests[process.argv[2]]] : Object.values(tests)) await test();
  completed = true;
  console.log('tab_handoff_races: passed');
})().catch(error => { completed = true; console.error(error); process.exitCode = 1; });
