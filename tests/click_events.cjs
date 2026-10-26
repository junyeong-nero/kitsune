// Run with: node tests/click_events.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    Object.assign(this, { clientX: init.clientX, clientY: init.clientY, detail: init.detail || 0,
      button: init.button || 0, buttons: init.buttons || 0, pointerType: init.pointerType || '' });
  }
}

class Element extends EventTarget {
  constructor(tagName, id, type) {
    super();
    Object.assign(this, { tagName, id, type, textContent: id, isConnected: true,
      checked: false, nativeClicks: 0, events: [], dispatched: [] });
    for (const type of ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click', 'dblclick', 'input', 'change']) {
      this.addEventListener(type, event => { this.events.push(event.type); this.dispatched.push(event); });
    }
  }
  matches() { return false; }
  getAttribute() { return null; }
  hasAttribute() { return false; }
  closest() { return null; }
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
  click() { this.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); }
  // As in a browser, any dispatched click runs native activation after its listeners.
  dispatchEvent(event) {
    const result = super.dispatchEvent(event);
    if (event.type === 'click') {
      this.nativeClicks++;
      if (this.type === 'checkbox') this.checked = !this.checked;
    }
    return result;
  }
}

class Anchor extends Element {
  constructor(id, attributes, href = 'https://example.com/ok2') {
    super('A', id);
    Object.assign(this, { attributes, href, protocol: new URL(href).protocol,
      relList: { contains: token => (attributes.rel || '').split(' ').includes(token) } });
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  hasAttribute(name) { return name in this.attributes; }
  closest() { return this; }
}

async function testClickEvents(pointerEvents) {
  const button = new Element('BUTTON', 'go', 'submit');
  const checkbox = new Element('INPUT', 'toggle', 'checkbox');
  const child = new Element('SPAN', 'child');
  child.parentElement = button;
  const form = new Element('FORM', 'form');
  form.querySelector = () => button;
  form.querySelectorAll = () => [button];
  form.getRootNode = () => form;
  button.form = form;
  const elements = { '#go': button, '#toggle': checkbox, '#form': form };
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, PointerEvent: MouseEvent,
    CSS: { escape: x => x }, navigator: { platform: 'Linux x86_64' },
    window: { PointerEvent: pointerEvents ? MouseEvent : undefined,
      getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: {
      body: {},
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
      elementFromPoint: () => child,
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve));
  const ref = (await send({ action: 'find', query: '#go' })).results[0].ref;
  const clickSequence = ['pointerover', 'pointerenter', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
  for (const target of [{ selector: '#go' }, { selector: ref }, { coords: { x: 20, y: 10 } }]) {
    button.events = [];
    const nativeClicks = button.nativeClicks;
    assert.equal((await send({ action: 'click', ...target })).ok, true);
    assert.equal(button.events.filter(type => type === 'click').length, 1, 'one click handler invocation per action');
    assert.equal(button.nativeClicks, nativeClicks + 1, 'retain native activation');
    assert.deepEqual(button.events, clickSequence);
    // #201: released buttons read 0, and the click carries the point and a click count.
    const x = target.coords ? 20 : 50, y = target.coords ? 10 : 15;
    assert.deepEqual(button.dispatched.map(event => [event.clientX, event.clientY, event.button, event.buttons]),
      clickSequence.map(type => [x, y, 0, ['pointerup', 'mouseup', 'click'].includes(type) ? 0 : 1]));
    const click = button.dispatched.at(-1);
    assert.equal(click.detail, 1);
    assert.equal(click.pointerType, 'mouse');
    button.dispatched = [];
  }

  const disabled = new Element('BUTTON', 'off', 'button');
  disabled.matches = selector => selector.includes('button:disabled');
  elements['#off'] = disabled;
  assert.equal((await send({ action: 'click', selector: '#off' })).ok, true);
  assert.deepEqual(disabled.events, clickSequence.slice(0, -1), 'a disabled control gets no click, as with el.click()');
  assert.equal(disabled.nativeClicks, 0);

  for (const checked of [true, false]) {
    checkbox.events = [];
    assert.equal((await send({ action: 'click', selector: '#toggle' })).ok, true);
    assert.equal(checkbox.checked, checked, 'one native checkbox toggle per click');
    assert.equal(checkbox.events.filter(type => type === 'click').length, 1);
  }

  button.events = [];
  const nativeClicks = button.nativeClicks;
  assert.equal((await send({ action: 'doubleClick', selector: '#go' })).ok, true);
  assert.deepEqual(button.events, ['mousedown', 'mouseup', 'click', 'mousedown', 'mouseup', 'click', 'dblclick']);
  assert.equal(button.nativeClicks, nativeClicks + 2, 'each of doubleClick\'s two clicks activates once');

  for (const checked of [true, false]) {
    checkbox.events = [];
    const nativeClicks = checkbox.nativeClicks;
    const result = await send({ action: 'checkBox', selector: '#toggle', checked });
    assert.equal(result.ok, true);
    assert.equal(result.checked, checked);
    assert.equal(checkbox.checked, checked);
    assert.deepEqual(checkbox.events, ['input', 'change']);
    assert.equal(checkbox.nativeClicks, nativeClicks, 'checkBox sets state without extra clicks');
  }

  button.events = [];
  assert.equal((await send({ action: 'submitForm', selector: '#form' })).ok, true);
  assert.deepEqual(button.events, ['click'], 'submit button activates once');
  assert.equal(button.nativeClicks, nativeClicks + 3);

  // #94: a click on an http(s) link that opens a new tab names it for the background.
  const blankLink = async (link, base = null) => {
    elements['#link'] = link;
    elements['base[target]'] = base;
    const result = await send({ action: 'click', selector: '#link' });
    assert.equal(result.ok, true);
    assert.equal(link.nativeClicks, 1, 'retain native activation');
    return result.targetBlankLink && { ...result.targetBlankLink };  // copy out of the vm realm
  };
  const url = 'https://example.com/ok2';
  assert.deepEqual(await blankLink(new Anchor('blank', { target: '_blank' })), { url, opener: false });
  assert.deepEqual(await blankLink(new Anchor('opener', { target: '_BLANK', rel: 'noreferrer opener' })),
    { url, opener: true });
  const base = { getAttribute: () => '_blank' };
  assert.deepEqual(await blankLink(new Anchor('base', {}), base), { url, opener: false });
  assert.equal(await blankLink(new Anchor('own-target', { target: '' }), base), undefined, 'own target wins');
  assert.equal(await blankLink(new Anchor('self', {})), undefined);
  assert.equal(await blankLink(new Anchor('download', { target: '_blank', download: '' })), undefined);
  assert.equal(await blankLink(new Anchor('mail', { target: '_blank' }, 'mailto:a@example.com')), undefined);
  const cancelled = new Anchor('cancelled', { target: '_blank' });
  cancelled.addEventListener('click', event => event.preventDefault());
  assert.equal(await blankLink(cancelled), undefined, 'a cancelled click is left to the page');
  // A click on a link's descendant follows the link.
  const label = new Element('SPAN', 'label');
  const parent = new Anchor('parent', { target: '_blank' });
  label.closest = () => parent;
  elements['#label'] = label;
  assert.deepEqual({ ...(await send({ action: 'click', selector: '#label' })).targetBlankLink }, { url, opener: false });

  // #223: a middle or Ctrl/Cmd click on any http(s) link asks the background for its new tab,
  // and says whether the page cancelled the click (auxclick for the middle button).
  const newTabLink = async (link, extra) => {
    elements['#link'] = link;
    elements['base[target]'] = null;
    const result = await send({ action: 'click', selector: '#link', ...extra });
    assert.equal(result.ok, true);
    assert.equal(result.targetBlankLink, undefined, 'not also reported as a target=_blank link');
    return result.newTabLink && { ...result.newTabLink };  // copy out of the vm realm
  };
  for (const extra of [{ modifiers: ['ControlOrMeta'] }, { modifiers: ['Meta'] }, { modifiers: ['Control', 'Shift'] },
    { button: 'middle' }]) {
    const label = JSON.stringify(extra);
    assert.deepEqual(await newTabLink(new Anchor('plain', {}), extra), { url, prevented: false }, label);
    assert.deepEqual(await newTabLink(new Anchor('blank', { target: '_blank' }), extra), { url, prevented: false }, label);
    const handled = new Anchor('handled', {});
    for (const type of ['click', 'auxclick']) handled.addEventListener(type, event => event.preventDefault());
    assert.deepEqual(await newTabLink(handled, extra), { url, prevented: true }, label);
    assert.equal(await newTabLink(new Anchor('download', { download: '' }), extra), undefined, label);
    assert.equal(await newTabLink(new Anchor('mail', {}, 'mailto:a@example.com'), extra), undefined, label);
  }
  // Shift alone, Alt and the right button do not open a link in a new tab.
  assert.equal(await newTabLink(new Anchor('shift', {}), { modifiers: ['Shift'] }), undefined);
  assert.equal(await newTabLink(new Anchor('alt', {}), { modifiers: ['Alt'] }), undefined);
  assert.equal(await newTabLink(new Anchor('right', {}), { button: 'right' }), undefined);
  // A modified click on a button is not a link click.
  const modified = await send({ action: 'click', selector: '#go', modifiers: ['ControlOrMeta'] });
  assert.equal(modified.ok, true);
  assert.equal(modified.newTabLink, undefined);
}

(async () => {
  await testClickEvents(true);
  await testClickEvents(false);
  console.log('Click event counts and native activation checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
