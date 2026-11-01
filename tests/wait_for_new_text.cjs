// Run with: node tests/wait_for_new_text.cjs (no browser or npm dependencies).
// #402: a new_only text read counts the text's occurrences and is met only once more occur.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

(async () => {
  const body = { tagName: 'BODY', innerText: 'Generate report\nConfirmation code' };
  let listener;
  const context = vm.createContext({
    console, URL, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    location: { href: 'https://example.test/report' },
    window: { innerWidth: 800, innerHeight: 600, scrollX: 0, scrollY: 0,
      __kitsuneDialogSnapshot: () => '{"dialogs":[]}',
      getComputedStyle: () => ({ display: 'block', visibility: 'visible', getPropertyValue: () => '' }) },
    document: { body, readyState: 'complete', documentElement: { appendChild() {} },
      createElement: () => ({ remove() {} }), querySelector: () => null, querySelectorAll: () => [] },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener({ action: 'waitForText', timeout: 0, ...msg }, {}, resolve));

  // Without text_seen, the label's text meets the wait and no count is reported.
  const plain = await send({ text: 'code' });
  assert.equal(plain.ok, true);
  assert.equal(plain.text_count, undefined);

  // The first new_only read (text_seen 0) is met by the label and counts it.
  const start = await send({ text: 'code', text_seen: 0 });
  assert.equal(start.ok, true);
  assert.equal(start.text_count, 1);

  // Later reads wait past that occurrence.
  const unmet = await send({ text: 'code', text_seen: 1 });
  assert.equal(unmet.ok, false);
  assert.equal(unmet.text_count, 1);
  assert.equal(unmet.error, 'Timed out after 0ms waiting for new visible text');

  body.innerText += '\nReport ready. Confirmation code: 4A159A';
  const met = await send({ text: 'code', text_seen: 1 });
  assert.equal(met.ok, true);
  assert.equal(met.text_count, 2);

  // text_seen needs text and a non-negative integer.
  for (const msg of [{ text_gone: 'code', text_seen: 0 }, { text: 'code', text_seen: -1 },
                     { text: 'code', text_seen: 1.5 }]) {
    assert.match((await send(msg)).error, /Provide non-empty text/);
  }

  console.log('Wait for new text checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
