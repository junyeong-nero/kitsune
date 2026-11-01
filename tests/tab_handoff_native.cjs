// #396: handoffs allow DOM operations, never native-input preparation (including writable).
const assert = require('node:assert/strict');
const { harness, run } = require('./shared_browser.cjs');
const client = { ownerId: 'owner', sessionId: 'session-a', clientName: 'Client A' };

async function driver() {
  const h = harness({ userTabsWritable: false });
  await h.send({ action: 'openHandoffSession', ...client });
  await h.send({ action: 'openHandoffSession', ...client, sessionId: 'session-b' });
  const grant = async () => {
    const pending = h.send({ action: 'requestTab', ...client, tabId: 2, reason: 'DOM work' });
    await h.tick(0);
    const prompt = (await h.popup({ action: 'handoffs' })).requests[0];
    await h.popup({ action: 'handoffDecision', id: prompt.id, decision: 'approve' });
    assert.equal((await pending).owned, true);
  };
  await grant();
  const own = await h.send({ action: 'newTab', ...client, url: 'https://agent.test/' });
  h.sent.length = h.calls.length = 0;
  return async request => {
    switch (request.op) {
      case 'setup': return { tabId: 2, ownTabId: own.tabId };
      case 'command': {
        const command = { ...client, ...request.command };
        return command.action === 'screenPoint' ? run(h, command, 200) : h.send(command);
      }
      case 'revoke': return h.popup({ action: 'takeBackHandoff', tabId: 2 });
      case 'regrant': await h.popup({ action: 'takeBackHandoff', tabId: 2 }); await grant(); return { ok: true };
      case 'state': return { sent: h.sent, calls: h.calls, shown: h.shown(), focusedWindow: h.focusedWindow };
      case 'clear': h.sent.length = h.calls.length = 0; return { ok: true };
      default: throw new Error(`Unknown operation: ${request.op}`);
    }
  };
}

async function checks() {
  const send = await driver();
  const { tabId, ownTabId } = await send({ op: 'setup' });
  const command = command => send({ op: 'command', command });
  for (const userTabsWritable of [false, true]) {
    for (const action of ['trustedClick', 'trustedDrag', 'trustedType', 'trustedPressKey',
      'screenPoint', 'agentInput', 'focusState']) {
      for (const target of [{ selector: '#field' }, { ref: 'e1' }, { x: 1, y: 2 }]) {
        const params = { tabId, userTabsWritable, ...target, click: true,
          handoff: false, nativeAllowed: true, grant: null, TAB_ADMISSION: { writable: true } };
        const result = await command({ action: 'checkTabWrite', writeAction: action, ...params });
        assert.equal(result.code, 'unsupported_target', `${action}/${userTabsWritable}`);
        assert.equal(result.retryable, false);
        assert.equal(result.state.reason, 'handoff_native_input');
        assert.match(result.hint, /DOM.*new own tab/);
        if (['screenPoint', 'agentInput', 'focusState'].includes(action)) {
          assert.equal((await command({ action, ...params })).code, 'unsupported_target');
        }
      }
    }
  }
  assert.deepEqual(await send({ op: 'state' }), { sent: [], calls: [], shown: [1], focusedWindow: 1 });
  assert.equal((await command({ action: 'screenPoint', tabId, sessionId: 'session-b', click: true })).code,
    'user_tab_read_only', 'same stable owner, different session cannot use the grant');
  assert.equal((await command({ action: 'click', tabId, sessionId: 'session-b' })).code, 'user_tab_read_only');
  for (const action of ['click', 'doubleClick', 'hover', 'fill', 'keypress', 'drag']) {
    assert.equal((await command({ action, tabId, selector: '#field' })).ok, true, `DOM ${action}`);
  }
  assert.equal((await command({ action: 'screenPoint', tabId: ownTabId, click: true })).screenX, 10);
  await command({ action: 'restoreActiveTab', tabId: 1, from: ownTabId });
  await send({ op: 'regrant' });
  assert.equal((await command({ action: 'screenPoint', tabId, click: true })).code, 'unsupported_target');
  await send({ op: 'revoke' });
  assert.equal((await command({ action: 'screenPoint', tabId, click: true })).code, 'user_tab_read_only');
  assert.equal((await command({ action: 'click', tabId })).code, 'user_tab_read_only');

  // A compatibility command admitted before a grant appears must still refuse at readiness.
  const h = harness({ userTabsWritable: true });
  await h.send({ action: 'openHandoffSession', ...client });
  const original = h.browser.tabs.sendMessage;
  let grantAtPing = true;
  h.browser.tabs.sendMessage = async (id, message) => {
    if (message.action === 'ping' && grantAtPing) {
      grantAtPing = false;
      const pending = h.send({ action: 'requestTab', ...client, tabId: id, reason: 'work' });
      await h.tick(0);
      const prompt = (await h.popup({ action: 'handoffs' })).requests[0];
      await h.popup({ action: 'handoffDecision', id: prompt.id, decision: 'approve' });
      await pending;
    }
    return original(id, message);
  };
  assert.equal((await h.send({ action: 'screenPoint', ...client, tabId: 1, click: true })).code,
    'unsupported_target');
  assert.ok(!h.sent.some(([name]) => ['screenPoint', 'agentInput'].includes(name)));
  assert.deepEqual(h.calls, []);
  console.log('tab_handoff_native: passed');
}

async function serve() {
  const send = await driver();
  for await (const line of require('node:readline').createInterface({ input: process.stdin })) {
    try { console.log(JSON.stringify(await send(JSON.parse(line)))); }
    catch (error) { console.log(JSON.stringify({ testError: error.stack })); }
  }
}
let completed = false;
process.on('beforeExit', () => assert.ok(completed, 'native policy test did not finish'));
(process.argv.includes('--bridge') ? serve() : checks()).then(() => { completed = true; })
  .catch(error => { completed = true; console.error(error); process.exitCode = 1; });
