// #397: real background dispatch for native-input cleanup, without a browser or OS input.
// Python's native keyboard loop uses --bridge; standalone checks exercise the same state machine.
const assert = require('node:assert/strict');
const { harness, run } = require('./shared_browser.cjs');

async function driver() {
  const h = harness({ userTabsWritable: false });
  const ownerId = 'client-a';
  const { tabId } = await h.send({ action: 'newTab', url: 'https://agent.test/', ownerId });
  return async request => {
    switch (request.op) {
      case 'setup': return { tabId, previousTabId: 1 };
      case 'command': {
        const command = { ownerId, ...request.command };
        return command.action === 'screenPoint' ? run(h, command, 200) : h.send(command);
      }
      case 'input':
        if (request.kind === 'frame') h.frameInput(tabId, 7, 'wheel');
        else if (request.kind === 'navigation') {
          await h.tick(2001); // outside the existing agent-effect navigation grace
          h.load(tabId, 'https://user.test/navigation');
        } else h.activity[tabId] = { reason: 'input', input: 'wheel', sinceMs: 0, windowMs: 3000 };
        return { ok: true };
      case 'release':
        delete h.activity[tabId];
        h.tabs[tabId].status = 'complete';
        await h.tick(10000);
        return { ok: true };
      case 'activate': h.activate(request.tabId); return { ok: true };
      case 'state':
        return { shown: h.shown(), tabs: (await h.send({ action: 'getTabs', ownerId })).tabs };
      default: throw new Error(`Unknown test operation: ${request.op}`);
    }
  };
}

async function checks() {
  for (const kind of ['top', 'frame', 'navigation']) {
    const send = await driver();
    const { tabId, previousTabId } = await send({ op: 'setup' });
    const command = command => send({ op: 'command', command });
    await command({ action: 'screenPoint', tabId, click: true, selector: '#field' });
    await send({ op: 'input', kind });
    const interrupted = await command({ action: 'focusState', tabId, selector: '#field' });
    assert.equal(interrupted.code, 'user_active', kind);
    assert.equal(interrupted.retryable, true);
    const restore = { action: 'restoreActiveTab', tabId: previousTabId, from: tabId };
    assert.equal((await command(restore)).restored, false, `${kind}: cleanup must leave the busy tab displayed`);
    let state = await send({ op: 'state' });
    assert.deepEqual(state.shown, [tabId]);
    assert.equal(state.tabs.find(t => t.id === tabId).owned, true);
    await send({ op: 'release' });
    assert.equal((await command(restore)).restored, false, 'busy cleanup consumes its transition');
    await send({ op: 'activate', tabId: 2 });
    assert.equal((await command(restore)).restored, false, 'a stale restore cannot hijack a later user view');
    assert.deepEqual((await send({ op: 'state' })).shown, [2]);
  }
  const send = await driver();
  const { tabId, previousTabId } = await send({ op: 'setup' });
  const command = command => send({ op: 'command', command });
  await command({ action: 'screenPoint', tabId, click: true, selector: '#field' });
  const restore = { action: 'restoreActiveTab', tabId: previousTabId, from: tabId };
  assert.equal((await command({ ...restore, ownerId: 'other-client' })).restored, false);
  assert.equal((await command({ ...restore, tabId: 2 })).restored, false, 'previous-tab binding is authenticated');
  assert.deepEqual((await send({ op: 'state' })).shown, [tabId]);
  assert.equal((await command(restore)).restored, true, 'idle authorized cleanup still works');
  assert.deepEqual((await send({ op: 'state' })).shown, [previousTabId]);

  // The activity query itself yields. Even switching away and back invalidates the old
  // temporary transition, so cleanup must not overrule the user's newer choice.
  const h = harness({ userTabsWritable: false });
  const opened = await h.send({ action: 'newTab', url: 'https://agent.test/', ownerId: 'client-a' });
  await run(h, { action: 'screenPoint', tabId: opened.tabId, click: true, ownerId: 'client-a' }, 200);
  Object.defineProperty(h.activity, opened.tabId, { get() {
    h.activate(2);
    h.activate(opened.tabId);
    return undefined;
  } });
  assert.equal((await h.send({ action: 'restoreActiveTab', tabId: 1, from: opened.tabId,
    ownerId: 'client-a' })).restored, false, 'view changes during the query invalidate restoration');
  assert.deepEqual(h.shown(), [opened.tabId]);
  console.log('native_input_restore: ok');
}

async function serve() {
  const send = await driver();
  const lines = require('node:readline').createInterface({ input: process.stdin });
  for await (const line of lines) {
    try { console.log(JSON.stringify(await send(JSON.parse(line)))); }
    catch (error) { console.log(JSON.stringify({ testError: error.stack })); }
  }
}
(process.argv.includes('--bridge') ? serve() : checks())
  .catch(error => { console.error(error); process.exitCode = 1; });
