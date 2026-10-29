// Run with: node tests/hover_events.cjs (no browser or npm dependencies).
// #327: browser_hover dispatches what a real pointer arriving at its target would: pointerover,
// pointerenter, mouseover, mouseenter, pointermove, mousemove. Over and move bubble out of shadow
// roots; enter events do not bubble and go to each newly entered ancestor, outermost first.
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
class PointerEvent extends MouseEvent {}

// A tree with DOM event propagation: capture down the path, the target, then bubbling up. At a
// shadow root the path continues to its host only for a composed event.
class Node {
  constructor(nodeType, parentNode) {
    this.nodeType = nodeType;
    this.parentNode = parentNode || null;
    this.listeners = [];
  }
  get isConnected() { return !this.removed && (!this.parentNode || this.parentNode.isConnected); }
  addEventListener(type, listener, capture = false) { this.listeners.push({ type, listener, capture }); }
  dispatchEvent(event) {
    const path = [];
    for (let node = this; node; node = node.nodeType === 11 ? (event.composed ? node.host : null) : node.parentNode) {
      path.push(node);
    }
    Object.defineProperty(event, 'target', { value: this });
    const call = (node, phase) => {
      for (const { type, listener, capture } of node.listeners) {
        if (type === event.type && (phase === 'target' || capture === (phase === 'capture'))) listener(event, node);
      }
    };
    for (const node of path.slice(1).reverse()) call(node, 'capture');
    call(this, 'target');
    if (event.bubbles) for (const node of path.slice(1)) call(node, 'bubble');
    return !event.defaultPrevented;
  }
}

class Element extends Node {
  constructor(tagName, id, parentNode) {
    super(1, parentNode);
    Object.assign(this, { tagName, id, textContent: id, hidden: false });
  }
  get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
  matches() { return false; }
  getAttribute() { return null; }
  hasAttribute() { return false; }
  closest() { return null; }
  getClientRects() { return [{}]; }
  getBoundingClientRect() { return { x: 10, y: 20, width: 100, height: 40 }; }
  scrollIntoView() {}
}

class ShadowRoot extends Node {
  constructor(host) {
    super(11, null);
    this.host = host;
  }
  get isConnected() { return this.host.isConnected; }
}

const HOVER_TYPES = ['pointerover', 'pointerenter', 'mouseover', 'mouseenter', 'pointermove', 'mousemove'];
const ENTER_TYPES = new Set(['pointerenter', 'mouseenter']);

// html > body > ul#list > (li#row1 > span#title1, button#menu1), (li#row2 > span#title2),
// x-card#card with a shadow root > div#inner > span#label; and a same-origin frame's document,
// html#frameHtml > body#frameBody > span#framed, whose elements the selectors also reach.
function page() {
  const document = new Node(9, null);
  const html = new Element('HTML', 'html', document);
  const body = new Element('BODY', 'body', html);
  const list = new Element('UL', 'list', body);
  const row1 = new Element('LI', 'row1', list);
  const title1 = new Element('SPAN', 'title1', row1);
  const menu1 = new Element('BUTTON', 'menu1', row1);
  const row2 = new Element('LI', 'row2', list);
  const title2 = new Element('SPAN', 'title2', row2);
  const card = new Element('X-CARD', 'card', body);
  const shadow = new ShadowRoot(card);
  const inner = new Element('DIV', 'inner', shadow);
  const label = new Element('SPAN', 'label', inner);
  const frameDocument = new Node(9, null);
  const frameHtml = new Element('HTML', 'frameHtml', frameDocument);
  const frameBody = new Element('BODY', 'frameBody', frameHtml);
  const framed = new Element('SPAN', 'framed', frameBody);
  const elements = { html, body, list, row1, title1, menu1, row2, title2, card, inner, label,
    frameHtml, frameBody, framed };

  // Every event any element sees, by the element listening: [listener, type, target].
  const log = [];
  for (const el of Object.values(elements)) {
    for (const type of HOVER_TYPES) {
      el.addEventListener(type, (event, node) => log.push([node.id, event.type, event.target.id]));
    }
  }
  // The row's hover controls appear on pointerenter, as list rows such as YouTube's do (#327).
  menu1.hidden = true;
  row1.addEventListener('pointerenter', () => { menu1.hidden = false; });

  let listener;
  const window = { PointerEvent, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) };
  Object.assign(document, {
    body,
    querySelector: selector => elements[selector.slice(1)] || null,
    querySelectorAll: selector => elements[selector.slice(1)] ? [elements[selector.slice(1)]] : [],
    elementFromPoint: () => label,
  });
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, PointerEvent, CSS: { escape: x => x },
    navigator: { platform: 'Linux x86_64' }, window, document,
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  // Set after loading, so the script skips installing its page-world functions.
  document.documentElement = html;
  const send = msg => new Promise(resolve => listener(msg, {}, result => resolve(JSON.parse(JSON.stringify(result)))));
  return { send, log, window, document, elements };
}

// The enter events of one hover, as [element, type], in dispatch order.
const enters = log => log.filter(([, type]) => ENTER_TYPES.has(type)).map(([id, type]) => [id, type]);

(async () => {
  // A row that shows its button on pointerenter does so when one of its children is hovered.
  {
    const { send, log, window, elements } = page();
    const events = [];
    for (const type of HOVER_TYPES) elements.title1.addEventListener(type, event => events.push(event));
    const result = await send({ action: 'hover', selector: '#title1' });
    // popupWatch asks the background to take a popup the hover opened; it removes the field (#329).
    assert.deepEqual(result, { ok: true, tag: 'span', popupWatch: true });
    assert.equal(elements.menu1.hidden, false, 'the row saw pointerenter and revealed its button');

    // The order of a pointer arriving: each enter type right after its over type.
    assert.deepEqual(events.map(event => event.type), HOVER_TYPES);
    for (const event of events) {
      const enter = ENTER_TYPES.has(event.type);
      assert.equal(event.bubbles, !enter, `${event.type} bubbles`);
      assert.equal(event.composed, !enter, `${event.type} composed`);
      assert.equal(event.cancelable, !enter, `${event.type} cancelable`);
      assert.equal(event instanceof PointerEvent, event.type.startsWith('pointer'), `${event.type} class`);
      assert.equal(event.view, window);
      assert.deepEqual([event.clientX, event.clientY], [60, 40], 'the center of the target');
      assert.deepEqual([event.pointerType, event.pointerId, event.isPrimary], ['mouse', 1, true]);
    }

    // On the first hover every ancestor up to the root element is entered, outermost first,
    // and an enter event reaches only the element it is sent to: it does not bubble.
    const chain = ['html', 'body', 'list', 'row1', 'title1'];
    assert.deepEqual(enters(log), [
      ...chain.map(id => [id, 'pointerenter']), ...chain.map(id => [id, 'mouseenter'])]);
    for (const [listening, type, target] of log) {
      if (ENTER_TYPES.has(type)) assert.equal(listening, target, `${type} on ${target} reached ${listening}`);
    }
    // Over and move bubble from the target to the root element.
    assert.deepEqual(log.filter(([, type]) => type === 'pointerover').map(([id]) => id),
      ['title1', 'row1', 'list', 'body', 'html']);

    // Within the same row only the newly entered button gets enter events.
    log.length = 0;
    await send({ action: 'hover', selector: '#menu1' });
    assert.deepEqual(enters(log), [['menu1', 'pointerenter'], ['menu1', 'mouseenter']]);
    assert.deepEqual(log.filter(([id]) => id === 'row1').map(([, type]) => type),
      ['pointerover', 'mouseover', 'pointermove', 'mousemove'], 'the row only sees bubbled over and move');

    // Moving to the next row enters it and its child, not the list it shares with the first.
    log.length = 0;
    await send({ action: 'hover', selector: '#title2' });
    assert.deepEqual(enters(log), [
      ['row2', 'pointerenter'], ['title2', 'pointerenter'], ['row2', 'mouseenter'], ['title2', 'mouseenter']]);

    // Hovering the same element again enters its whole chain: the pointer may have left
    // meanwhile, and a retry must be able to reveal the controls again.
    log.length = 0;
    await send({ action: 'hover', selector: '#title2' });
    assert.deepEqual(enters(log).filter(([, type]) => type === 'mouseenter').map(([id]) => id),
      ['html', 'body', 'list', 'row2', 'title2']);
    assert.equal(log.filter(([id, type]) => id === 'title2' && type === 'mousemove').length, 1);

    // A removed previous element no longer holds the pointer inside its ancestors.
    elements.row2.removed = true;
    log.length = 0;
    await send({ action: 'hover', selector: '#title1' });
    assert.deepEqual(enters(log).filter(([, type]) => type === 'pointerenter').map(([id]) => id), chain);
  }

  // Click and hover share pointer tracking: the click enters the row, then a sibling hover
  // enters only the sibling, not their shared ancestors.
  {
    const { send, log, elements } = page();
    elements.menu1.click = () => {};
    let rowEntered = 0;
    elements.row1.addEventListener('mouseenter', () => { rowEntered++; });
    assert.equal((await send({ action: 'click', selector: '#menu1' })).ok, true);
    assert.equal(rowEntered, 1, 'the click enters the row before pressing');
    log.length = 0;
    await send({ action: 'hover', selector: '#title1' });
    assert.equal(rowEntered, 1, 'the sibling hover does not re-enter the row');
    assert.deepEqual(enters(log).filter(([, type]) => type === 'mouseenter').map(([id]) => id),
      ['title1']);
  }

  // A page element named "host" (<form name="host">) becomes document.host. The ancestor walk
  // crosses only shadow roots to their hosts, so it ends at the root element.
  {
    const { send, log, document, elements } = page();
    const form = new Element('FORM', 'search', elements.body);
    let hostReads = 0;
    Object.defineProperty(document, 'host', { get() {
      if (++hostReads > 50) throw new Error('the ancestor walk looped through document.host');
      return form;
    } });
    assert.equal((await send({ action: 'hover', selector: '#title1' })).ok, true);
    assert.deepEqual(enters(log).filter(([, type]) => type === 'pointerenter').map(([id]) => id),
      ['html', 'body', 'list', 'row1', 'title1']);
    // Exercise flatParent's warning walk and the selector parent step as well (#339).
    elements.title1.getClientRects = () => [];
    assert.equal((await send({ action: 'hover', selector: '#title1' })).ok, true);
    const parent = await send({ action: 'hover', selector: '#html >> ..' });
    assert.equal(parent.code, 'not_found');
    assert.equal(hostReads, 0, 'Document.host is never a shadow boundary');
  }

  // The chain stays within the target's document. Moving into a same-origin frame enters the
  // frame document's chain, and moving back enters the top document's chain again: the two
  // documents share no ancestors.
  {
    const { send, log } = page();
    await send({ action: 'hover', selector: '#title1' });
    log.length = 0;
    assert.equal((await send({ action: 'hover', selector: '#framed' })).ok, true);
    assert.deepEqual(enters(log).filter(([, type]) => type === 'pointerenter').map(([id]) => id),
      ['frameHtml', 'frameBody', 'framed']);
    log.length = 0;
    await send({ action: 'hover', selector: '#title1' });
    assert.deepEqual(enters(log).filter(([, type]) => type === 'pointerenter').map(([id]) => id),
      ['html', 'body', 'list', 'row1', 'title1']);
  }

  // Inside a shadow root: listeners there and outside it see the composed over and move events,
  // and the host is entered as an ancestor across the shadow boundary.
  {
    const { send, log, elements } = page();
    const outside = [];
    elements.body.addEventListener('pointerover', event => outside.push(event.type));
    elements.body.addEventListener('mousemove', event => outside.push(event.type));
    const inside = [];
    elements.inner.addEventListener('pointerover', event => inside.push(event.type));
    elements.inner.addEventListener('mousemove', event => inside.push(event.type));
    const result = await send({ action: 'hover', selector: '#label' });
    assert.equal(result.ok, true, JSON.stringify(result));
    assert.deepEqual(inside, ['pointerover', 'mousemove'], 'the listener inside the shadow root');
    assert.deepEqual(outside, ['pointerover', 'mousemove'], 'composed events leave the shadow root');
    assert.deepEqual(enters(log).filter(([, type]) => type === 'pointerenter').map(([id]) => id),
      ['html', 'body', 'card', 'inner', 'label']);
    for (const [listening, type, target] of log) {
      if (ENTER_TYPES.has(type)) assert.equal(listening, target, `${type} on ${target} reached ${listening}`);
    }

    // At coordinates the element at that point is hovered, and the host is not entered again.
    await send({ action: 'hover', selector: '#card' });
    log.length = 0;
    assert.equal((await send({ action: 'hover', coords: { x: 50, y: 60 } })).ok, true);
    assert.deepEqual(enters(log), [['inner', 'pointerenter'], ['label', 'pointerenter'],
      ['inner', 'mouseenter'], ['label', 'mouseenter']]);
  }

  console.log('Hover event checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
