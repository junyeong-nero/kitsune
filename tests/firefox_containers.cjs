// Run with: node tests/firefox_containers.cjs (no browser or npm dependencies).
// #495: newTab opens a tab in a Firefox container (contextual identity) named by container, and
// getTabs reports the container of each tab that is in one.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function event() {
  const listeners = new Set();
  return { addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn) };
}

// identities: what browser.contextualIdentities.query knows; without it the API is absent, as in a
// build whose manifest lacks the contextualIdentities permission. createError: what tabs.create
// rejects a cookieStoreId with. queryError: what contextualIdentities.query rejects with.
function harness({ tabs = [], identities = null, createError = null, queryError = null } = {}) {
  const open = new Map(tabs.map(tab => [tab.id, tab]));
  const h = {
    creates: [],
    send: command => context.handleCommand({ includeSnapshot: false, ...command })
      .then(result => JSON.parse(JSON.stringify(result))),
  };
  const browser = {
    tabs: {
      onCreated: event(), onUpdated: event(), onRemoved: event(),
      query: async () => [...open.values()].map(tab => ({ ...tab })),
      create: async props => {
        h.creates.push({ ...props });
        if (createError && props.cookieStoreId) throw new Error(createError);
        const tab = { id: 20 + open.size, url: props.url, title: '', status: 'complete', active: props.active,
          windowId: 1, index: open.size, cookieStoreId: props.cookieStoreId || 'firefox-default' };
        open.set(tab.id, tab);
        return { ...tab };
      },
      get: async id => {
        if (!open.has(id)) throw new Error(`Invalid tab ID: ${id}`);
        return { ...open.get(id) };
      },
      sendMessage: async () => { throw new Error('Unexpected content message'); },
      executeScript: async () => { throw new Error('Unexpected content injection'); },
    },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
  };
  if (identities) {
    browser.contextualIdentities = {
      query: async ({ name }) => {
        if (queryError) throw new Error(queryError);
        return identities.filter(identity => identity.name === name).map(identity => ({ ...identity }));
      },
    };
  }
  const context = vm.createContext({
    browser, URL, console: { log() {}, error() {} },
    WebSocket: class { close() {} },
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  for (const tab of tabs) if (tab.agent) context.markAgentTab(tab.id, 'owner-1');
  return h;
}

const HINT = 'Pass a cookieStoreId (firefox-container-<n>) as container; browser_tabs action=list ' +
  'shows it on each tab that is in a container.';

function unavailable(container, reason, error, extra = {}) {
  return { error, code: 'container_unavailable', retryable: true, hint: HINT, state: { container, reason, ...extra } };
}

async function main() {
  const url = 'https://example.com/';
  // cookies already suffices for cookieStoreId; contextualIdentities would force containers on.
  const manifest = JSON.parse(fs.readFileSync('extension/manifest.json', 'utf8'));
  assert.ok(manifest.permissions.includes('cookies'));
  assert.ok(![...manifest.permissions, ...manifest.optional_permissions].includes('contextualIdentities'));

  // Without container the tab opens in the default store as before: tabs.create gets no cookieStoreId.
  const plain = harness();
  assert.deepEqual(await plain.send({ action: 'newTab', url }), { ok: true, tabId: 20 });
  assert.deepEqual(plain.creates, [{ url, active: false }]);
  for (const container of [null, '']) {
    const omitted = harness();
    await omitted.send({ action: 'newTab', url, container });
    assert.deepEqual(omitted.creates, [{ url, active: false }], `container ${JSON.stringify(container)}`);
  }

  // An explicit newTab does not inherit the container of the caller's own tab, even the shown one.
  const beside = harness({ tabs: [{ id: 1, url, title: 'Mail', status: 'complete', active: true, windowId: 1,
    index: 0, cookieStoreId: 'firefox-container-2', agent: true }] });
  assert.deepEqual(await beside.send({ action: 'newTab', url, ownerId: 'owner-1' }), { ok: true, tabId: 21 });
  assert.deepEqual(beside.creates, [{ url, active: false }]);

  // A cookieStoreId goes to tabs.create as it is, with and without wait. The result names the
  // tab's store, which tells the bridge that this extension did not ignore container.
  const stored = harness();
  assert.deepEqual(await stored.send({ action: 'newTab', url, container: 'firefox-container-1' }),
    { ok: true, tabId: 20, cookieStoreId: 'firefox-container-1' });
  assert.deepEqual(await stored.send({ action: 'newTab', url: 'about:blank', container: 'firefox-container-12',
    wait: true }), { ok: true, tabId: 21, url: 'about:blank', status: 'complete', cookieStoreId: 'firefox-container-12' });
  assert.deepEqual(await stored.send({ action: 'newTab', url, container: 'firefox-default' }),
    { ok: true, tabId: 22, cookieStoreId: 'firefox-default' });
  assert.deepEqual(stored.creates, [
    { url, active: false, cookieStoreId: 'firefox-container-1' },
    { url: 'about:blank', active: false, cookieStoreId: 'firefox-container-12' },
    { url, active: false, cookieStoreId: 'firefox-default' },
  ]);

  // getTabs names the container of a tab that is in one; a tab in the default store has no field.
  assert.deepEqual((await stored.send({ action: 'getTabs' })).tabs.map(tab => [tab.id, tab.cookieStoreId]),
    [[20, 'firefox-container-1'], [21, 'firefox-container-12'], [22, undefined]]);
  const listed = harness({ tabs: [
    { id: 1, url, title: 'A', active: true, windowId: 1, index: 0 },  // no cookies permission: no field
    { id: 2, url, title: 'B', active: false, windowId: 1, index: 1, cookieStoreId: 'firefox-private' },
  ] });
  assert.deepEqual((await listed.send({ action: 'getTabs' })).tabs, [
    { id: 1, url, title: 'A', active: true, windowId: 1, index: 0, owned: false },
    { id: 2, url, title: 'B', active: false, windowId: 1, index: 1, owned: false, cookieStoreId: 'firefox-private' },
  ]);

  // A name needs browser.contextualIdentities, which this build does not have: no tab opens,
  // rather than one in the default store.
  const nameless = harness();
  for (const container of ['Personal', 'firefox-container-x', ' firefox-container-1']) {
    assert.deepEqual(await nameless.send({ action: 'newTab', url, container }), unavailable(container,
      'names_unsupported', 'This Kitsune build cannot resolve container names; no tab was opened'));
  }
  assert.deepEqual(nameless.creates, []);
  assert.deepEqual((await nameless.send({ action: 'getTabs' })).tabs, []);

  // With the API, a name that one container has is that container.
  const identities = [
    { name: 'Personal', cookieStoreId: 'firefox-container-1' },
    { name: 'Work', cookieStoreId: 'firefox-container-2' },
    { name: 'Work', cookieStoreId: 'firefox-container-7' },
  ];
  const named = harness({ identities });
  assert.deepEqual(await named.send({ action: 'newTab', url, container: 'Personal' }),
    { ok: true, tabId: 20, cookieStoreId: 'firefox-container-1' });
  assert.deepEqual(named.creates, [{ url, active: false, cookieStoreId: 'firefox-container-1' }]);
  // A name no container has, or several have, opens nothing.
  assert.deepEqual(await named.send({ action: 'newTab', url, container: 'personal' }),
    unavailable('personal', 'not_found', 'No container is named "personal"; no tab was opened'));
  assert.deepEqual(await named.send({ action: 'newTab', url, container: 'Work' }),
    unavailable('Work', 'ambiguous', 'Several containers are named "Work"; no tab was opened',
      { matches: ['firefox-container-2', 'firefox-container-7'] }));
  assert.equal(named.creates.length, 1);
  // The lookup itself can fail, as with containers switched off.
  const disabled = harness({ identities, queryError: 'Contextual identities are currently disabled' });
  assert.deepEqual(await disabled.send({ action: 'newTab', url, container: 'Personal' }),
    unavailable('Personal', 'rejected', 'Could not open a tab in container "Personal": ' +
      'Contextual identities are currently disabled; no tab was opened'));
  assert.deepEqual(disabled.creates, []);

  // The browser refuses a cookieStoreId it does not have; the tab is not opened elsewhere instead.
  const missing = harness({ createError: 'No cookie store exists with ID firefox-container-99' });
  assert.deepEqual(await missing.send({ action: 'newTab', url, container: 'firefox-container-99' }),
    unavailable('firefox-container-99', 'rejected', 'Could not open a tab in container "firefox-container-99": ' +
      'No cookie store exists with ID firefox-container-99; no tab was opened'));
  assert.equal(missing.creates.length, 1, 'tabs.create is not repeated without the container');
  assert.deepEqual((await missing.send({ action: 'getTabs' })).tabs, []);
  for (const message of ['Contextual identities are currently disabled',
    'Cannot create a container tab in a private window']) {
    const rejected = harness({ createError: message });
    const result = await rejected.send({ action: 'newTab', url, container: 'firefox-container-1' });
    assert.equal(result.code, 'container_unavailable');
    assert.equal(result.state.reason, 'rejected');
    assert.match(result.error, new RegExp(message));
    assert.equal(rejected.creates.length, 1, 'no retry in the default store');
    assert.deepEqual((await rejected.send({ action: 'getTabs' })).tabs, []);
  }

  console.log('firefox containers: ok');
  finished = true;
}

// A pending command must not pass merely because the mocked timers leave no event-loop work.
let finished = false;
process.on('exit', () => { if (!finished) process.exitCode = 1; });
main().catch(error => { console.error(error); process.exitCode = 1; });
