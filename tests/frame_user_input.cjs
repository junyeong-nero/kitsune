// Run with: node tests/frame_user_input.cjs. Input observation must not need a frame command.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const manifest = JSON.parse(fs.readFileSync('extension/manifest.json', 'utf8'));
const source = file => fs.readFileSync(`extension/${file.replace(/^\//, '')}`, 'utf8');
const copy = value => JSON.parse(JSON.stringify(value));
function event() {
  const listeners = [];
  return { addListener: fn => listeners.push(fn), emit: (...args) => listeners.map(fn => fn(...args)) };
}
function harness() {
  let now = 100000, nextTab = 10;
  const frames = [], messages = [], injected = [], forwarded = [], timers = new Map();
  let nextTimer = 0;
  const tabs = new Map([[1, { id: 1, active: true, url: 'https://parent.test/', windowId: 1 }]]);
  const runtime = { id: 'kitsune-test', onMessage: event(), onStartup: event(), onInstalled: event() };
  const browser = {
    runtime,
    tabs: {
      onCreated: event(), onRemoved: event(), onUpdated: event(), onActivated: event(),
      query: async () => [...tabs.values()], get: async id => tabs.get(id), getZoom: async () => 1,
      sendMessage: async (id, msg, options) => {
        const frame = frames.find(f => f.frameId === options?.frameId);
        return frame ? frame.send(msg) : null;
      },
      executeScript: async (id, params) => {
        injected.push(copy(params));
        for (const frame of frames) if (params.allFrames || frame.top) frame.load(params.file);
      },
    },
    windows: { getLastFocused: async () => ({ id: 1, focused: true }), get: async () => ({}) },
    storage: { local: { get: async () => ({}), set: async () => {} } },
  };
  class WebSocket { constructor() { this.readyState = 1; } send() {} }
  const context = vm.createContext({ browser, WebSocket, URL,
    Date: class extends Date { static now() { return now; } },
    console: { log() {}, error() {} },
    setTimeout: (fn, delay, ...args) => {
      const id = ++nextTimer;
      if (delay === 250) timers.set(id, { at: now + delay, fn: () => fn(...args) });
      return id;
    },
    clearTimeout: id => timers.delete(id), setInterval: () => 0, clearInterval() {},
  });
  vm.runInContext(source('background/background.js'), context);
  context.ownAgentTab(1, 'client-a');
  // Exercise real preparation dispatch while isolating geometry / content command implementation.
  context.forwardToContent = async (action, params) => {
    forwarded.push([action, copy(params)]);
    return { ok: true, screenX: 10, screenY: 10 };
  };
  const frame = ({ top = false, url = 'https://child.test/', install = true } = {}) => {
    const listeners = new Map(), replies = [];
    const window = {
      addEventListener(type, fn, options) {
        assert.equal(options.capture, true);
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(fn);
      },
      getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
    };
    window.top = top ? window : {};
    const document = { body: {}, activeElement: null, hasFocus: () => true,
      querySelector: () => null, querySelectorAll: () => [] };
    const sender = { id: runtime.id, tab: { id: 1 }, frameId: top ? 0 : frames.length + 1, url };
    const frameContext = vm.createContext({ window, document, console, crypto: webcrypto, Event,
      CSS: { escape: x => x }, Date: class extends Date { static now() { return now; } },
      browser: { runtime: {
        onMessage: { addListener: fn => replies.push(fn) },
        sendMessage: msg => {
          if (msg.action === 'trustedTabInput') messages.push(copy(msg));
          return Promise.resolve(runtime.onMessage.emit(msg, sender)[0]);
        },
      } },
    });
    const f = { top, frameId: sender.frameId, document, load: file => vm.runInContext(source(file), frameContext),
      send: async msg => {
        for (const reply of replies) {
          const result = reply(msg, {}, () => {});
          if (result !== undefined && result !== false) return result;
        }
        return null;
      },
      input: (type, key, isTrusted = true, target = { tagName: 'A' }) => {
        for (const listener of listeners.get(type) || []) listener({ type, key, isTrusted, target });
      },
      activity: () => new Promise(resolve => replies.at(-1)({ action: 'userActivity' }, {}, resolve)),
    };
    frames.push(f);
    if (install) for (const entry of manifest.content_scripts) {
      if (!top && !entry.all_frames) continue;
      if (/^about:/.test(url) && !entry.match_about_blank) continue;
      for (const file of entry.js) f.load(file);
    }
    return f;
  };
  return { context, frame, frames, messages, injected, forwarded, runtime, browser,
    busy: async () => copy(await context.userActivity(1)),
    command: async params => copy(await context.handleCommand({ ownerId: 'client-a', tabId: 1,
      includeSnapshot: false, ...params })),
    tick: ms => {
      now += ms;
      for (const [id, timer] of timers) if (timer.at <= now) { timers.delete(id); timer.fn(); }
    },
    inputMessage: (msg, sender) => runtime.onMessage.emit(msg, sender),
    open: async () => {
      const tab = { id: nextTab++, openerTabId: 1, active: false, url: 'https://opened.test/' };
      tabs.set(tab.id, tab);
      browser.tabs.onCreated.emit(tab);
      return (await context.getOpenTabs('client-a')).tabs.find(t => t.id === tab.id).owned;
    },
    prepare: params => context.dispatchCommand({ tabId: 1, ...params }),
  };
}

async function genuineFrameInput() {
  for (const url of ['https://parent.test/frame', 'https://other.test/frame', 'about:blank', 'about:srcdoc']) {
    for (const [type, key] of [['pointerdown'], ['keydown', 'Control'], ['keydown', 'Meta']]) {
      const h = harness(), child = h.frame({ url });
      child.input(type, key);
      assert.equal(await h.open(), false, `${url}: trusted ${type} ${key || 'middle-click'} is user-owned`);
    }
  }
  const h = harness(), child = h.frame();
  child.input('pointerdown', undefined, false);
  assert.equal(await h.open(), true, 'synthetic frame input is not user input');
  assert.deepEqual(h.messages, []);
}

async function startupAndDuplicateInjection() {
  const h = harness();
  const child = h.frame({ install: false }); // existing eligible frame on extension reload
  await h.context.injectAllTabs();
  const observer = h.injected.find(p => p.file === '/content/frame-input.js');
  assert.ok(observer?.allFrames && observer.matchAboutBlank);
  await h.context.injectAllTabs();
  child.load('content/frame-input.js');
  child.load('content/content.js'); // a later frame command must not double-report
  child.input('pointerdown');
  assert.equal(h.messages.length, 1);
  assert.equal(await h.open(), false);
  assert.equal((await child.activity()).active, true, 'full content user_active still sees frame input');
  const reverse = harness(), existing = reverse.frame({ install: false });
  existing.load('content/content.js');
  existing.load('content/frame-input.js');
  existing.input('pointerdown');
  assert.equal(reverse.messages.length, 1, 'observer installed after full content does not double-report');
  const blocked = harness(), blockedTop = blocked.frame({ top: true, install: false });
  const inject = blocked.browser.tabs.executeScript;
  blocked.browser.tabs.executeScript = (id, params) => params.file.endsWith('frame-input.js')
    ? Promise.reject(new Error('inaccessible frame')) : inject(id, params);
  await blocked.context.injectAllTabs();
  blockedTop.input('pointerdown');
  assert.equal(blocked.messages.length, 1, 'observer injection failure does not break top-frame injection');
  const top = harness(), page = top.frame({ top: true });
  page.input('pointerdown');
  assert.equal(top.messages.length, 1, 'top frame continues reporting through full content only');
}

async function announcedAgentInput() {
  for (const newFrame of [false, true]) {
    const h = harness();
    let child = newFrame ? null : h.frame();
    await h.prepare({ action: 'screenPoint', click: true, x: 10, y: 10, modifiers: ['Control', 'Meta'] });
    child ||= h.frame(); // input preparation also covers frames created afterwards
    child.input('keydown', 'Control');
    child.input('keydown', 'Meta');
    child.input('pointerdown');
    child.input('mousedown');
    assert.equal(await h.open(), true, 'announced OS pointer/modifiers do not change ownership');
    child.input('keydown', 'Shift');
    assert.equal(await h.open(), false, 'an unrelated modifier still counts');
  }
  for (const [prepare, unrelated] of [
    [{ action: 'agentInput', ms: 3000 }, 'mousedown'],
    [{ action: 'screenPoint', click: true, x: 10, y: 10 }, 'keydown'],
    [{ action: 'screenPoint', click: true, x: 10, y: 10 }, 'wheel'],
  ]) {
    const h = harness(), child = h.frame();
    await h.prepare(prepare);
    child.input(unrelated, 'x');
    assert.equal(await h.open(), false, `unrelated ${unrelated} is not suppressed`);
  }
  const newKeys = harness();
  await newKeys.prepare({ action: 'agentInput', ms: 3000 });
  newKeys.frame().input('keydown', 'x');
  assert.equal(await newKeys.open(), true, 'new frames also respect announced keyboard input');
  assert.ok(!JSON.stringify(newKeys.messages).includes('"x"'), 'typed characters are never sent');
  const h = harness(), child = h.frame();
  await h.prepare({ action: 'agentInput', ms: 3000 });
  child.input('keydown', 'x');
  child.input('paste');
  assert.equal(await h.open(), true);
  child.load('content/content.js');
  child.input('keydown', 'x');
  assert.equal((await child.activity()).active, true, 'ownership filtering does not weaken user_active');
  h.tick(3000);
  child.input('keydown', 'x');
  assert.equal(await h.open(), false, 'keyboard exclusion expires');
  const click = harness(), clicked = click.frame();
  await click.prepare({ action: 'screenPoint', click: true, x: 10, y: 10 });
  click.tick(2000);
  clicked.input('pointerdown');
  assert.equal(await click.open(), false, 'pointer exclusion expires at the existing 2 s boundary');
  const capped = harness(), cappedFrame = capped.frame();
  await capped.prepare({ action: 'agentInput', ms: Infinity });
  capped.tick(60000);
  cappedFrame.input('keydown', 'x');
  assert.equal(await capped.open(), false, 'keyboard suppression has the existing finite 60 s cap');
}

async function failedPreparationAndSenderValidation() {
  for (const params of [{ action: 'agentInput', ms: 3000 }, { action: 'screenPoint', click: true, x: 1, y: 1 }]) {
    const h = harness(), child = h.frame();
    h.context.forwardToContent = async () => ({ error: 'not ready' });
    assert.ok((await h.prepare(params)).error);
    child.input(params.action === 'agentInput' ? 'keydown' : 'pointerdown');
    assert.equal(await h.open(), false, 'failed preparation must not suppress user input');
  }
  const read = harness(), readFrame = read.frame();
  await read.prepare({ action: 'screenPoint', x: 1, y: 1 });
  readFrame.input('pointerdown');
  assert.equal(await read.open(), false, 'read-only measurement does not announce an OS click');
  const h = harness();
  h.inputMessage({ action: 'trustedTabInput', tabId: 1, at: 100000, type: 'pointerdown' }, {});
  h.inputMessage({ action: 'trustedTabInput', tabId: 1, at: 100000, type: 'pointerdown' },
    { id: 'other-extension', tab: { id: 1 } });
  assert.equal(await h.open(), true, 'only the extension content sender can identify an input tab');
}

async function frameActivityTransitions() {
  for (const url of ['https://parent.test/frame', 'https://other.test/frame', 'about:blank', 'about:srcdoc']) {
    const h = harness(), child = h.frame({ url });
    const field = { tagName: 'INPUT', value: 'user draft', isConnected: true };
    child.document.activeElement = field;
    child.input('keydown', 'x', true, field);
    assert.equal((await h.busy()).reason, 'input', url);
    h.tick(2999);
    assert.equal((await h.busy()).sinceMs, 2999);
    h.tick(1);
    assert.equal((await h.busy()).reason, 'typing', 'typing outlives the input window');
    h.tick(60000);
    const forwarded = h.forwarded.length;
    for (const action of ['type', 'navigate', 'closeTab', 'agentInput', 'screenPoint', 'focusState']) {
      const result = await h.command({ action, text: 'agent', selector: 'input', url: 'https://new.test/' });
      assert.equal(result.code, 'user_active', action);
      assert.equal(result.retryable, true);
    }
    assert.equal(h.forwarded.length, forwarded, 'no command or keystroke reaches the user field');
    assert.equal(field.value, 'user draft');
    assert.equal((await h.context.getOpenTabs('client-a')).tabs.find(t => t.id === 1).owned, true);
    child.document.hasFocus = () => false;
    assert.equal(await h.busy(), null, 'temporarily unfocused document is inactive');
    child.document.hasFocus = () => true;
    assert.equal((await h.busy())?.reason, 'typing', 'focus return restores typing without another input event');
    assert.equal((await h.command({ action: 'type', text: 'agent' })).code, 'user_active');
    assert.equal(h.forwarded.length, forwarded, 'focus round trip does not admit writes to the draft');
    field.value = '';
    assert.equal(await h.busy(), null, 'empty field releases');
    assert.equal((await h.command({ action: 'type', text: 'agent' })).ok, true);
    for (const kind of ['blur', 'document blur', 'removed', 'empty editor']) {
      const editor = { tagName: 'DIV', isContentEditable: true, textContent: 'draft', isConnected: true };
      child.document.hasFocus = () => true;
      child.document.activeElement = editor;
      child.input('paste', undefined, true, editor);
      h.tick(3000);
      assert.equal((await h.busy()).reason, 'typing');
      if (kind === 'blur') child.document.activeElement = null;
      if (kind === 'document blur') child.document.hasFocus = () => false;
      if (kind === 'removed') editor.isConnected = false;
      if (kind === 'empty editor') editor.textContent = '  ';
      assert.equal(await h.busy(), null, kind);
    }
  }
  for (const type of ['keydown', 'mousedown', 'pointerdown', 'touchstart', 'wheel', 'paste']) {
    const h = harness(), child = h.frame();
    child.input(type, 'x', false);
    assert.equal(await h.busy(), null, 'synthetic input does not block');
    child.input(type, 'x');
    assert.equal((await h.busy()).reason, 'input');
    h.tick(3000);
    assert.equal(await h.busy(), null, type + ' expires exactly at 3 s');
  }
  for (const installAfter of [false, true]) {
    const h = harness();
    let child = installAfter ? null : h.frame();
    await h.prepare({ action: 'agentInput', ms: 3000 });
    child ||= h.frame();
    child.input('keydown', 'x');
    assert.equal(await h.busy(), null, 'announced agent keys do not create child activity');
    child.input('wheel');
    assert.equal((await h.busy()).input, 'wheel', 'unrelated real input still blocks');
  }
  for (const files of [
    ['content/frame-input.js', 'content/content.js'],
    ['content/content.js', 'content/frame-input.js'],
  ]) {
    const h = harness(), child = h.frame({ install: false });
    for (const file of files) child.load(file);
    const host = { tagName: 'MAIL-COMPOSER', isConnected: true };
    child.document.activeElement = host;
    child.input('paste', undefined, true, host);
    h.tick(3000);
    assert.equal((await h.busy()).reason, 'typing', 'co-installed engines preserve shadow-host semantics');
    assert.equal(h.messages.length, 1, 'co-installed engines report only once');
  }
  for (const newFrame of [false, true]) {
    const h = harness();
    let child = newFrame ? null : h.frame();
    await h.prepare({ action: 'screenPoint', click: true, x: 10, y: 10, modifiers: ['Control'] });
    child ||= h.frame();
    child.input('pointerdown');
    child.input('keydown', 'Control');
    assert.equal(await h.busy(), null, 'announced pointer and modifiers are not user activity');
    child.input('keydown', 'Shift');
    assert.equal((await h.busy()).input, 'keydown', 'unrelated modifiers still block');
  }
  const stale = harness(), old = stale.frame();
  const field = { tagName: 'TEXTAREA', value: 'draft', isConnected: true };
  old.document.activeElement = field;
  old.input('paste', undefined, true, field);
  stale.tick(3000);
  assert.equal((await stale.busy()).reason, 'typing');
  old.document.hasFocus = () => false;
  assert.equal(await stale.busy(), null);
  const replacement = stale.frame();
  replacement.frameId = old.frameId;
  replacement.document.activeElement = { tagName: 'INPUT', value: 'new document draft', isConnected: true };
  stale.frames.splice(stale.frames.indexOf(old), 1);
  assert.equal(await stale.busy(), null, 'document token rejects a document replaced while unfocused');
  old.document.hasFocus = () => true;
  stale.frames.splice(0, 1, old);
  assert.equal(await stale.busy(), null, 'a discarded document record cannot resurrect its stale draft');
  const removed = harness(), detached = removed.frame();
  detached.input('pointerdown');
  removed.frames.length = 0;
  assert.equal((await removed.busy()).reason, 'input', 'lost frames retain only the bounded input pause');
  removed.tick(3000);
  assert.equal(await removed.busy(), null);
  const hung = harness(), unresponsive = hung.frame();
  unresponsive.input('wheel');
  hung.tick(3000);
  unresponsive.send = () => new Promise(() => {});
  const query = hung.busy();
  await new Promise(resolve => setImmediate(resolve));
  hung.tick(250);
  assert.equal(await query, null, 'unresponsive frame cannot leave an indefinite typing lock');

  const race = harness(), current = race.frame();
  current.input('wheel');
  race.tick(3000);
  current.send = async () => {
    current.input('pointerdown');
    return { active: false };
  };
  await race.busy();
  assert.equal((await race.busy()).input, 'pointerdown', 'old query cannot clear a newer event');
}

(async () => {
  await genuineFrameInput();
  await startupAndDuplicateInjection();
  await announcedAgentInput();
  await failedPreparationAndSenderValidation();
  await frameActivityTransitions();
  console.log('frame_user_input: ok');
})().catch(error => { console.error(error); process.exit(1); });
