// Run with: node tests/user_activity.cjs (no browser or npm dependencies).
// Issue #139: the content script tells the user's trusted input apart from the agent's synthetic
// events, so agent writes back off (user_active) only while the user is using the page.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

function content() {
  let now = 100000;
  const listeners = {};
  const reported = [];
  const field = { tagName: 'INPUT', value: '', isConnected: true };
  const editor = { tagName: 'DIV', isContentEditable: true, textContent: '', isConnected: true };
  const host = { tagName: 'MAIL-COMPOSER', isConnected: true };  // a field inside its shadow root
  const button = { tagName: 'BUTTON', isConnected: true };
  const document = { body: {}, activeElement: null, focused: true, hasFocus() { return this.focused; },
    querySelector: () => null, querySelectorAll: () => [] };
  let onMessage;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, CSS: { escape: x => x },
    Date: class extends Date { static now() { return now; } },
    window: {
      addEventListener: (type, fn, options) => {
        assert.equal(options.capture, true, 'listeners see input before page handlers');
        listeners[type] = fn;
      },
      getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
    },
    document,
    browser: { runtime: { onMessage: { addListener: fn => { onMessage = fn; } }, sendMessage: msg => {
      if (msg.action === 'trustedTabInput') reported.push(JSON.parse(JSON.stringify(msg)));
      return Promise.resolve(null);
    } } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  return {
    field, editor, host, button, document, listeners, reported,
    tick: ms => { now += ms; },
    // Trusted input comes from the browser; Kitsune's own dispatchEvent calls are untrusted.
    input: (type, target, isTrusted = true) => listeners[type]({ type, target, isTrusted }),
    send: msg => new Promise(resolve => onMessage(msg, {}, resolve)).then(r => JSON.parse(JSON.stringify(r))),
  };
}

async function main() {
  const page = content();
  assert.deepEqual(Object.keys(page.listeners).sort(),
    ['keydown', 'mousedown', 'paste', 'pointerdown', 'touchstart', 'wheel']);
  assert.deepEqual(await page.send({ action: 'userActivity' }), { active: false });

  // Synthetic events (Kitsune's own clicks and keypresses) are not the user.
  page.input('mousedown', page.button, false);
  page.input('keydown', page.field, false);
  assert.deepEqual(await page.send({ action: 'userActivity' }), { active: false });

  assert.deepEqual(page.reported, [], 'synthetic events do not affect opener inheritance');

  // Any trusted input counts for 3 s.
  page.input('wheel', page.button);
  assert.deepEqual(page.reported, [{ action: 'trustedTabInput', at: 100000 }]);
  page.tick(2999);
  assert.deepEqual(await page.send({ action: 'userActivity' }),
    { active: true, state: { reason: 'input', input: 'wheel', sinceMs: 2999, windowMs: 3000 } });
  page.tick(1);
  assert.deepEqual(await page.send({ action: 'userActivity' }), { active: false });

  // Typing counts while the field keeps focus in a focused document and holds content.
  page.document.activeElement = page.field;
  page.input('keydown', page.field);
  page.field.value = 'draft';
  page.tick(60000);
  assert.deepEqual(await page.send({ action: 'userActivity' }),
    { active: true, state: { reason: 'typing', field: 'input' } }, 'the value is never read out');
  page.document.focused = false;  // the user switched to another tab or app
  assert.deepEqual(await page.send({ action: 'userActivity' }), { active: false });
  page.document.focused = true;
  page.field.value = '';
  assert.deepEqual(await page.send({ action: 'userActivity' }), { active: false }, 'an emptied field is no draft');
  page.field.value = 'draft';
  page.document.activeElement = page.button;
  assert.deepEqual(await page.send({ action: 'userActivity' }), { active: false }, 'focus left the field');

  // Editors and fields in shadow roots (seen as their custom-element host) count too.
  for (const [el, text] of [[page.editor, 'textContent'], [page.host, null]]) {
    page.document.activeElement = el;
    page.input('paste', el);
    if (text) el[text] = 'pasted';
    page.tick(5000);
    assert.equal((await page.send({ action: 'userActivity' })).state?.reason, 'typing', el.tagName);
  }
  // A field the agent typed into is not the user's draft.
  const agentField = { tagName: 'TEXTAREA', value: 'agent text', isConnected: true };
  page.document.activeElement = agentField;
  page.input('keydown', agentField, false);
  page.tick(5000);
  assert.deepEqual(await page.send({ action: 'userActivity' }), { active: false });

  // browser_trusted_click: the OS click right after its screenPoint is the agent's, a keypress is not.
  const clicked = content();
  await clicked.send({ action: 'screenPoint', x: 5, y: 5, click: true });
  clicked.input('pointerdown', clicked.button);
  clicked.input('mousedown', clicked.button);
  assert.deepEqual(clicked.reported, [], 'agent OS clicks do not affect opener inheritance');
  assert.deepEqual(await clicked.send({ action: 'userActivity' }), { active: false });
  clicked.input('keydown', clicked.button);
  assert.equal((await clicked.send({ action: 'userActivity' })).state.input, 'keydown');
  clicked.tick(2000);  // the trusted-click window is over
  clicked.input('mousedown', clicked.button);
  assert.equal((await clicked.send({ action: 'userActivity' })).state.input, 'mousedown');
  // Issue #306: the trusted click's own modifier key presses are the agent's, other keys are not.
  const modified = content();
  await modified.send({ action: 'screenPoint', x: 5, y: 5, click: true, modifiers: ['Shift', 'Meta'] });
  for (const key of ['Shift', 'Meta']) modified.listeners.keydown({ type: 'keydown', key, target: modified.button, isTrusted: true });
  modified.input('mousedown', modified.button);
  assert.deepEqual(modified.reported, [], 'agent modifiers do not affect opener inheritance');
  assert.deepEqual(await modified.send({ action: 'userActivity' }), { active: false });
  modified.listeners.keydown({ type: 'keydown', key: 'Control', target: modified.button, isTrusted: true });
  assert.equal((await modified.send({ action: 'userActivity' })).state.input, 'keydown');
  const later = content();
  await later.send({ action: 'screenPoint', x: 5, y: 5, click: true, modifiers: ['Shift'] });
  later.tick(2000);
  later.listeners.keydown({ type: 'keydown', key: 'Shift', target: later.button, isTrusted: true });
  assert.equal((await later.send({ action: 'userActivity' })).state.input, 'keydown');
  // A later click without modifiers no longer excuses the earlier click's keys.
  const plain = content();
  await plain.send({ action: 'screenPoint', x: 5, y: 5, click: true, modifiers: ['Shift'] });
  await plain.send({ action: 'screenPoint', x: 5, y: 5, click: true });
  plain.listeners.keydown({ type: 'keydown', key: 'Shift', target: plain.button, isTrusted: true });
  assert.equal((await plain.send({ action: 'userActivity' })).state.input, 'keydown');
  // #346/#347: during the window the bridge announces with agentInput, trusted keys and pastes
  // are its OS keyboard, not the user's; a mouse press is still the user's, and after the window
  // keys are the user's again.
  const typed = content();
  assert.deepEqual(await typed.send({ action: 'agentInput', ms: 3000 }), { ok: true });
  typed.input('keydown', typed.field);
  typed.input('paste', typed.field);
  assert.deepEqual(typed.reported, [], 'agent OS keys do not affect opener inheritance');
  assert.deepEqual(await typed.send({ action: 'userActivity' }), { active: false });
  typed.input('mousedown', typed.button);
  assert.equal((await typed.send({ action: 'userActivity' })).state.input, 'mousedown');
  const after = content();
  await after.send({ action: 'agentInput', ms: 3000 });
  after.tick(3000);
  after.input('keydown', after.field);
  assert.equal((await after.send({ action: 'userActivity' })).state.input, 'keydown');
  // A plain screenPoint read opens no window.
  const read = content();
  await read.send({ action: 'screenPoint', x: 5, y: 5 });
  read.input('mousedown', read.button);
  assert.equal((await read.send({ action: 'userActivity' })).state.input, 'mousedown');
}

main().then(() => console.log('User activity checks passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
