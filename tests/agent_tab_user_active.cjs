// #397: owned-tab admission, writable compatibility and window-wide mutation safety.
// Run with: node tests/agent_tab_user_active.cjs (no browser/OS input).
const assert = require('node:assert/strict');
const { harness } = require('./shared_browser.cjs');

(async () => {
  for (const userTabsWritable of [false, true]) {
    const h = harness({ userTabsWritable });
    const { tabId } = await h.send({ action: 'newTab', url: 'https://agent.test/' });
    h.activity[tabId] = { reason: 'typing', field: 'input' };
    for (const command of [
      { action: 'type', text: 'agent' }, { action: 'fill', text: 'agent' },
      { action: 'navigate', url: 'https://next.test/' }, { action: 'goBack' }, { action: 'goForward' },
      { action: 'reloadTabBrowser' }, { action: 'closeTab', force: true },
      { action: 'screenPoint', click: true }, { action: 'agentInput', ms: 3000 },
      { action: 'focusState' }, { action: 'pinTab' }, { action: 'muteTab' }, { action: 'setZoom' },
      { action: 'moveTab', index: 0 }, { action: 'detachTab' },
      { action: 'find', loadMore: true }, { action: 'storageOp', op: 'set' },
    ]) {
      const effects = h.calls.length;
      const result = await h.send({ tabId, ...command });
      assert.equal(result.code, 'user_active', command.action);
      assert.equal(result.retryable, true);
      assert.equal(h.calls.length, effects, 'no mutation before busy refusal');
    }
    const inventory = await h.send({ action: 'getTabs' });
    assert.equal(inventory.tabs.find(t => t.id === tabId).owned, true, 'input is not a handoff');
    delete h.activity[tabId];
    assert.equal((await h.send({ action: 'type', tabId, text: 'agent' })).ok, true, 'release permits retry');
    assert.equal((await h.send({ action: 'closeTab', tabId })).ok, true);

    const win = await h.send({ action: 'createWindow', url: 'https://agent.test/window' });
    const first = win.tabIds[0];
    const second = (await h.send({ action: 'newTab', url: 'https://agent.test/other' })).tabId;
    await h.send({ action: 'moveTab', tabId: second, windowId: win.windowId });
    h.activity[second] = { reason: 'input', input: 'wheel', sinceMs: 0, windowMs: 3000 };
    for (const command of [
      { action: 'closeWindow', windowId: win.windowId },
      { action: 'resizeWindow', tabId: first, width: 800, height: 600 },
    ]) {
      const effects = h.calls.length;
      const result = await h.send(command);
      assert.equal(result.code, 'user_active', command.action + ' checks the other affected tab');
      assert.equal(result.state.tabId, second);
      assert.equal(h.calls.length, effects);
    }
    delete h.activity[second];
    assert.equal((await h.send({ action: 'closeWindow', windowId: win.windowId })).ok, true);
  }
  const h = harness();
  h.load(1, 'https://user.test/loading');
  await h.tick(9999);
  assert.equal((await h.send({ action: 'closeTab', tabId: 1 })).state.reason, 'navigating');
  await h.tick(1);
  assert.equal((await h.send({ action: 'closeTab', tabId: 1 })).ok, true, '10 s navigation bound');
  console.log('agent_tab_user_active: ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
