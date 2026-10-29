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
// openThrows makes reading the page's window.open throw, so the injected watch fails.
function load({ injected = true, openThrows = false } = {}) {
  const popup = { closed: false };
  const button = new Element('BUTTON', 'google');
  const elements = { '#google': button };
  // The page's own window.open; next decides what it returns (null: the popup blocker refused it).
  const page = { next: () => null, calls: [], scripts: 0 };
  const window = {
    PointerEvent: MouseEvent,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
    addEventListener() {},
    open(...args) {
      page.calls.push({ self: this, args });
      return page.next(...args);
    },
  };
  if (openThrows) Object.defineProperty(window, 'open', { get() { throw new Error('page'); } });
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, MouseEvent, PointerEvent: MouseEvent, URL,
    CSS: { escape: x => x }, navigator: { platform: 'Linux x86_64' }, window,
    document: {
      baseURI: 'https://site.example/login/',
      body: {},
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
      elementFromPoint: () => button,
      createElement: () => ({ remove() {} }),
      // A strict CSP blocks any injected script; an injected script's exception stays in the page.
      documentElement: { appendChild(script) {
        page.scripts++;
        if (!injected) return;
        try { vm.runInContext(script.textContent, context); } catch {}
      } },
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  const nativeOpen = openThrows ? null : window.open;
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve))
    .then(result => JSON.parse(JSON.stringify(result)));  // copy out of the vm realm
  const click = () => send({ action: 'click', selector: '#google' });
  return { window, page, popup, button, nativeOpen, send, click, context };
}

async function main() {
  // The wrapper passes this, arguments, the return value and exceptions through unchanged.
  const h = load();
  assert.equal(h.page.scripts, 1, 'the dialogs and the window.open watch share one injected script');
  assert.equal(typeof h.window.__kitsuneDialogSnapshot, 'function');
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
  assert.deepEqual((await h.click()).scriptPopup, { url, urls: [url] });

  // A popup that opened is no warning.
  h.page.next = () => h.popup;
  assert.equal('scriptPopup' in (await h.click()), false);
  // noopener makes window.open return null for a popup that opened.
  h.page.next = () => null;
  features = 'noopener';
  assert.equal('scriptPopup' in (await h.click()), false);
  features = 'width=500,NoReferrer';
  assert.equal('scriptPopup' in (await h.click()), false);
  // The page's record keeps the URL of a refused call only.
  const record = JSON.parse(h.window.__kitsuneWindowOpenCalls());
  assert.deepEqual(record.slice(-4).map(call => [call.blocked, call.url]),
    [[true, url], [false, null], [false, null], [false, null]]);

  // A relative URL is resolved against the page's base URL; one that does not parse stays as given.
  const relative = load();
  relative.button.addEventListener('click', () => relative.window.open('/ok2?popup'));
  assert.deepEqual((await relative.click()).scriptPopup, { url: 'https://site.example/ok2?popup', urls: ['https://site.example/ok2?popup'] });
  const unparsable = load();
  unparsable.button.addEventListener('click', () => unparsable.window.open('http://['));
  assert.deepEqual((await unparsable.click()).scriptPopup, { url: 'http://[', urls: ['http://['] });

  // A URL that is not a string is not reported; a long one is cut.
  const urlObject = load();
  urlObject.button.addEventListener('click', () => urlObject.window.open({ href: 'https://a.example/' }));
  assert.deepEqual((await urlObject.click()).scriptPopup, { urls: [] });
  const blank = load();
  blank.button.addEventListener('click', () => blank.window.open());
  assert.deepEqual((await blank.click()).scriptPopup, { urls: [] });
  const long = load();
  long.button.addEventListener('click', () => long.window.open(`https://a.example/${'x'.repeat(5000)}`));
  assert.equal((await long.click()).scriptPopup.url.length, 2048);

  // Every refused URL is retained in call order, bounded by the existing 20-call record (#337).
  const multiple = load();
  multiple.button.addEventListener('click', () => {
    for (let i = 0; i < 25; i++) multiple.window.open(`/popup/${i}`);
  });
  const reported = (await multiple.click()).scriptPopup;
  assert.deepEqual(reported.urls, Array.from({ length: 20 }, (_, i) => `https://site.example/popup/${i + 5}`));
  assert.equal(reported.url, reported.urls[0]);

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

  // A window.open watch that fails in the page still leaves the dialogs installed.
  const failed = load({ openThrows: true });
  assert.equal(typeof failed.window.__kitsuneDialogSnapshot, 'function');
  assert.equal('__kitsuneWindowOpenCalls' in failed.window, false);
  assert.equal((await failed.click()).ok, true);

  console.log('Script popup checks passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
