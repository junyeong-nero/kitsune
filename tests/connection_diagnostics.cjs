// Run with: node tests/connection_diagnostics.cjs (no browser or npm dependencies).
// Issue #34: the extension reports its browser state to the bridge and names session and
// host-permission failures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function event() {
  const listeners = new Set();
  return { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
    fire: (...args) => [...listeners].forEach(fn => fn(...args)) };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

// The extension's files as fetch serves them: { manifest, 'path': text }, manifest being what
// getManifest() returns. Without them fetch fails.
function background({ tabs = {}, windows = 1, granted = () => true, files = null, browserInfo = null } = {}) {
  const sockets = [];
  const sent = [];
  const contains = [];
  class WebSocket {
    static OPEN = 1;
    constructor(url) { Object.assign(this, { url, readyState: WebSocket.OPEN, messages: [] }); sockets.push(this); }
    send(message) { this.messages.push(JSON.parse(message)); }
    close() { this.readyState = 3; }
  }
  const state = { windows, granted };
  const browser = {
    tabs: {
      onCreated: event(), onUpdated: event(), onRemoved: event(),
      query: async query => Object.values(tabs).filter(tab => !query.active || tab.active).map(tab => ({ ...tab })),
      get: async id => { if (!tabs[id]) throw new Error(`Invalid tab ID: ${id}`); return { ...tabs[id] }; },
      sendMessage: async (id, msg) => {
        if (msg.action === 'ping') throw new Error('Could not establish connection. Receiving end does not exist.');
        // The user-activity probe only reads; sent records commands.
        if (msg.action === 'userActivity') return { active: false };
        sent.push(msg);
        return { ok: true };
      },
      executeScript: async () => { throw new Error('Missing host permission for the tab'); },
    },
    windows: {
      onCreated: event(), onRemoved: event(),
      getAll: async query => {
        assert.equal(JSON.stringify(query), '{"windowTypes":["normal"]}');
        return Array.from({ length: state.windows }, (_, id) => ({ id }));
      },
    },
    permissions: {
      onAdded: event(), onRemoved: event(),
      contains: async ({ origins }) => { contains.push(...origins); return state.granted(origins[0]); },
    },
    runtime: {
      onMessage: event(), onStartup: event(), onInstalled: event(),
      getManifest: () => ({ version: '0.2.1', ...files?.manifest }),
      getURL: path => `moz-extension://kitsune/${path}`,
      ...(browserInfo ? { getBrowserInfo: browserInfo } : {}),
    },
    webRequest: { onBeforeRequest: event(), onHeadersReceived: event() },
  };
  const fetch = async url => {
    const text = files?.[url.replace('moz-extension://kitsune/', '')];
    if (typeof text !== 'string') throw new TypeError('NetworkError when attempting to fetch resource.');
    return new Response(text);
  };
  const context = vm.createContext({
    browser, URL, WebSocket, setTimeout, clearTimeout, Date, fetch, crypto, TextEncoder, Blob,
    setInterval: () => 0, clearInterval: () => {},
    console: { log() {}, error() {} },
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  return {
    browser, sent, contains, state,
    socket: () => sockets[sockets.length - 1],
    reconnect: () => context.connect(),
    send: command => context.handleCommand({ includeSnapshot: false, ...command })
      .then(result => JSON.parse(JSON.stringify(result))),
  };
}

const page = (id, url, extra = {}) => ({ id, url, title: `Tab ${id}`, status: 'complete', active: false, ...extra });

async function testBrowserStateReachesTheBridge() {
  const h = background({ windows: 1 });
  const ws = h.socket();
  ws.onopen();
  await flush();
  assert.deepEqual(ws.messages.map(m => m.type), ['hello', 'state']);
  assert.deepEqual(ws.messages[1], { type: 'state', windows: 1, allSites: true });
  assert.deepEqual(h.contains, ['<all_urls>']);

  // Window and permission changes are reported again.
  h.state.windows = 0;
  h.browser.windows.onRemoved.fire(1);
  await flush();
  assert.deepEqual(ws.messages.at(-1), { type: 'state', windows: 0, allSites: true });
  h.state.granted = () => false;
  h.browser.permissions.onRemoved.fire({ origins: ['<all_urls>'] });
  await flush();
  assert.deepEqual(ws.messages.at(-1), { type: 'state', windows: 0, allSites: false });

  // Nothing is sent without an open connection.
  ws.readyState = 3;
  const count = ws.messages.length;
  h.browser.windows.onCreated.fire({ id: 2 });
  await flush();
  assert.equal(ws.messages.length, count);
}

// The same files as SourceHashTests in tests/test_connection_diagnostics.py: background scripts, then
// content scripts, each once, and the manifest.json bytes. The bridge compares both fingerprints with
// the unpacked extension on disk.
const MANIFEST_TEXT = '{"background": {"scripts": ["bg/a.js"]}, "content_scripts": [{"js": ["content/b.js", "bg/a.js"]}]}';
const HASH_FILES = {
  manifest: JSON.parse(MANIFEST_TEXT), 'manifest.json': MANIFEST_TEXT, 'bg/a.js': 'A\n', 'content/b.js': 'B\n',
};
const SOURCE_HASH = '9825576d37c4d475';
const MANIFEST_HASH = '2655402d232f0401';

async function testHelloCarriesTheSourceAndManifestHashes() {
  const h = background({ files: HASH_FILES });
  const ws = h.socket();
  await ws.onopen();
  assert.deepEqual(ws.messages[0],
    { type: 'hello', version: '0.2.1', build: 'dev', sourceHash: SOURCE_HASH, manifestHash: MANIFEST_HASH });

  // Firefox's getManifest() lists script paths as absolute moz-extension:// URLs (seen in a real
  // browser, #121); the hash still covers the manifest's relative paths.
  const absolute = background({ files: { ...HASH_FILES, manifest: {
    background: { scripts: ['moz-extension://kitsune/bg/a.js'] },
    content_scripts: [{ js: ['moz-extension://kitsune/content/b.js', '/bg/a.js'] }],
  } } });
  await absolute.socket().onopen();
  assert.equal(absolute.socket().messages[0].sourceHash, SOURCE_HASH, 'absolute script URLs');

  // Unknown, never an error: the bridge then skips that part of the build check.
  for (const [files, sourceHash, manifestHash] of [
    [null, null, null],
    [{ ...HASH_FILES, 'content/b.js': undefined }, null, MANIFEST_HASH],
    [{ ...HASH_FILES, 'manifest.json': undefined }, SOURCE_HASH, null],
  ]) {
    const unknown = background({ files });
    await unknown.socket().onopen();
    assert.deepEqual(unknown.socket().messages[0],
      { type: 'hello', version: '0.2.1', build: 'dev', sourceHash, manifestHash });
  }
}

async function testRuntimeInfoIsReadOnlyAndConnectionScoped() {
  const info = { name: 'Zen', vendor: 'Mozilla', version: '1.24b', buildID: '20261029123729' };
  const h = background({ browserInfo: async () => ({ ...info, privateField: 'not sent' }) });
  await h.socket().onopen();
  await flush();
  assert.deepEqual(h.socket().messages.find(m => m.type === 'browser_info'), { type: 'browser_info', info });
  assert.deepEqual(h.sent, [], 'runtime metadata needs no page command');

  const failed = background({ browserInfo: async () => { throw new Error('unsupported'); } });
  await failed.socket().onopen();
  await flush();
  assert.deepEqual(failed.socket().messages.map(m => m.type), ['hello', 'state']);

  let resolve;
  const delayed = background({ browserInfo: () => new Promise(r => { resolve = r; }) });
  const old = delayed.socket();
  await old.onopen();
  await flush();
  assert.deepEqual(old.messages.map(m => m.type), ['hello', 'state'], 'info must not block startup');
  delayed.reconnect();
  resolve(info);
  await flush();
  assert.equal(old.messages.some(m => m.type === 'browser_info'), false);
  assert.deepEqual(delayed.socket().messages, [], 'late info must not reach the replacement socket');
}

async function testNoActiveTabIsAnUnavailableWindow() {
  const h = background({ tabs: {}, windows: 0 });
  for (const [command, error] of [
    [{ action: 'getSnapshot' }, 'No active tab found'],
    [{ action: 'click', selector: '#go' }, 'No active tab found'],
    [{ action: 'waitForUrl', pattern: 'x', timeout: 0 }, 'No active tab found'],
    [{ action: 'navigate', url: 'https://example.com/' }, 'No active tab'],
  ]) {
    const result = await h.send(command);
    assert.equal(result.error, error, 'error text is unchanged');
    assert.equal(result.code, 'browser_window_unavailable', command.action);
    assert.equal(result.retryable, true);
    assert.match(result.hint, /normal browser window.*Private Windows.*tab_id/);
  }
}

async function testHostPermissionIsToldApartFromErrorPages() {
  const tabs = {
    1: page(1, 'https://addons.mozilla.org/en-US/firefox/', { active: true }),
    2: page(2, 'http://127.0.0.1:8000/private'),
    3: page(3, 'https://example.com/404'),
  };
  const h = background({ tabs, granted: origin => origin !== 'http://127.0.0.1/*' });

  const restricted = await h.send({ action: 'getSnapshot', tabId: 1 });
  assert.equal(restricted.error, 'Cannot inject content script: Missing host permission for the tab');
  assert.deepEqual([restricted.code, restricted.retryable, restricted.state.reason],
    ['host_permission_denied', true, 'restricted_domain']);
  assert.match(restricted.hint, /about:addons/);

  const withheld = await h.send({ action: 'click', tabId: 2, selector: '#go' });
  assert.deepEqual([withheld.code, withheld.state.reason, withheld.state.url],
    ['host_permission_denied', 'not_granted', 'http://127.0.0.1:8000/private']);
  assert.ok(h.contains.includes('http://127.0.0.1/*'), 'match patterns have no port');

  // With site access, the same refusal is the page's own error document.
  h.browser.webRequest.onHeadersReceived.fire({ tabId: 3, requestId: 1, url: tabs[3].url, statusCode: 404 });
  const errorPage = await h.send({ action: 'getSnapshot', tabId: 3 });
  assert.deepEqual([errorPage.code, errorPage.state.httpStatus], ['error_page', 404]);
  assert.deepEqual(h.sent, [], 'nothing reached a page');
}

// Known limitation (#118, docs/api-reference.md#connection-diagnosis): Firefox tells extensions
// neither which sites Mozilla quarantines nor that a refusal was one; permissions.contains still
// grants them. So a quarantined site that refuses the content script stays error_page.
async function testQuarantinedDomainIsAnErrorPage() {
  const tabs = { 1: page(1, 'https://quarantined.example/', { active: true }) };
  const h = background({ tabs });
  const result = await h.send({ action: 'getSnapshot', tabId: 1 });
  assert.deepEqual([result.code, result.retryable, result.state.reason], ['error_page', false, undefined]);
  assert.equal(result.error,
    'The tab shows a page with no scriptable document. Cannot inject content script: Missing host permission for the tab');
  assert.deepEqual(h.contains, ['https://quarantined.example/*'], 'site access was checked');
  assert.deepEqual(h.sent, [], 'nothing reached the page');
}

(async () => {
  await testBrowserStateReachesTheBridge();
  await testHelloCarriesTheSourceAndManifestHashes();
  await testRuntimeInfoIsReadOnlyAndConnectionScoped();
  await testNoActiveTabIsAnUnavailableWindow();
  await testHostPermissionIsToldApartFromErrorPages();
  await testQuarantinedDomainIsAnErrorPage();
  console.log('connection diagnostics ok');
})().catch(error => { console.error(error); process.exit(1); });
