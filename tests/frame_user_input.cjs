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
  const frames = [], messages = [], injected = [];
  const tabs = new Map([[1, { id: 1, active: true, url: 'https://parent.test/', windowId: 1 }]]);
  const runtime = { id: 'kitsune-test', onMessage: event(), onStartup: event(), onInstalled: event() };
  const browser = {
    runtime,
    tabs: {
      onCreated: event(), onRemoved: event(), onUpdated: event(), onActivated: event(),
      query: async () => [...tabs.values()], get: async id => tabs.get(id), getZoom: async () => 1,
      sendMessage: async () => null,
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
    console: { log() {}, error() {} }, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  });
  vm.runInContext(source('background/background.js'), context);
  context.ownAgentTab(1, 'client-a');
  // Exercise real preparation dispatch while isolating geometry / content command implementation.
  context.forwardToContent = async () => ({ ok: true, screenX: 10, screenY: 10 });
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
    const sender = { id: runtime.id, tab: { id: 1 }, frameId: frames.length + 1, url };
    const frameContext = vm.createContext({ window, document, console, crypto: webcrypto, Event,
      CSS: { escape: x => x }, Date: class extends Date { static now() { return now; } },
      browser: { runtime: {
        onMessage: { addListener: fn => replies.push(fn) },
        sendMessage: msg => {
          if (msg.action === 'trustedTabInput') messages.push(copy(msg));
          return Promise.resolve(runtime.onMessage.emit(msg, sender));
        },
      } },
    });
    const f = { top, load: file => vm.runInContext(source(file), frameContext),
      input: (type, key, isTrusted = true) => {
        for (const listener of listeners.get(type) || []) listener({ type, key, isTrusted, target: { tagName: 'A' } });
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
  return { context, frame, messages, injected, runtime, browser,
    tick: ms => { now += ms; },
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

(async () => {
  await genuineFrameInput();
  await startupAndDuplicateInjection();
  await announcedAgentInput();
  await failedPreparationAndSenderValidation();
  console.log('frame_user_input: ok');
})().catch(error => { console.error(error); process.exit(1); });
