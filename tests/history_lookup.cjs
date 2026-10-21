// Run with: node tests/history_lookup.cjs (no browser or npm dependencies).
// History and bookmark lookup (#140): the extension handlers in background/history.js with a
// mocked browser, and the options page that is the only place the permission is requested.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const event = () => ({ addListener() {}, removeListener() {} });
const DAY = 86_400_000;
const NOW = Date.UTC(2026, 9, 21, 12);
const iso = ms => new Date(ms).toISOString();

// granted: the optional permissions the user allowed. Firefox exposes browser.history and
// browser.bookmarks only once granted, so the getters count accesses made without the grant.
function harness({ granted = [], history = [], visits = {}, bookmarks = [], exposed = granted } = {}) {
  const calls = { search: [], getVisits: [], bookmarkSearch: [], getRecent: [], ungrantedApi: 0 };
  const historyApi = {
    search: async query => { calls.search.push(JSON.parse(JSON.stringify(query))); return history.map(item => ({ ...item })); },
    getVisits: async ({ url }) => { calls.getVisits.push(url); return (visits[url] || []).map(v => ({ ...v })); },
  };
  const nodes = new Map(bookmarks.map(node => [node.id, node]));
  const bookmarksApi = {
    search: async query => { calls.bookmarkSearch.push(query); return bookmarks.filter(node => node.url); },
    getRecent: async n => { calls.getRecent.push(n); return bookmarks.filter(node => node.url); },
    get: async id => (nodes.has(id) ? [{ ...nodes.get(id) }] : Promise.reject(new Error('not found'))),
  };
  const browser = {
    tabs: { onCreated: event(), onUpdated: event(), onRemoved: event(), query: async () => [] },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
    permissions: {
      contains: async ({ permissions }) => permissions.every(p => granted.includes(p)),
      request: async () => { throw new Error('the background must never request permissions'); },
    },
    get history() { if (!granted.includes('history')) calls.ungrantedApi++; return exposed.includes('history') ? historyApi : undefined; },
    get bookmarks() { if (!granted.includes('bookmarks')) calls.ungrantedApi++; return exposed.includes('bookmarks') ? bookmarksApi : undefined; },
  };
  const context = vm.createContext({
    browser, URL, console: { log() {}, error() {} },
    WebSocket: class { close() {} },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('extension/background/history.js', 'utf8'), context);
  return {
    calls,
    send: command => context.handleCommand({ includeSnapshot: false, ...command })
      .then(result => JSON.parse(JSON.stringify(result))),
    redact: url => context.redactHistoryUrl(url),
  };
}

const item = (url, daysAgo, visitCount = 1, title = '') =>
  ({ id: url, url, title, lastVisitTime: NOW - daysAgo * DAY, visitCount });

async function permissionRequired() {
  const none = harness();
  for (const [action, permission] of [['historySearch', 'history'], ['historyVisits', 'history'],
    ['bookmarksSearch', 'bookmarks']]) {
    const result = await none.send({ action, query: 'x', url: 'https://example.com/', startTime: 0 });
    assert.equal(result.code, 'permission_required', action);
    assert.equal(result.retryable, false, action);
    assert.deepEqual(result.state, { permission });
    assert.match(result.hint, /options/i);
    assert.match(result.hint, /cannot grant/);
  }
  assert.equal(none.calls.ungrantedApi, 0, 'the API is not touched before the permission check');

  // Each tool needs its own permission.
  const historyOnly = harness({ granted: ['history'] });
  assert.equal((await historyOnly.send({ action: 'bookmarksSearch', query: 'x' })).code, 'permission_required');
  assert.equal((await historyOnly.send({ action: 'historySearch', query: 'x' })).ok, true);

  // Granted, but this copy of the extension does not expose the API yet: reload, not re-grant.
  const stale = await harness({ granted: ['history'], exposed: [] }).send({ action: 'historySearch', query: 'x' });
  assert.equal(stale.code, 'permission_required');
  assert.match(stale.hint, /Reload Kitsune/);
}

async function searchScopeAndShape() {
  const h = harness({
    granted: ['history'],
    history: [
      item('https://docs.example.com/guide', 1, 3, 'Guide'),
      item('https://example.com/', 0.5, 7, 'Home'),
      item('https://bank.example.com/accounts', 0.2),
      item('https://other.test/page', 2),
      item('https://old.test/', 40),
      item('file:///Users/someone/notes.txt', 0.1),
      item('data:text/html,<h1>secret page</h1>', 0.1),
      item('about:preferences', 0.1),
    ],
  });
  const startTime = NOW - 30 * DAY;
  const all = await h.send({ action: 'historySearch', query: 'guide', startTime, limit: 20 });
  assert.deepEqual(h.calls.search[0], { text: 'guide', startTime, maxResults: 1000 });
  assert.deepEqual(all, {
    ok: true, count: 4, results: [
      { url: 'https://bank.example.com/accounts', title: '', last_visit: iso(NOW - 0.2 * DAY), visit_count: 1 },
      { url: 'https://example.com/', title: 'Home', last_visit: iso(NOW - 0.5 * DAY), visit_count: 7 },
      { url: 'https://docs.example.com/guide', title: 'Guide', last_visit: iso(NOW - DAY), visit_count: 3 },
      { url: 'https://other.test/page', title: '', last_visit: iso(NOW - 2 * DAY), visit_count: 1 },
    ],
  }, 'newest first; outside the window and non-http(s) URLs are dropped');

  const denied = await h.send({ action: 'historySearch', query: '', startTime, denyDomains: ['bank.example.com', 'other.test'] });
  assert.deepEqual(denied.results.map(r => r.url), ['https://example.com/', 'https://docs.example.com/guide']);

  const allowed = await h.send({ action: 'historySearch', query: '', startTime, allowDomains: ['example.com'],
    denyDomains: ['bank.example.com'] });
  assert.deepEqual(allowed.results.map(r => r.url), ['https://example.com/', 'https://docs.example.com/guide'],
    'an allowed domain covers subdomains; deny wins');

  const limited = await h.send({ action: 'historySearch', query: '', startTime, limit: 2 });
  assert.equal(limited.count, 2);
  assert.equal(limited.truncated, true);
  const clamped = await h.send({ action: 'historySearch', query: '', startTime, limit: 5000 });
  assert.equal(clamped.count, 4);
  assert.equal(clamped.truncated, undefined);
}

async function searchRedactsAndMerges() {
  const h = harness({
    granted: ['history'],
    history: [
      item('https://mail.test/login?next=%2Finbox&token=abc123', 1, 2, 'Sign in (old)'),
      item('https://mail.test/login?next=%2Finbox&token=zzz999', 0.5, 3, 'Sign in'),
      item('https://u:hunter2@intranet.test/', 0.4, 1, 'Intranet'),
    ],
  });
  const result = await h.send({ action: 'historySearch', query: '', startTime: 0 });
  assert.deepEqual(result.results, [
    { url: 'https://intranet.test/', title: 'Intranet', last_visit: iso(NOW - 0.4 * DAY), visit_count: 1 },
    { url: 'https://mail.test/login?next=%2Finbox&token=[redacted]', title: 'Sign in',
      last_visit: iso(NOW - 0.5 * DAY), visit_count: 5 },
  ], 'one entry per redacted URL, with the newest title and the summed visit count');
  for (const secret of ['hunter2', 'abc123', 'zzz999']) assert.ok(!JSON.stringify(result).includes(secret), secret);
}

function redactionRules() {
  const { redact } = harness();
  const jwt = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl';
  const cases = [
    // Sensitive parameter names, by word or name part.
    ['https://a.test/cb?code=4/0AX4XfWh&state=xyz', 'https://a.test/cb?code=[redacted]&state=xyz'],
    ['https://a.test/?access_token=abc&q=kitsune', 'https://a.test/?access_token=[redacted]&q=kitsune'],
    ['https://a.test/?apiKey=abc&page=2', 'https://a.test/?apiKey=[redacted]&page=2'],
    ['https://a.test/?X-Amz-Signature=ab12&X-Amz-Credential=AK%2F2026', 'https://a.test/?X-Amz-Signature=[redacted]&X-Amz-Credential=[redacted]'],
    ['https://a.test/?PHPSESSID=abc&sid=1&password=pw', 'https://a.test/?PHPSESSID=[redacted]&sid=[redacted]&password=[redacted]'],
    ['https://a.test/reset?resetToken=abc', 'https://a.test/reset?resetToken=[redacted]'],
    // Credential-like values under any name.
    [`https://a.test/?x=${jwt}`, 'https://a.test/?x=[redacted]'],
    ['https://a.test/?h=0123456789abcdef0123456789abcdef', 'https://a.test/?h=[redacted]'],
    ['https://a.test/?r=aZ3kQ9xP2mL7vB4nC8wE1rT6yU5iO0pAs', 'https://a.test/?r=[redacted]'],
    ['https://a.test/?gh=ghp_abcdefghijklmnop', 'https://a.test/?gh=[redacted]'],
    ['https://a.test/?bearer_header=Bearer%20abc', 'https://a.test/?bearer_header=[redacted]'],
    // Fragments: OAuth parameters, routes with a query, and a bare credential.
    ['https://a.test/cb#access_token=abc&token_type=bearer&expires_in=3600',
      'https://a.test/cb#access_token=[redacted]&token_type=[redacted]&expires_in=3600'],
    ['https://a.test/app#/inbox?token=abc&tab=2', 'https://a.test/app#/inbox?token=[redacted]&tab=2'],
    [`https://a.test/#${jwt}`, 'https://a.test/#[redacted]'],
    // User info is dropped.
    ['https://user:pw@a.test/path', 'https://a.test/path'],
    // Ordinary URLs are unchanged, including long lowercase slugs and empty values.
    ['https://a.test/search?q=firefox+history&page=2#results', 'https://a.test/search?q=firefox+history&page=2#results'],
    ['https://a.test/?utm_campaign=summer2026_newsletter_launch_week', 'https://a.test/?utm_campaign=summer2026_newsletter_launch_week'],
    ['https://a.test/?token=&flag', 'https://a.test/?token=&flag'],
  ];
  for (const [input, expected] of cases) assert.equal(redact(input), expected, input);
}

async function visits() {
  const page = 'https://example.com/article';
  const tokenA = 'https://example.com/dl?token=aaa', tokenB = 'https://example.com/dl?token=bbb';
  const h = harness({
    granted: ['history'],
    history: [item(tokenA, 1), item(tokenB, 2), item('https://example.com/dl?token=ccc&x=1', 1)],
    visits: {
      [page]: [
        { visitId: '11', visitTime: NOW - 2 * DAY, referringVisitId: '-1', transition: 'typed' },
        { visitId: '12', visitTime: NOW - DAY, referringVisitId: '7', transition: 'link' },
        { visitId: '10', visitTime: NOW - 45 * DAY, referringVisitId: '-1', transition: 'link' },
      ],
      [tokenA]: [{ visitId: '21', visitTime: NOW - DAY, referringVisitId: '-1', transition: 'link' }],
      [tokenB]: [{ visitId: '22', visitTime: NOW - 2 * DAY, referringVisitId: '-1', transition: 'auto_bookmark' }],
    },
  });
  const startTime = NOW - 30 * DAY;
  assert.deepEqual(await h.send({ action: 'historyVisits', url: page, startTime }), {
    ok: true, url: page, count: 2, visits: [
      { visit_time: iso(NOW - DAY), transition: 'link', visit_id: '12', referring_visit_id: '7' },
      { visit_time: iso(NOW - 2 * DAY), transition: 'typed', visit_id: '11' },
    ],
  }, 'newest first, inside the window, referrer only where Firefox knows it');
  assert.deepEqual(h.calls.getVisits, [page]);

  // A redacted URL from historySearch resolves to the visited URLs it stands for.
  const redacted = await h.send({ action: 'historyVisits', url: 'https://example.com/dl?token=[redacted]', startTime });
  assert.equal(redacted.url, 'https://example.com/dl?token=[redacted]');
  assert.deepEqual(redacted.visits.map(v => v.visit_id), ['21', '22']);
  assert.deepEqual(h.calls.search.at(-1), { text: 'example.com', startTime, maxResults: 1000 });
  assert.ok(!JSON.stringify(redacted).includes('aaa'));

  h.calls.getVisits.length = 0;
  for (const url of ['https://bank.test/', 'file:///etc/passwd', 42]) {
    const outside = await h.send({ action: 'historyVisits', url, startTime, denyDomains: ['bank.test'] });
    assert.match(outside.error, /scope/);
  }
  assert.deepEqual(h.calls.getVisits, [], 'out-of-scope URLs are not looked up');
}

async function bookmarks() {
  const h = harness({
    granted: ['bookmarks'],
    bookmarks: [
      { id: 'root________', title: '' },
      { id: 'toolbar_____', parentId: 'root________', title: 'Bookmarks Toolbar' },
      { id: 'work', parentId: 'toolbar_____', title: 'Work' },
      { id: 'b1', parentId: 'work', title: 'Tracker', url: 'https://tracker.example.com/board?sessionid=s3cr3t' },
      { id: 'b2', parentId: 'toolbar_____', title: 'Bank', url: 'https://bank.test/' },
      { id: 'b3', parentId: 'root________', title: 'Local', url: 'file:///Users/someone/' },
      { id: 'b4', parentId: 'work', title: 'Docs', url: 'https://docs.example.com/' },
    ],
  });
  assert.deepEqual(await h.send({ action: 'bookmarksSearch', query: 'work', limit: 20, denyDomains: ['bank.test'] }), {
    ok: true, count: 2, results: [
      { url: 'https://tracker.example.com/board?sessionid=[redacted]', title: 'Tracker', folder: 'Bookmarks Toolbar/Work' },
      { url: 'https://docs.example.com/', title: 'Docs', folder: 'Bookmarks Toolbar/Work' },
    ],
  });
  assert.deepEqual(h.calls.bookmarkSearch, ['work']);
  const limited = await h.send({ action: 'bookmarksSearch', query: '', limit: 1, allowDomains: ['example.com'] });
  assert.deepEqual(h.calls.getRecent, [1000], 'an empty query lists recent bookmarks');
  assert.deepEqual(limited.results.map(r => r.title), ['Tracker']);
  assert.equal(limited.truncated, true);
}

async function optionsPage() {
  const manifest = JSON.parse(fs.readFileSync('extension/manifest.json', 'utf8'));
  assert.deepEqual(manifest.optional_permissions, ['history', 'bookmarks']);
  assert.ok(!manifest.permissions.includes('history') && !manifest.permissions.includes('bookmarks'));
  assert.ok(fs.existsSync(`extension/${manifest.options_ui.page}`));
  assert.ok(manifest.background.scripts.includes('background/history.js'));
  // The agent can never grant it: only the options page's click handler requests permissions.
  for (const file of fs.readdirSync('extension/background')) {
    assert.doesNotMatch(fs.readFileSync(`extension/background/${file}`, 'utf8'), /permissions\.request/, file);
  }

  const granted = new Set();
  const requests = [], removals = [];
  const elements = {};
  const element = id => (elements[id] ??= {
    id, hidden: false, disabled: false, textContent: '', listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; },
  });
  const browser = {
    permissions: {
      contains: async ({ permissions }) => permissions.every(p => granted.has(p)),
      request: async ({ permissions }) => { requests.push([...permissions]); permissions.forEach(p => granted.add(p)); return true; },
      remove: async ({ permissions }) => { removals.push([...permissions]); permissions.forEach(p => granted.delete(p)); return true; },
      onAdded: event(), onRemoved: event(),
    },
  };
  vm.runInContext(fs.readFileSync('extension/options/options.js', 'utf8'),
    vm.createContext({ browser, document: { getElementById: element } }));
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(requests, [], 'loading the page requests nothing');
  assert.equal(elements['history-status'].textContent, 'not allowed');
  assert.equal(elements.revoke.disabled, true);

  elements.grant.listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(requests, [['history', 'bookmarks']]);
  assert.equal(elements['bookmarks-status'].textContent, 'allowed');
  assert.equal(elements.grant.disabled, true);

  elements.revoke.listeners.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(removals, [['history', 'bookmarks']]);
  assert.equal(elements['history-status'].textContent, 'not allowed');
}

async function main() {
  await permissionRequired();
  await searchScopeAndShape();
  await searchRedactsAndMerges();
  redactionRules();
  await visits();
  await bookmarks();
  await optionsPage();
  console.log('history lookup tests passed');
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
