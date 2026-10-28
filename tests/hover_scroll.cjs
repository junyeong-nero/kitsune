// Run with: node tests/hover_scroll.cjs (no browser or npm dependencies).
// #220: browser_hover scrolls its target into view, through every scrolling ancestor, before
// it dispatches, and the events carry the point measured after that scroll.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    Object.assign(this, { clientX: init.clientX, clientY: init.clientY });
  }
}

// The last item of an inner scrolling list, below the list's visible end until scrolled into view.
class Item extends EventTarget {
  constructor() {
    super();
    Object.assign(this, { tagName: 'LI', id: 'last', textContent: 'last', isConnected: true,
      top: 2400, scrolls: [], events: [] });
    for (const type of ['mouseenter', 'mouseover', 'mousemove']) {
      this.addEventListener(type, event => this.events.push([event.type, event.clientX, event.clientY]));
    }
  }
  matches() { return false; }
  getAttribute() { return null; }
  hasAttribute() { return false; }
  closest() { return null; }
  getClientRects() { return [{}]; }
  getBoundingClientRect() { return { x: 20, y: this.top, width: 200, height: 40 }; }
  scrollIntoView(options) {
    this.scrolls.push({ ...options });  // a plain copy out of the vm realm
    this.top = 280;
  }
}

function load(elements, point) {
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, CSS: { escape: x => x },
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: {
      body: {},
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
      elementFromPoint: () => point,
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  return msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
}

(async () => {
  const item = new Item();
  const send = load({ '#last': item }, item);
  const result = await send({ action: 'hover', selector: '#last' });
  assert.equal(result.ok, true, JSON.stringify(result));
  assert.deepEqual(item.scrolls, [{ behavior: 'instant', block: 'center', inline: 'center' }],
    'scrolled into view once, instantly, before dispatching');
  assert.deepEqual(item.events, [['mouseover', 120, 300], ['mouseenter', 120, 300], ['mousemove', 120, 300]],
    'the point is the centre of the rect measured after the scroll');

  // Coordinates name a point in the viewport, so a hover there does not scroll.
  const atPoint = new Item();
  const sendAtPoint = load({}, atPoint);
  assert.equal((await sendAtPoint({ action: 'hover', coords: { x: 50, y: 60 } })).ok, true);
  assert.deepEqual(atPoint.scrolls, []);

  console.log('Hover scroll checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
