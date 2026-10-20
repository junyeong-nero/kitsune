// Run with: node tests/signed_build.cjs (no browser or npm dependencies; needs python3).
// Builds both extension variants and checks agent-supplied JavaScript only runs in the dev build.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

function build(variant) {
  const out = fs.mkdtempSync(path.join(os.tmpdir(), `kitsune-${variant}-`));
  const python = process.env.PYTHON || 'python3';
  execFileSync(python, ['scripts/build_extension.py', variant, '--out', out], { stdio: 'pipe' });
  return out;
}

function loadContent(dir) {
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, CSS: { escape: x => x }, setTimeout,
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: { body: {}, querySelector: () => null, querySelectorAll: () => [] },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync(path.join(dir, 'content/content.js'), 'utf8'), context);
  return msg => new Promise(resolve => {
    const keepOpen = listener(msg, {}, resolve);
    if (!keepOpen) setTimeout(() => resolve(undefined), 0);
  });
}

async function main() {
  const signed = build('signed');
  const dev = build('dev');
  try {
    const signedSend = loadContent(signed);
    for (const msg of [{ action: 'executeJS', code: '1 + 1' }, { action: 'waitForResult', code: '1', timeout: 10 }]) {
      const reply = await signedSend(msg);
      assert.equal(reply.devBuildRequired, true, msg.action);
      assert.match(reply.error, /developer extension build/);
    }
    const html = await signedSend({ action: 'setEditableContent', selector: '#x', value: '<b>x</b>', format: 'html' });
    assert.equal(html.devBuildRequired, true);
    assert.equal((await signedSend({ action: 'keepAlive' })).ok, true);

    const devReply = await loadContent(dev)({ action: 'executeJS', code: '1 + 1' });
    assert.equal(devReply.result, '2');

    const manifest = JSON.parse(fs.readFileSync(path.join(signed, 'manifest.json'), 'utf8'));
    const version = /^version = "(.+)"$/m.exec(fs.readFileSync('pyproject.toml', 'utf8'))[1];
    assert.equal(manifest.version, version);
  } finally {
    fs.rmSync(signed, { recursive: true, force: true });
    fs.rmSync(dev, { recursive: true, force: true });
  }
  console.log('signed build tests passed');
}

main().catch(error => { console.error(error); process.exit(1); });
