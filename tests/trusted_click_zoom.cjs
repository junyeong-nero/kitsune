// Run with: node tests/trusted_click_zoom.cjs (no browser or npm dependencies).
// #307: browser_trusted_click converts its viewport point to a desktop point with the tab's
// page zoom, and refuses when the element at that point is not the target or inside it.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const copy = value => JSON.parse(JSON.stringify(value));

class Element {
  constructor(tagName, attrs = {}, children = [], text = '') {
    Object.assign(this, { tagName, attrs, children, parentElement: null, parentNode: null, isConnected: true,
      textContent: text, innerText: text, scrolled: 0 });
    for (const child of children) child.parentElement = child.parentNode = this;
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  // Selectors used here: *, tag and #id.
  matches(selector) {
    return selector.split(',').map(s => s.trim()).some(s =>
      s === '*' || s === this.tagName.toLowerCase() || s === `#${this.attrs.id}`);
  }
  descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
  querySelectorAll(selector) { return this.descendants().filter(el => el.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) {
    for (let el = this; el; el = el.parentElement) if (el.matches(selector)) return el;
    return null;
  }
  getClientRects() { return [{}]; }
  // 100x20 at (40, 300) in the page's CSS pixels: the centre is (90, 310).
  getBoundingClientRect() { return { x: 40, y: 300, width: 100, height: 20, left: 40, top: 300, right: 140, bottom: 320 }; }
  scrollIntoView() { this.scrolled++; }
}

// The target #go holds a <span>; #cover is an overlay; #host has a shadow root with a <b>;
// #check is a custom checkbox input under its <label for=check>, which holds a <span>.
function page() {
  const label = new Element('SPAN', {}, [], 'Go');
  const go = new Element('BUTTON', { id: 'go' }, [label]);
  const cover = new Element('DIV', { id: 'cover' }, [], 'Cookie banner');
  const inner = new Element('B', {}, [], 'Shadow');
  const host = new Element('MY-BUTTON', { id: 'host' });
  const shadow = {
    nodeType: 11, host, children: [inner],
    querySelectorAll: selector => [inner].filter(el => el.matches(selector)),
    querySelector: selector => [inner].find(el => el.matches(selector)) || null,
    elementFromPoint: () => inner,
  };
  inner.parentNode = shadow;
  host.openOrClosedShadowRoot = shadow;
  const check = new Element('INPUT', { id: 'check', type: 'checkbox' });
  const box = new Element('SPAN', {}, [], 'Remember me');
  const checkLabel = new Element('LABEL', { for: 'check' }, [box]);
  check.labels = [checkLabel];
  const body = new Element('BODY', {}, [go, cover, host, check, checkLabel]);
  return { body, go, label, cover, host, inner, check, checkLabel, box };
}

function load(dom, { at, screen = { mozInnerScreenX: 100, mozInnerScreenY: 80 } } = {}) {
  let listener;
  const points = [];
  const context = vm.createContext({
    console, crypto: webcrypto, Event, CSS: { escape: x => x }, setTimeout, MessageChannel,
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { ...screen, devicePixelRatio: 2, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: {
      title: 'Zoomed', body: dom.body, activeElement: dom.body,
      documentElement: { scrollWidth: 100, scrollHeight: 100, appendChild() {} },
      createElement: () => ({ remove() {} }),
      getElementById: id => dom.body.querySelector('#' + id),
      querySelector: selector => dom.body.querySelector(selector),
      querySelectorAll: selector => dom.body.querySelectorAll(selector),
      elementFromPoint: (x, y) => { points.push([x, y]); return at; },
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, result => resolve(copy(result))));
  return { send, points };
}

async function testZoomScalesTheDesktopPoint() {
  // Firefox reports mozInnerScreenX/Y and the element rect in the page's zoomed CSS pixels:
  // (100 + 90, 80 + 310) of them, each spanning `zoom` desktop pixels.
  for (const zoom of [1, 1.5, 0.8]) {
    const dom = page();
    const { send, points } = load(dom, { at: dom.go });
    const point = await send({ action: 'screenPoint', selector: '#go', click: true, zoom });
    assert.equal(point.error, undefined, JSON.stringify(point));
    assert.deepEqual([point.viewportX, point.viewportY], [90, 310], `zoom ${zoom}`);
    assert.deepEqual([point.screenX, point.screenY], [190 * zoom, 390 * zoom], `zoom ${zoom}`);
    assert.equal(point.zoom, zoom);
    assert.equal(point.devicePixelRatio, 2);
    assert.deepEqual(points, [[90, 310]], 'the click point is hit-tested in viewport pixels');
  }

  // Raw viewport x/y convert the same way; they name no target, so nothing is hit-tested.
  const dom = page();
  const raw = load(dom, { at: dom.cover });
  const point = await raw.send({ action: 'screenPoint', x: 10, y: 20, click: true, zoom: 1.5 });
  assert.deepEqual([point.screenX, point.screenY, point.zoom], [165, 150, 1.5]);
  assert.deepEqual(raw.points, []);

  // Without mozInnerScreenX/Y, the window-chrome estimate is in the same zoomed pixels.
  const fallback = load(page(), { at: null, screen: { screenX: 10, screenY: 20, outerWidth: 820,
    innerWidth: 800, outerHeight: 700, innerHeight: 600 } });
  const estimated = await fallback.send({ action: 'screenPoint', x: 10, y: 20, click: true, zoom: 0.8 });
  assert.deepEqual([estimated.screenX, estimated.screenY], [(10 + 10 + 10) * 0.8, (20 + 100 + 20) * 0.8]);

  // An older background sends no zoom: the point is taken as unzoomed, and says so.
  const unzoomed = await load(page(), { at: null }).send({ action: 'screenPoint', x: 10, y: 20 });
  assert.deepEqual([unzoomed.screenX, unzoomed.screenY, unzoomed.zoom], [110, 100, 1]);
}

async function testClickRefusesWhenTheTargetIsNotAtThePoint() {
  const dom = page();
  const { send } = load(dom, { at: dom.cover });
  const covered = await send({ action: 'screenPoint', selector: '#go', click: true, zoom: 1.5 });
  assert.equal(covered.code, 'target_not_at_point', JSON.stringify(covered));
  assert.equal(covered.retryable, true);
  assert.match(covered.hint, /state\.hit/);
  assert.equal(covered.screenX, undefined, 'no desktop point to click');
  assert.deepEqual([covered.state.viewportX, covered.state.viewportY], [90, 310]);
  assert.equal(covered.state.hit.name, 'Cookie banner', 'the hit summary carries a ref, role and name');
  assert.match(covered.state.hit.ref, /^s\d+e\d+$/);
  assert.equal(covered.state.hit.role, 'div');

  // Nothing at the point (scrolled off, or outside the viewport) refuses too.
  const nothing = await load(page(), { at: null }).send({ action: 'screenPoint', selector: '#go', click: true });
  assert.equal(nothing.code, 'target_not_at_point');
  assert.equal(nothing.state.hit, null);

  // A read reports the point without hit-testing it.
  const read = load(page(), { at: dom.cover });
  assert.equal((await read.send({ action: 'screenPoint', selector: '#go' })).screenX, 190);
  assert.deepEqual(read.points, []);
}

async function testDescendantsOfTheTargetCount() {
  // A light-DOM child of the target.
  const dom = page();
  const child = await load(dom, { at: dom.label }).send({ action: 'screenPoint', selector: '#go', click: true });
  assert.equal(child.error, undefined, JSON.stringify(child));
  // document.elementFromPoint stops at the host; its shadow root reports the <b> inside it.
  const shadow = page();
  const inShadow = await load(shadow, { at: shadow.host }).send({ action: 'screenPoint', selector: '#host', click: true });
  assert.equal(inShadow.error, undefined, JSON.stringify(inShadow));
  // A custom checkbox's input sits under its label; the browser sends the label's click to it.
  const custom = page();
  const labelled = await load(custom, { at: custom.box }).send({ action: 'screenPoint', selector: '#check', click: true });
  assert.equal(labelled.error, undefined, JSON.stringify(labelled));
  // Another control's label is not the target's.
  const other = page();
  const notLabel = await load(other, { at: other.box }).send({ action: 'screenPoint', selector: '#go', click: true });
  assert.equal(notLabel.code, 'target_not_at_point');
  // The target's parent is not the target.
  const parent = page();
  const outside = await load(parent, { at: parent.body }).send({ action: 'screenPoint', selector: '#go', click: true });
  assert.equal(outside.code, 'target_not_at_point');
}

// The background reads the tab's zoom and hands it to the content script with the point request.
function background(zoomOf) {
  const sent = [], activations = [];
  const tabs = {
    1: { id: 1, windowId: 1, active: true, url: 'https://example.com/user' },
    2: { id: 2, windowId: 1, active: false, url: 'https://example.com/agent' },
  };
  const noop = { addListener() {}, removeListener() {} };
  const browser = {
    tabs: {
      onCreated: noop, onUpdated: noop, onRemoved: noop, onActivated: noop,
      get: async id => { if (!tabs[id]) throw new Error(`Invalid tab ID: ${id}`); return { ...tabs[id] }; },
      query: async query => Object.values(tabs).filter(tab => tab.active && tab.windowId === query.windowId),
      update: async (id, props) => {
        if (props.active) {
          Object.values(tabs).forEach(tab => { tab.active = tab.id === id; });
          activations.push(id);
        }
        return { ...tabs[id] };
      },
      getZoom: async id => zoomOf(id),
    },
    windows: { get: async id => ({ id, left: 0, top: 25, width: 1440, height: 875 }) },
    storage: { local: { get: async () => ({}), set: async () => {} } },
    runtime: { onMessage: noop, onStartup: noop, onInstalled: noop },
  };
  const context = vm.createContext({
    browser, console: { log() {}, error() {} }, WebSocket: class { close() {} },
    setTimeout: fn => { fn(); return 0; }, clearTimeout() {},
    setInterval: () => 0, clearInterval() {},
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  context.userActiveError = async () => null;
  context.getActiveTabId = async () => 1;
  context.forwardToContent = async (action, params) => {
    sent.push([action, copy(params)]);
    return { screenX: 1, screenY: 2, viewportX: 1, viewportY: 2, zoom: params.zoom };
  };
  return { context, sent, activations, tabs };
}

async function testBackgroundPassesTheTabZoom() {
  const h = background(id => ({ 1: 0.8, 2: 1.5 })[id]);
  const point = copy(await h.context.screenPointForClick({ tabId: 2, selector: '#go', click: true }));
  assert.deepEqual(h.sent, [['screenPoint', { tabId: 2, selector: '#go', click: true, zoom: 1.5 }]]);
  assert.deepEqual([point.zoom, point.tabId, point.previousTabId], [1.5, 2, 1]);
  // The tab's window, which the bridge requires at the click point (#336).
  assert.deepEqual(point.window, { left: 0, top: 25, width: 1440, height: 875 });

  // A read uses the zoom of the tab it resolves to.
  await h.context.dispatchCommand({ action: 'screenPoint', selector: '#go' });
  assert.deepEqual(h.sent[1], ['screenPoint', { selector: '#go', tabId: 1, zoom: 0.8 }]);

  // A zoom that cannot be read sends nothing, and the user's tab is shown again.
  const failing = background(() => { throw new Error('Invalid tab ID: 2'); });
  const error = copy(await failing.context.screenPointForClick({ tabId: 2, click: true }));
  assert.ok(error.error, JSON.stringify(error));
  assert.deepEqual(failing.sent, []);
  assert.deepEqual(failing.activations, [2, 1]);
}

(async () => {
  await testZoomScalesTheDesktopPoint();
  await testClickRefusesWhenTheTargetIsNotAtThePoint();
  await testDescendantsOfTheTargetCount();
  await testBackgroundPassesTheTabZoom();
})().then(() => console.log('Trusted click zoom checks passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
