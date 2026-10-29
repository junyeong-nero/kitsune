// Run with: node tests/drag_events.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    const { bubbles, cancelable, composed, ...rest } = init;
    Object.assign(this, rest);
  }
}

class Element extends EventTarget {
  constructor(id, y, props = {}) {
    super();
    Object.assign(this, { tagName: 'LI', id, textContent: id, isConnected: true, y, draggable: false,
      events: [], moves: [], captures: [] }, props);
    for (const type of ['pointerdown', 'mousedown', 'pointermove', 'mousemove', 'pointerup', 'mouseup',
      'dragstart', 'dragover', 'drop', 'dragend']) {
      this.addEventListener(type, event => this.events.push(event.type));
    }
    this.addEventListener('pointermove', event => this.moves.push(event));
  }
  getAttribute() { return null; }
  hasAttribute() { return false; }
  closest(selector) { return selector === '[draggable="true"]' && this.draggable ? this : null; }
  setPointerCapture(pointerId) { this.captures.push(pointerId); }
  getBoundingClientRect() { return { x: 0, y: this.y, width: 200, height: 30 }; }
}

// A sortable list as pointer drag libraries build it: pointerdown captures the pointer,
// moves past a 5px threshold start the drag, and pointerup reorders at the point.
function pointerSortable(list) {
  for (const item of list) {
    item.addEventListener('pointerdown', down => {
      item.setPointerCapture(down.pointerId);
      let dragging = false;
      const move = event => {
        if (event.pointerId !== down.pointerId || event.buttons !== 1 || !event.isPrimary) return;
        dragging ||= Math.hypot(event.clientX - down.clientX, event.clientY - down.clientY) > 5;
      };
      const up = event => {
        item.removeEventListener('pointermove', move);
        item.removeEventListener('pointerup', up);
        const over = list.find(other => event.clientY >= other.y && event.clientY < other.y + 30);
        if (!dragging || !over || over === item) return;
        list.splice(list.indexOf(item), 1);
        list.splice(list.indexOf(over) + (over.y > item.y ? 1 : 0), 0, item);
      };
      // Captured events reach the capturing element, so the library listens on the item.
      item.addEventListener('pointermove', move);
      item.addEventListener('pointerup', up);
    });
  }
}

function page(items) {
  let listener;
  const elements = Object.fromEntries(items.map(item => [`#${item.id}`, item]));
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, PointerEvent: MouseEvent, DragEvent: MouseEvent,
    DataTransfer: class {}, MessageChannel, CSS: { escape: x => x },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { PointerEvent: MouseEvent, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: {
      body: {},
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  // Results come from the vm realm, so compare plain copies.
  return msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
}

async function testPointerCaptureSortableReorders() {
  const [a, b, c] = [new Element('a', 0), new Element('b', 40), new Element('c', 80)];
  const list = [a, b, c];
  pointerSortable(list);
  const send = page(list);
  assert.deepEqual(await send({ action: 'drag', from: '#a', to: '#c' }),
    { ok: true, from: '#a', to: '#c', via: 'pointer' });
  assert.deepEqual(list.map(item => item.id), ['b', 'c', 'a']);
  assert.deepEqual(a.captures, [1]);
  // Several moves on the capturing source, each further along, end on the destination.
  const ys = a.moves.map(event => event.clientY);
  assert.ok(ys.length > 2);
  assert.ok(ys.every((y, i) => i === 0 || y > ys[i - 1]));
  assert.equal(ys.at(-1), 95);
  assert.deepEqual(a.events.filter(type => type.startsWith('pointer')).slice(0, 2), ['pointermove', 'pointerdown']);
  assert.equal(a.events.at(-1), 'pointerup');
  assert.deepEqual(c.events.filter(type => type.startsWith('mouse')), ['mouseup']);
}

async function testHtml5DropStaysHtml5() {
  const from = new Element('from', 0, { draggable: true });
  const to = new Element('to', 40);
  let dropped = false;
  to.addEventListener('dragover', event => event.preventDefault());
  to.addEventListener('drop', event => { event.preventDefault(); dropped = true; });
  const send = page([from, to]);
  assert.deepEqual(await send({ action: 'drag', from: '#from', to: '#to' }),
    { ok: true, from: '#from', to: '#to', via: 'html5' });
  assert.equal(dropped, true);
  assert.equal(from.moves.length, 1, 'move to the source before pressing, even for HTML5');
  assert.equal(from.moves[0].buttons, 0);
  assert.deepEqual(from.events, ['pointermove', 'mousemove', 'pointerdown', 'mousedown', 'dragstart', 'dragend']);
}

// Pointer libraries cancel dragstart on draggable items; a page-wide dragover handler
// (file drop) or a non-draggable source does not make the drag HTML5 either.
async function testUnacceptedHtml5FallsBackToPointer() {
  const cancelled = new Element('cancelled', 0, { draggable: true });
  cancelled.addEventListener('dragstart', event => event.preventDefault());
  const plain = new Element('plain', 0);
  for (const from of [cancelled, plain]) {
    const to = new Element('to', 40);
    to.addEventListener('dragover', event => event.preventDefault());
    const send = page([from, to]);
    assert.equal((await send({ action: 'drag', from: `#${from.id}`, to: '#to' })).via, 'pointer', from.id);
    assert.ok(from.events.includes('dragstart') && to.events.includes('drop'), 'HTML5 events still fire');
    assert.equal(from.moves.length > 2, true, from.id);
  }
}

(async () => {
  await testPointerCaptureSortableReorders();
  await testHtml5DropStaysHtml5();
  await testUnacceptedHtml5FallsBackToPointer();
  console.log('drag_events: all tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
