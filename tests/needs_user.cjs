// Run with: node tests/needs_user.cjs (no browser or npm dependencies).
// #376: page info and snapshots carry a needs_user hint for what only the user can do on the page:
// a shown, unsolved CAPTCHA or verification frame known by origin and path (human_check), or a shown
// sign-in field (sign_in). Page text never makes one.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const RESPONSE_FIELDS = new Set(['g-recaptcha-response', 'h-captcha-response', 'cf-turnstile-response']);

class Element {
  constructor(tagName, attrs = {}, props = {}) {
    Object.assign(this, { tagName, attrs, nodeType: 1, isConnected: true, children: [], value: '',
      style: { display: 'block', visibility: 'visible' }, rect: { x: 10, y: 10, width: 300, height: 80 } }, props);
    this.type = props.type ?? String(attrs.type || 'text').toLowerCase();
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  // Only the response fields' selector is matched against elements.
  matches(selector) {
    assert.match(selector, /g-recaptcha-response/);
    return (this.tagName === 'TEXTAREA' || this.tagName === 'INPUT') && RESPONSE_FIELDS.has(this.attrs.name);
  }
  getBoundingClientRect() {
    const { x, y, width, height } = this.rect;
    return { x, y, left: x, top: y, width, height, right: x + width, bottom: y + height };
  }
  getClientRects() { return this.style.display === 'none' ? [] : [this.getBoundingClientRect()]; }
  closest() { return null; }
}

// A tree root (the document or a shadow root) over elements, each possibly hosting a shadow root.
function tree(elements) {
  return {
    querySelectorAll(selector) {
      if (selector === '*') return [...elements];
      if (selector === 'iframe, input, textarea') {
        return elements.filter(el => ['IFRAME', 'INPUT', 'TEXTAREA'].includes(el.tagName));
      }
      return [];
    },
    querySelector: () => null,
  };
}

function load(elements, url = 'https://shop.example/login') {
  let listener;
  const window = {
    getComputedStyle: el => el.style || { display: 'block', visibility: 'visible' },
    addEventListener() {},
    open() { return null; },
    innerWidth: 1280, innerHeight: 800, scrollX: 0, scrollY: 0,
  };
  const page = { elements };
  const document = {
    baseURI: url, title: 'Sign in', readyState: 'complete', body: {},
    documentElement: { lang: 'en', scrollWidth: 1280, scrollHeight: 2000, appendChild() {} },
    forms: [], links: [], images: [],
    querySelector: () => null,
    querySelectorAll: selector => tree(page.elements).querySelectorAll(selector),
    elementFromPoint: () => null,
    createElement: () => ({ remove() {} }),
    addEventListener() {},
  };
  const context = vm.createContext({
    console, crypto: webcrypto, Event, URL, CSS: { escape: x => x }, navigator: { platform: 'Linux x86_64' },
    window, document, location: new URL(url),
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } }, sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve))
    .then(result => JSON.parse(JSON.stringify(result)));  // copy out of the vm realm
  return { page, window, info: async () => (await send({ action: 'getPageInfo' })).needs_user };
}

const frame = (src, props = {}) => new Element('IFRAME', { src }, props);
const input = (attrs, props = {}) => new Element('INPUT', attrs, props);

async function main() {
  // Nothing to do for the user: no hint, and page info is unchanged.
  const plain = load([input({ type: 'search', name: 'q' }), frame('https://www.youtube.com/embed/x')]);
  assert.equal(await plain.info(), undefined);

  // A shown password field is a sign-in; so are username and passkey fields of a sign-in form.
  const password = input({ type: 'password', name: 'pw' });
  assert.deepEqual(await load([input({ type: 'email' }), password]).info(),
    { reason: 'sign_in', evidence: { field: 'password' } });
  assert.deepEqual(await load([input({ type: 'text', autocomplete: 'current-password' })]).info(),
    { reason: 'sign_in', evidence: { field: 'password' } }, 'a password shown in clear text');
  assert.deepEqual(await load([input({ type: 'email', autocomplete: 'username' })]).info(),
    { reason: 'sign_in', evidence: { field: 'username' } }, 'the first step of an identifier-first sign-in');
  assert.deepEqual(await load([input({ type: 'text', autocomplete: 'username webauthn' })]).info(),
    { reason: 'sign_in', evidence: { field: 'username' } });
  // A new password (a sign-up, a password change) is not a sign-in, nor is a hidden or off-page field.
  assert.equal(await load([input({ type: 'password', autocomplete: 'new-password' })]).info(), undefined);
  assert.equal(await load([input({ type: 'text', autocomplete: 'username' }),
    input({ type: 'password', autocomplete: 'new-password' })]).info(), undefined, 'a sign-up form');
  assert.deepEqual(await load([input({ type: 'password' }),
    input({ type: 'password', autocomplete: 'new-password' }, { hidden: true })]).info(),
    { reason: 'sign_in', evidence: { field: 'password' } }, 'a sign-up form not shown');
  assert.equal(await load([input({ type: 'password' }, { hidden: true })]).info(), undefined);
  assert.equal(await load([input({ type: 'password' }, { style: { display: 'none', visibility: 'visible' } })]).info(),
    undefined);
  assert.equal(await load([input({ type: 'password' }, { style: { display: 'block', visibility: 'hidden' } })]).info(),
    undefined);
  assert.equal(await load([input({ type: 'password' }, { rect: { x: 0, y: -10000, width: 300, height: 40 } })]).info(),
    undefined);
  assert.equal(await load([input({ type: 'password' }, { rect: { x: 0, y: 0, width: 0, height: 0 } })]).info(),
    undefined);
  // Words on the page never make a hint: a field merely named like a password is not one.
  assert.equal(await load([input({ type: 'text', name: 'password', placeholder: 'Sign in to continue' })]).info(),
    undefined);

  // Known CAPTCHA and verification frames by origin and path, with the widget and the frame's origin and path.
  for (const [src, widget, shown] of [
    ['https://www.google.com/recaptcha/api2/anchor?ar=1&k=key&size=normal', 'recaptcha',
      'https://www.google.com/recaptcha/api2/anchor'],
    ['https://www.recaptcha.net/recaptcha/enterprise/bframe?k=key', 'recaptcha',
      'https://www.recaptcha.net/recaptcha/enterprise/bframe'],
    ['https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox&sitekey=k', 'hcaptcha',
      'https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html'],
    ['https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv0/0/x/0x4/auto/normal',
      'turnstile', 'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/av0/rcv0/0/x/0x4/auto/normal'],
  ]) {
    assert.deepEqual(await load([frame(src)]).info(), { reason: 'human_check', evidence: { widget, frame: shown } }, src);
  }
  // Not by look-alikes, other paths, http, an invisible widget, or a challenge frame not shown yet.
  for (const src of [
    'https://www.google.com.evil.example/recaptcha/api2/anchor', 'https://evil.example/recaptcha/api2/anchor',
    'https://www.google.com/maps/embed', 'http://www.google.com/recaptcha/api2/anchor',
    'https://www.google.com/recaptcha/api2/anchor?k=key&size=invisible',
    'https://newassets.hcaptcha.com/captcha/v1/abc/static/hcaptcha.html#frame=checkbox&size=invisible',
    'https://nothcaptcha.com/captcha/v1/x', 'https://challenges.cloudflare.com/other', '', 'http://[',
  ]) {
    assert.equal(await load([frame(src)]).info(), undefined, src);
  }
  const bframe = frame('https://www.google.com/recaptcha/api2/bframe?k=key',
    { style: { display: 'block', visibility: 'hidden' } });
  assert.equal(await load([bframe]).info(), undefined, 'a challenge waits hidden until the checkbox is pressed');
  // A frame in a page's closed shadow root counts too (Turnstile renders there).
  const turnstile = frame('https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/g/turnstile/if/x');
  const host = new Element('DIV');
  host.openOrClosedShadowRoot = tree([turnstile]);
  assert.equal((await load([host]).info())?.evidence?.widget, 'turnstile');

  // A CAPTCHA comes before a sign-in field, until every response field holds the solved widget's token.
  const anchor = 'https://www.google.com/recaptcha/api2/anchor?k=key';
  const response = new Element('TEXTAREA', { name: 'g-recaptcha-response' }, { style: { display: 'none' } });
  const both = load([input({ type: 'password' }), frame(anchor), response]);
  assert.equal((await both.info()).reason, 'human_check');
  response.value = '03AFcWeA-token';
  assert.deepEqual(await both.info(), { reason: 'sign_in', evidence: { field: 'password' } }, 'solved');
  both.page.elements.push(new Element('INPUT', { name: 'cf-turnstile-response', type: 'hidden' }));
  assert.equal((await both.info()).reason, 'human_check', 'another widget is still unsolved');
  // Page info reports it as the page changes: the sign-in is gone once the user signed in.
  both.page.elements = [input({ type: 'search' })];
  assert.equal(await both.info(), undefined);

  // A page whose DOM cannot be read still answers page info, without a hint.
  const broken = load([]);
  broken.page.elements = null;
  assert.equal(await broken.info(), undefined);

  console.log('needs_user detection checks passed');
  finished = true;
}

// A read that never settles empties the event loop; fail instead of exiting 0.
let finished = false;
process.on('exit', () => { if (!finished) process.exitCode = 1; });
main().catch(error => { console.error(error); process.exitCode = 1; });
