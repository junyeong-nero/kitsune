// Run with: node tests/pointer_gestures.cjs (no browser or npm dependencies).
// #351: a click can hold its button (holdMs) for long-press controls, and a hover can keep
// moving over its target (durationMs, steps) for hover-intent controls. The background times
// the pauses, since a hidden tab's page timers fire after 1 s at the earliest.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    Object.assign(this, { clientX: init.clientX, clientY: init.clientY, button: init.button || 0,
      buttons: init.buttons || 0 });
  }
}

class Element extends EventTarget {
  constructor(tagName, id) {
    super();
    Object.assign(this, { tagName, id, textContent: id, isConnected: true, events: [] });
    for (const type of ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'pointermove', 'mousemove',
      'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'contextmenu']) {
      this.addEventListener(type, event => this.events.push([event.type, event.clientX, event.clientY,
        event.buttons, clock.now]));
    }
  }
  matches() { return false; }
  getAttribute() { return null; }
  hasAttribute() { return false; }
  closest() { return null; }
  getClientRects() { return [{}]; }
  scrollIntoView() {}
  // 200x40 at (100, 50): the center is (200, 70).
  getBoundingClientRect() { return { x: 100, y: 50, width: 200, height: 40 }; }
}

// A page clock the pauses advance, so held_ms and hovered_ms are exact.
const clock = { now: 0 };

// pauses: 'background' answers pointerPause like background.js; 'none' leaves it unanswered, as
// an older background would, so the page times the pause.
function load(pauses = 'background') {
  const elements = { '#press': new Element('BUTTON', 'press'), '#tile': new Element('DIV', 'tile') };
  const asked = [];
  const timers = [];
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, PointerEvent: MouseEvent, MessageChannel,
    CSS: { escape: x => x }, navigator: { platform: 'Linux x86_64' }, performance: { now: () => clock.now },
    setTimeout: (fn, ms) => { timers.push(ms); clock.now += ms; fn(); return 0; },
    window: { PointerEvent: MouseEvent, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: {
      body: {},
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
      elementFromPoint: () => elements['#press'],
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: async msg => {
        if (msg.action !== 'pointerPause') return null;
        asked.push(msg.ms);
        if (pauses === 'none') return undefined;
        clock.now += msg.ms;
        return { ok: true };
      },
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
  return { send, elements, asked, timers };
}

const PRESS_TYPES = ['pointerdown', 'mousedown', 'pointerup', 'mouseup'];

async function testClickHoldsTheButton() {
  const { send, elements, asked, timers } = load();
  clock.now = 1000;
  const result = await send({ action: 'click', selector: '#press', holdMs: 600 });
  assert.equal(result.ok, true, JSON.stringify(result));
  assert.equal(result.held_ms, 600);
  assert.deepEqual(asked, [600], 'the background times the hold');
  assert.deepEqual(timers, [], 'no page timer');
  const events = elements['#press'].events.filter(([type]) => PRESS_TYPES.includes(type) || type === 'click');
  // Pressed at 1000 with the button held; released and clicked 600 ms later.
  assert.deepEqual(events.map(([type, x, y, buttons, at]) => [type, x, y, buttons, at]), [
    ['pointerdown', 200, 70, 1, 1000], ['mousedown', 200, 70, 1, 1000],
    ['pointerup', 200, 70, 0, 1600], ['mouseup', 200, 70, 0, 1600], ['click', 200, 70, 0, 1600]]);

  // A right-button hold opens the context menu after the release, as its click would.
  elements['#press'].events = [];
  const right = await send({ action: 'click', selector: '#press', holdMs: 700, button: 'right' });
  assert.equal(right.held_ms, 700);
  assert.deepEqual(elements['#press'].events.filter(([type]) => type !== 'click').slice(-5).map(([type]) => type),
    [...PRESS_TYPES, 'contextmenu']);

  // Without holdMs (or with 0) the click is as before: no pause and no held_ms.
  elements['#press'].events = [];
  for (const holdMs of [undefined, 0]) {
    const plain = await send({ action: 'click', selector: '#press', holdMs });
    assert.equal(plain.ok, true);
    assert.equal(plain.held_ms, undefined);
  }
  assert.deepEqual(asked, [600, 700]);

  // Out-of-range or non-integer holds are refused before anything is dispatched.
  elements['#press'].events = [];
  for (const holdMs of [-1, 10001, 1.5, '600', true]) {
    assert.deepEqual(await send({ action: 'click', selector: '#press', holdMs }),
      { error: 'holdMs must be an integer from 0 to 10000' }, String(holdMs));
  }
  assert.deepEqual(elements['#press'].events, []);
  assert.equal((await send({ action: 'click', selector: '#press', holdMs: 10000 })).held_ms, 10000);
}

async function testHoldWithoutTheBackgroundUsesAPageTimer() {
  const { send, asked, timers } = load('none');
  clock.now = 0;
  const result = await send({ action: 'click', selector: '#press', holdMs: 650 });
  assert.equal(result.held_ms, 650);
  assert.deepEqual(asked, [650]);
  assert.deepEqual(timers, [650]);
}

async function testHoverKeepsMovingAcrossTheTarget() {
  const { send, elements, asked } = load();
  const tile = elements['#tile'];
  clock.now = 5000;
  const result = await send({ action: 'hover', selector: '#tile', durationMs: 400 });
  assert.equal(result.ok, true, JSON.stringify(result));
  assert.equal(result.moves, 6, 'the arrival move and 5 more by default');
  assert.equal(result.hovered_ms, 400);
  assert.deepEqual(asked, [80, 80, 80, 80, 80]);
  // Entered once on arrival; then moves cross the middle half of the tile, left to right, at
  // the vertical center, with no button held.
  assert.deepEqual(tile.events.filter(([type]) => type === 'mouseenter').length, 1);
  const moves = tile.events.filter(([type]) => type === 'mousemove');
  assert.deepEqual(moves.map(([, x, y, buttons, at]) => [x, y, buttons, at]), [
    [150, 70, 0, 5000], [170, 70, 0, 5080], [190, 70, 0, 5160], [210, 70, 0, 5240], [230, 70, 0, 5320],
    [250, 70, 0, 5400]]);
  assert.equal(tile.events.filter(([type]) => type === 'pointermove').length, 6);

  // steps sets the count; without durationMs the moves are a task apart, with no pause.
  tile.events = [];
  asked.length = 0;
  const quick = await send({ action: 'hover', selector: '#tile', steps: 3 });
  assert.equal(quick.moves, 4);
  assert.equal(quick.hovered_ms, 0);
  assert.deepEqual(asked, []);
  assert.deepEqual(tile.events.filter(([type]) => type === 'mousemove').map(([, x]) => Math.round(x * 100) / 100),
    [150, 183.33, 216.67, 250]);

  // A plain hover moves once, at the center, and reports no moves.
  tile.events = [];
  const plain = await send({ action: 'hover', selector: '#tile' });
  assert.deepEqual(Object.keys(plain).sort(), ['ok', 'popupWatch', 'tag']);
  assert.deepEqual(tile.events.filter(([type]) => type === 'mousemove').map(([, x]) => x), [200]);

  // Bounds are checked before the pointer moves.
  tile.events = [];
  for (const [extra, error] of [
    [{ durationMs: 10001 }, 'durationMs must be an integer from 0 to 10000'],
    [{ durationMs: -5 }, 'durationMs must be an integer from 0 to 10000'],
    [{ durationMs: 400, steps: 0 }, 'steps must be an integer from 1 to 50'],
    [{ steps: 51 }, 'steps must be an integer from 1 to 50'],
    [{ steps: 2.5 }, 'steps must be an integer from 1 to 50'],
  ]) {
    assert.deepEqual(await send({ action: 'hover', selector: '#tile', ...extra }), { error }, JSON.stringify(extra));
  }
  assert.deepEqual(tile.events, []);
}

// The background answers pointerPause after the pause, capped at 10 s.
async function testBackgroundTimesThePause() {
  const listeners = [];
  const delays = [];
  const noop = { addListener() {}, removeListener() {} };
  const browser = {
    tabs: { onCreated: noop, onUpdated: noop, onRemoved: noop, onActivated: noop },
    storage: { local: { get: async () => ({}), set: async () => {} } },
    runtime: { onMessage: { addListener: fn => listeners.push(fn) }, onStartup: noop, onInstalled: noop },
  };
  const context = vm.createContext({
    browser, console: { log() {}, error() {} }, WebSocket: class { close() {} },
    setTimeout: (fn, ms, ...args) => { delays.push(ms); fn(...args); return 0; }, clearTimeout() {},
    setInterval: () => 0, clearInterval() {},
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  const ask = msg => {
    for (const listener of listeners) {
      const reply = listener(msg, { tab: { id: 2 } });
      if (reply !== undefined) return reply;
    }
    return undefined;
  };
  delays.length = 0;
  assert.deepEqual({ ...await ask({ action: 'pointerPause', ms: 600 }) }, { ok: true });
  assert.deepEqual({ ...await ask({ action: 'pointerPause', ms: 60000 }) }, { ok: true });
  assert.deepEqual({ ...await ask({ action: 'pointerPause', ms: 'x' }) }, { ok: true });
  assert.deepEqual(delays, [600, 10000, 0]);
}

(async () => {
  await testClickHoldsTheButton();
  await testHoldWithoutTheBackgroundUsesAPageTimer();
  await testHoverKeepsMovingAcrossTheTarget();
  await testBackgroundTimesThePause();
  console.log('Pointer hold and sustained hover checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
