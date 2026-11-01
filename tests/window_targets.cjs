// Concrete window admission must match dispatch; aliases must never reach Firefox's API.
const assert = require('node:assert/strict');
const { harness } = require('./shared_browser.cjs');

async function main() {
  const h = harness({ userTabsWritable: false });
  const send = command => h.send({ ownerId: 'client-a', ...command });
  const own = (await send({ action: 'newTab' })).tabId;
  const reset = () => { h.calls.length = h.sent.length = 0; };
  const noEffects = () => { assert.deepEqual(h.calls, []); assert.deepEqual(h.sent, []); };
  for (const windowId of [-2, -1, 0, false, true, '', [], [1], {}, 1.5, '-2', '1.5', Infinity, NaN, 2 ** 53]) {
    for (const action of ['closeWindow', 'focusWindow', 'resizeWindow', 'moveTab', 'detachTab']) {
      for (const preflight of [false, true]) {
        reset();
        const result = await send({ action: preflight ? 'checkTabWrite' : action, writeAction: action,
          windowId, tabId: own, width: 800, height: 600 });
        assert.equal(result.code, 'invalid_window_id', `${action}/${windowId}/${preflight}`);
        assert.equal(result.retryable, false);
        noEffects();
      }
    }
  }
  for (const action of ['closeWindow', 'focusWindow']) {
    for (const windowId of [undefined, null]) {
      reset();
      assert.equal((await send({ action, windowId })).code, 'invalid_window_id');
      noEffects();
    }
  }
  for (const userTabsWritable of [false, true]) {
    for (const action of ['closeWindow', 'focusWindow', 'moveTab']) {
      reset();
      assert.equal((await send({ action, windowId: -2, tabId: own, userTabsWritable })).code, 'invalid_window_id');
      assert.equal((await send({ action, windowId: 999, tabId: own, userTabsWritable })).code, 'window_unavailable');
      noEffects();
    }
  }
  const win = await send({ action: 'createWindow' });
  const active = win.tabIds[0];
  for (const windowId of [String(win.windowId), '1', null]) {
    reset();
    assert.equal((await send({ action: 'moveTab', tabId: own, windowId })).ok, true);
    assert.deepEqual(h.calls, [['tabs.move', own, { windowId: windowId == null ? 1 : Number(windowId), index: -1 }]]);
    assert.deepEqual(h.shown(), [1]);
    assert.equal(h.focusedWindow, 1);
    assert.equal(h.tabs[active].active, true);
    assert.equal(h.tabs[own].active, false);
  }
  for (const tabId of [1, own]) {
    for (const action of ['moveTab', 'detachTab']) {
      reset();
      assert.equal((await send({ action, ownerId: 'client-b', tabId, windowId: win.windowId })).code, 'user_tab_read_only');
      noEffects();
    }
  }
  // No speculative mutation/restoration when removing the shown tab could select a user tab.
  h.activate(own);
  for (const action of ['moveTab', 'detachTab']) {
    reset();
    const result = await send({ action, tabId: own, windowId: win.windowId });
    assert.equal(result.code, 'unsupported_target');
    assert.equal(result.retryable, false);
    noEffects();
    assert.deepEqual(h.shown(), [own]);
  }
  reset();
  assert.equal((await send({ action: 'moveTab', tabId: own, index: 0 })).ok, true);
  assert.deepEqual(h.shown(), [own]);
  h.activate(1);
  // Full-window operations still refuse mixed user/client ownership, even via an owned tab.
  const foreign = (await send({ action: 'newTab', ownerId: 'client-b' })).tabId;
  await send({ action: 'moveTab', ownerId: 'client-b', tabId: foreign, windowId: win.windowId });
  for (const [windowId, tabId, refusedTab] of [[1, own, 1], [win.windowId, active, foreign]]) {
    for (const action of ['closeWindow', 'focusWindow', 'resizeWindow']) {
      reset();
      const result = await send({ action, windowId: String(windowId), tabId, width: 800, height: 600 });
      assert.equal(result.code, 'user_tab_read_only');
      assert.equal(result.state.tabId, refusedTab);
      noEffects();
    }
  }
  await send({ action: 'moveTab', ownerId: 'client-b', tabId: foreign, windowId: 1 });
  // An omitted move destination binds the source, even if the tab changes windows meanwhile.
  h.afterQuery = query => {
    if (Object.keys(query).length === 0) {
      h.afterQuery = null;
      h.tabs[own].windowId = win.windowId;
    }
  };
  reset();
  assert.equal((await send({ action: 'moveTab', tabId: own })).ok, true);
  assert.deepEqual(h.calls, [['tabs.move', own, { windowId: 1, index: -1 }]]);
  // A tab relocating after the admission snapshot cannot retarget resize to the user window.
  h.afterQuery = query => {
    if (Object.keys(query).length === 0) {
      h.afterQuery = null;
      h.tabs[active].windowId = 1;
    }
  };
  reset();
  assert.equal((await send({ action: 'resizeWindow', tabId: active, width: 800, height: 600 })).ok, true);
  assert.deepEqual(h.calls, [['windows.update', win.windowId, { state: 'normal', width: 800, height: 600 }]]);
  h.tabs[active].windowId = win.windowId;
  // Existing activity protection still guards explicit owned-window focus.
  h.activity[1] = { reason: 'input' };
  reset();
  assert.equal((await send({ action: 'focusWindow', windowId: win.windowId })).code, 'user_active');
  assert.deepEqual(h.calls, []);
  delete h.activity[1];
  assert.equal((await send({ action: 'focusWindow', windowId: String(win.windowId) })).ok, true);
  reset();
  assert.equal((await send({ action: 'closeWindow', windowId: String(win.windowId) })).ok, true);
  assert.deepEqual(h.calls, [['windows.remove', win.windowId]]);
  reset();
  assert.equal((await send({ action: 'focusWindow', windowId: win.windowId })).code, 'window_unavailable');
  noEffects();
  h.focusWindow(1);
  assert.equal((await send({ action: 'detachTab', tabId: own })).ok, true);
  assert.deepEqual(h.shown(), [1]);
  assert.equal(h.focusedWindow, 1);
  // Explicit writable compatibility retains active-tab movement, but not window aliases.
  assert.equal((await send({ action: 'detachTab', tabId: 1, userTabsWritable: true })).ok, true);
  const empty = harness({ userTabsWritable: false });
  for (const id of Object.keys(empty.tabs)) delete empty.tabs[id];
  for (const action of ['moveTab', 'detachTab', 'resizeWindow']) {
    assert.equal((await empty.send({ action })).code, 'browser_window_unavailable');
    assert.deepEqual(empty.calls, []);
    assert.deepEqual(empty.sent, []);
  }
  console.log('window_targets: passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
