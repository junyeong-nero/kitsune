// Run with: node tests/media_controls.cjs (no browser or npm dependencies).
// Issue #158: <video>/<audio> in snapshots, click-to-toggle and the media command.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element extends EventTarget {
  constructor(tagName, textContent = '', attrs = {}, children = []) {
    super();
    Object.assign(this, { tagName, textContent, attrs, children, nodeType: 1,
      childNodes: textContent ? [{ nodeType: 3, textContent }] : [], isConnected: true });
    children.forEach(child => { child.parentElement = this; });
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  closest() { return null; }
  scrollIntoView() {}
  getBoundingClientRect() { return { x: 0, y: 0, width: 320, height: 180 }; }
  click() { this.dispatchEvent(new Event('click', { bubbles: true, cancelable: true })); }
}

// HTMLMediaElement as far as Kitsune uses it; unmuted play() is refused like an autoplay block,
// and play() stays pending in a hidden tab that was never shown, as in Firefox.
class Media extends Element {
  constructor(tagName, attrs, fallback) {
    super(tagName, '', attrs, [new Element('SOURCE'), new Element('P', fallback)]);
    Object.assign(this, { currentSrc: 'http://127.0.0.1/clip.webm', duration: 4, time: 0,
      paused: true, muted: true, controls: true, title: '' });
  }
  get currentTime() { return this.time; }
  set currentTime(value) {
    this.time = value;
    setTimeout(() => this.dispatchEvent(new Event('seeked')), 5);
  }
  play() {
    if (this.document.hidden) return new Promise(() => {});
    this.paused = false;
    if (this.muted) return Promise.resolve();
    this.paused = true;
    const error = new Error('play() needs user activation');
    error.name = 'NotAllowedError';
    return Promise.reject(error);
  }
  pause() { this.paused = true; }
}

async function testMedia() {
  const video = new Media('VIDEO', { 'aria-label': 'Clip' }, 'Your browser cannot play this video');
  const audio = Object.assign(new Media('AUDIO', {}, ''), { duration: NaN, currentSrc: '', src: '' });
  const button = new Element('BUTTON', 'Next');
  const body = new Element('BODY', '', {}, [new Element('MAIN', '', {}, [video, audio, button])]);
  const elements = { '#video': video, '#button': button, 'video, audio': null };
  const document = { body, hidden: false, getElementById: () => null,
    querySelector: selector => elements[selector] || null,
    querySelectorAll: selector => selector === 'video, audio' ? [video, audio] :
      elements[selector] ? [elements[selector]] : [] };
  video.document = audio.document = document;
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout, clearTimeout, Event, MouseEvent: Event,
    window: { getComputedStyle: el => ({ display: el.tagName === 'SOURCE' ? 'none' : 'block', visibility: 'visible' }) },
    document,
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve));
  const snapshot = async () => (await send({ action: 'getSnapshot' })).snapshot;
  const copy = value => JSON.parse(JSON.stringify(value));  // out of the vm realm

  // Snapshot: role, name, ref and state; sources and fallback content are not listed.
  let text = await snapshot();
  assert.match(text, /- video "Clip" \[ref=s\d+e\d+\] \[src="http:\/\/127\.0\.0\.1\/clip\.webm" duration=4 currentTime=0 paused=true muted=true controls=true\]/);
  assert.match(text, /- audio "" \[ref=s\d+e\d+\] \[src="" duration=null currentTime=0 paused=true muted=true controls=true\]/);
  assert.doesNotMatch(text, /cannot play/);
  let ref = text.match(/video "Clip" \[ref=(s\d+e\d+)\]/)[1];

  // A click toggles playback through the media API, and reports the state.
  let clicked = await send({ action: 'click', selector: ref });
  assert.equal(clicked.ok, true);
  assert.equal(video.paused, false);
  assert.deepEqual(copy(clicked.media), { src: 'http://127.0.0.1/clip.webm', duration: 4, currentTime: 0,
    paused: false, muted: true, controls: true });
  assert.match(await snapshot(), /video "Clip" .*paused=false/);
  clicked = await send({ action: 'click', selector: '#video' });
  assert.equal(video.paused, true);
  assert.equal(clicked.media.paused, true);

  // A page that handles the click itself, or cancels it, is not toggled a second time.
  const playOnClick = () => video.play();
  video.addEventListener('click', playOnClick);
  clicked = await send({ action: 'click', selector: '#video' });
  assert.equal(video.paused, false, 'the page started playback; Kitsune did not pause it again');
  assert.equal(clicked.media, undefined);
  video.removeEventListener('click', playOnClick);
  const cancel = event => event.preventDefault();
  video.addEventListener('click', cancel);
  await send({ action: 'click', selector: '#video' });
  assert.equal(video.paused, false, 'a cancelled click leaves playback to the page');
  video.removeEventListener('click', cancel);
  video.pause();

  // Clicks on other elements report no media state.
  assert.equal((await send({ action: 'click', selector: '#button' })).media, undefined);

  // The media command: play, seek (after `seeked`), pause, mute and unmute.
  ref = (await snapshot()).match(/video "Clip" \[ref=(s\d+e\d+)\]/)[1];
  let result = await send({ action: 'media', selector: ref, op: 'play' });
  assert.equal(result.ok, true);
  assert.equal(result.tag, 'video');
  assert.equal(result.media.paused, false);
  result = await send({ action: 'media', selector: ref, op: 'seek', time: 2.5 });
  assert.equal(result.media.currentTime, 2.5);
  result = await send({ action: 'media', selector: ref, op: 'pause' });
  assert.equal(result.media.paused, true);
  assert.equal((await send({ action: 'media', selector: ref, op: 'unmute' })).media.muted, false);

  // Unmuted playback refused by the browser is an error that carries the state.
  result = await send({ action: 'media', selector: ref, op: 'play' });
  assert.match(result.error, /play\(\) was refused \(NotAllowedError: play\(\) needs user activation\)/);
  assert.equal(result.reason, 'refused');
  assert.equal(result.media.paused, true);
  clicked = await send({ action: 'click', selector: ref });
  assert.equal(clicked.ok, true, 'the click itself was dispatched');
  assert.equal(clicked.text, '', 'fallback content is not the click text');
  assert.equal(clicked.media.reason, 'refused');
  assert.match(clicked.media.error, /NotAllowedError/);
  assert.equal((await send({ action: 'media', selector: ref, op: 'mute' })).media.muted, true);

  // A background tab that was never shown holds play() pending; Kitsune cancels it and says so.
  document.hidden = true;
  let paused = 0;
  video.addEventListener('pause', () => paused++);
  const pause = video.pause;
  video.pause = function () { this.dispatchEvent(new Event('pause')); pause.call(this); };
  result = await send({ action: 'media', selector: ref, op: 'play' });
  assert.equal(result.reason, 'background_tab');
  assert.match(result.error, /browser_tabs select/);
  assert.equal(result.media.paused, true);
  assert.equal(paused, 1, 'the pending play is cancelled');
  clicked = await send({ action: 'click', selector: ref });
  assert.equal(clicked.media.reason, 'background_tab');
  document.hidden = false;
  video.pause = pause;

  // Invalid commands fail before acting.
  assert.equal((await send({ action: 'media', selector: ref, op: 'stop' })).error,
    'op must be play, pause, seek, mute or unmute');
  assert.equal((await send({ action: 'media', selector: ref, op: 'seek' })).error,
    'seek needs time: a non-negative number of seconds');
  assert.equal((await send({ action: 'media', selector: '#button', op: 'play' })).error,
    'Not a video or audio element: button');
  assert.equal((await send({ action: 'media', selector: '#missing', op: 'play' })).code, 'not_found');
  assert.equal((await send({ action: 'media', selector: 'video, audio', op: 'pause' })).code, 'ambiguous_target');
  assert.equal(video.paused, true);
}

testMedia().then(() => console.log('Media snapshot, click and media command checks passed'))
  .catch(error => { console.error(error); process.exitCode = 1; });
