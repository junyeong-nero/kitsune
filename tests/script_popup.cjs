// Run with: node tests/script_popup.cjs (no browser or npm dependencies).
// #304: a synthetic click during which a page script's window.open returns null (the popup
// blocker refused it) reports scriptPopup for the background's popup_blocked warning.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class MouseEvent extends Event {
  constructor(type, init = {}) {
    super(type, init);
    Object.assign(this, { clientX: init.clientX, clientY: init.clientY, detail: init.detail || 0,
      button: init.button || 0, buttons: init.buttons || 0 });
  }
}

class Element extends EventTarget {
  constructor(tagName, id) {
    super();
    Object.assign(this, { tagName, id, textContent: id, isConnected: true });
  }
  matches() { return false; }
  getAttribute() { return null; }
  hasAttribute() { return false; }
  closest() { return null; }
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
  click() {}
}

// The content script's page-world scripts run in the page; injected marks whether they can (CSP).
function load({ injected = true } = {}) {
  const popup = { closed: false };
  const button = new Element('BUTTON', 'google');
  const elements = { '#google': button };
  // The page's own window.open; next decides what it returns (null: the popup blocker refused it).
  const page = { next: () => null, calls: [] };
  const window = {
    PointerEvent: MouseEvent,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
    open(...args) {
      page.calls.push({ self: this, args });
      return page.next(...args);
    },
  };
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, PointerEvent: MouseEvent,
    CSS: { escape: x => x }, navigator: { platform: 'Linux x86_64' }, window,
    document: {
      body: {},
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
      elementFromPoint: () => button,
      createElement: () => ({ remove() {} }),
      // Only the window.open watch runs; a strict CSP blocks it like any injected script.
      documentElement: { appendChild(script) {
        if (injected && script.textContent.includes('installWindowOpenWatch')) {
          vm.runInContext(script.textContent, context);
        }
      } },
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  const nativeOpen = window.open;
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve))
    .then(result => JSON.parse(JSON.stringify(result)));  // copy out of the vm realm
  const click = () => send({ action: 'click', selector: '#google' });
  return { window, page, popup, button, nativeOpen, send, click, context };
}

async function main() {
  // The wrapper passes this, arguments, the return value and exceptions through unchanged.
  const h = load();
  assert.notEqual(h.window.open, h.nativeOpen, 'window.open is wrapped');
  assert.equal(h.window.open.name, 'open');
  h.page.next = () => h.popup;
  assert.equal(h.window.open('https://accounts.example/auth', 'auth', 'width=500'), h.popup);
  assert.equal(h.page.calls.at(-1).self, h.window);
  assert.deepEqual(h.page.calls.at(-1).args, ['https://accounts.example/auth', 'auth', 'width=500']);
  const other = {};
  h.window.open.call(other);
  assert.equal(h.page.calls.at(-1).self, other);
  assert.deepEqual(h.page.calls.at(-1).args, []);
  h.page.next = () => null;
  assert.equal(h.window.open('/blocked'), null);
  const failure = new SyntaxError('bad URL');
  h.page.next = () => { throw failure; };
  assert.throws(() => h.window.open('http://['), error => error === failure);

  // A blocked call before the click is not the click's.
  h.page.next = () => null;
  const quiet = await h.click();
  assert.equal(quiet.ok, true);
  assert.equal('scriptPopup' in quiet, false, 'no window.open during the click, no scriptPopup');

  // The click handler's window.open returns null: the popup blocker refused it.
  const url = 'https://accounts.example/o/oauth2/auth?client_id=1';
  let features = 'popup,width=500';
  h.button.addEventListener('click', () => h.window.open(url, 'g_auth', features));
  assert.deepEqual((await h.click()).scriptPopup, { url });

  // A popup that opened is no warning.
  h.page.next = () => h.popup;
  assert.equal('scriptPopup' in (await h.click()), false);
  // noopener makes window.open return null for a popup that opened.
  h.page.next = () => null;
  features = 'noopener';
  assert.equal('scriptPopup' in (await h.click()), false);
  features = 'width=500,NoReferrer';
  assert.equal('scriptPopup' in (await h.click()), false);

  // A URL that is not a string is not reported; a long one is cut.
  const urlObject = load();
  urlObject.button.addEventListener('click', () => urlObject.window.open({ href: 'https://a.example/' }));
  assert.deepEqual((await urlObject.click()).scriptPopup, {});
  const blank = load();
  blank.button.addEventListener('click', () => blank.window.open());
  assert.deepEqual((await blank.click()).scriptPopup, {});
  const long = load();
  long.button.addEventListener('click', () => long.window.open(`https://a.example/${'x'.repeat(5000)}`));
  assert.equal((await long.click()).scriptPopup.url.length, 2048);

  // A click that failed before dispatch reports nothing.
  assert.equal('scriptPopup' in (await h.send({ action: 'click', selector: '#missing' })), false);

  // The record is page data: a page that replaces it gets no warning and no error.
  for (const record of [() => 'not json', () => '{"seq":1}', () => { throw new Error('page'); },
    () => JSON.stringify([{ seq: 'x', blocked: true }]), 'not a function']) {
    const tampered = load();
    tampered.button.addEventListener('click', () => tampered.window.open('/popup'));
    tampered.window.__kitsuneWindowOpenCalls = record;
    const result = await tampered.click();
    assert.equal(result.ok, true);
    assert.equal('scriptPopup' in result, false);
  }

  // A second injection (a reinjected content script) keeps the one wrapper.
  const twice = load();
  const wrapper = twice.window.open;
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8')
    .replace(/const CONTENT_VERSION = \d+;/, 'const CONTENT_VERSION = 1e9;'), twice.context);
  assert.equal(twice.window.open, wrapper);
  twice.window.open('/once');
  assert.equal(twice.page.calls.length, 1, 'the original runs once per call');

  // Under a CSP that blocks the injected script, window.open stays the page's and clicks still work.
  const csp = load({ injected: false });
  assert.equal(csp.window.open, csp.nativeOpen);
  csp.button.addEventListener('click', () => csp.window.open('/popup'));
  const unwatched = await csp.click();
  assert.equal(unwatched.ok, true);
  assert.equal('scriptPopup' in unwatched, false);
  assert.equal(csp.page.calls.length, 1);

  console.log('Script popup checks passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
