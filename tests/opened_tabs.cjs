// Run with: node tests/opened_tabs.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function event() {
  const listeners = new Set();
  return {
    listeners,
    addListener: fn => listeners.add(fn),
    removeListener: fn => listeners.delete(fn),
    emit: (...args) => [...listeners].forEach(fn => fn(...args)),
  };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

// onClick(h, msg) runs when a click reaches the page, before the click replies.
function harness(onClick = () => {}) {
  let now = 0, nextId = 0;
  const timers = new Map();
  const schedule = (fn, delay, interval = false) => {
    const id = ++nextId;
    timers.set(id, { fn, at: now + delay, interval, delay });
    return id;
  };
  const onCreated = event(), onUpdated = event(), onRemoved = event();
  const tabs = { 1: { id: 1, url: 'https://example.com/ok', status: 'complete', active: true,
    windowId: 3, cookieStoreId: 'firefox-container-1' } };
  const listenersAtStart = { updated: onUpdated.listeners.size, removed: onRemoved.listeners.size };
  const h = {
    tabs,
    created: [],
    send: command => context.handleCommand({ includeSnapshot: false, ...command })
      .then(result => JSON.parse(JSON.stringify(result))),
    // Gecko creates a link's tab on about:blank and commits its URL afterwards.
    open: (id, openerTabId, url = 'about:blank') => {
      tabs[id] = { id, url, status: 'loading', active: false, openerTabId };
      onCreated.emit({ ...tabs[id] });
    },
    commit: (id, url) => {
      tabs[id] = { ...tabs[id], url };
      onUpdated.emit(id, { url }, { ...tabs[id] });
    },
    close: id => { delete tabs[id]; onRemoved.emit(id); },
    async tick(ms) {
      const end = now + ms;
      await flush();
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.at <= end)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        const [id, timer] = next;
        now = timer.at;
        if (timer.interval) timer.at += timer.delay;
        else timers.delete(id);
        timer.fn();
        await flush();
      }
      now = end;
      await flush();
    },
    assertClean() {
      assert.equal(onUpdated.listeners.size, listenersAtStart.updated, 'onUpdated leaked');
      assert.equal(onRemoved.listeners.size, listenersAtStart.removed, 'onRemoved leaked');
      assert.equal(timers.size, 0, 'timer leaked');
    },
  };
  const missing = id => new Error(`Invalid tab ID: ${id}`);
  const browser = {
    tabs: {
      onCreated, onUpdated, onRemoved,
      query: async () => Object.values(tabs).filter(tab => tab.active).map(tab => ({ ...tab })),
      get: async id => { if (!tabs[id]) throw missing(id); return { ...tabs[id] }; },
      // tabs.create with a missing opener fails like Firefox.
      create: async props => {
        if (!tabs[props.openerTabId]) throw missing(props.openerTabId);
        const id = 20 + h.created.length;
        h.created.push({ ...props });
        h.open(id, props.openerTabId);
        return { ...tabs[id] };
      },
      sendMessage: async (id, msg) => {
        if (msg.action === 'ping') return { ok: true, version: Infinity };
        if (msg.action === 'click') return (await onClick(h, msg)) || { ok: true };
        if (msg.action === 'openedPopup') return h.openedPopup ? { opened_popup: h.openedPopup } : {};
        return { ok: true };
      },
      executeScript: async () => {},
    },
    // The user last focused another browser window, so no tab here is held for being viewed (#326).
    windows: { getLastFocused: async () => ({ id: 99, focused: true }) },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
  };
  const context = vm.createContext({
    browser, URL, console: { log() {}, error() {} },
    WebSocket: class { close() {} },
    Date: class extends Date { static now() { return now; } },
    setTimeout: (fn, delay) => schedule(fn, delay),
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => schedule(fn, delay, true),
    clearInterval: id => timers.delete(id),
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('extension/background/history.js', 'utf8'), context);
  listenersAtStart.updated = onUpdated.listeners.size;
  listenersAtStart.removed = onRemoved.listeners.size;
  return h;
}

// Tracks whether a command has resolved, so a hang fails an assertion instead of exiting silently.
function start(h, command) {
  const pending = { settled: false };
  pending.result = h.send(command).then(result => { pending.settled = true; return result; });
  return pending;
}

async function main() {
  // A target=_blank click reports the new tab once its URL leaves about:blank.
  const blank = harness((h, msg) => { if (msg.selector === '#new') h.open(4, 1); });
  const clicking = start(blank, { action: 'click', tabId: 1, selector: '#new' });
  await blank.tick(50);
  assert.equal(clicking.settled, false, 'waits briefly for the opened tab URL');
  blank.commit(4, 'https://example.com/ok2');
  await blank.tick(0);
  assert.deepEqual(await clicking.result, {
    ok: true, opened_tabs: [{ tabId: 4, url: 'https://example.com/ok2' }],
  });
  blank.assertClean();

  // A no-op click is unchanged; the earlier tab is not reported again by a later action.
  assert.deepEqual(await blank.send({ action: 'click', tabId: 1, selector: '#noop' }), { ok: true });
  assert.deepEqual(await blank.send({ action: 'getOpenedTabs', tabId: 1 }), { opened_tabs: [] });

  // Without tabId the active tab is tracked; tabs opened by other tabs or with no opener are ignored.
  const many = harness(h => {
    h.open(5, 1, 'https://example.com/a');
    h.open(6, 2, 'https://example.com/b');
    h.open(7, undefined, 'https://example.com/c');
    h.open(8, 1, 'https://example.com/d');
  });
  assert.deepEqual(await many.send({ action: 'click', selector: '#many' }), {
    ok: true, opened_tabs: [{ tabId: 5, url: 'https://example.com/a' }, { tabId: 8, url: 'https://example.com/d' }],
  });
  many.assertClean();

  // A tab opened after the action replied is still found by getOpenedTabs; closed tabs drop out.
  many.open(9, 1, 'https://example.com/late');
  many.close(5);
  assert.deepEqual(await many.send({ action: 'getOpenedTabs', tabId: 1 }), {
    opened_tabs: [{ tabId: 8, url: 'https://example.com/d' }, { tabId: 9, url: 'https://example.com/late' }],
  });
  assert.deepEqual(await many.send({ action: 'getOpenedTabs', tabId: 2 }), { opened_tabs: [] });
  // Closing the opener forgets its opened tabs.
  many.close(1);
  assert.deepEqual(await many.send({ action: 'getOpenedTabs', tabId: 1 }), { opened_tabs: [] });

  // A tab that stays on about:blank is reported as such after the bounded wait.
  const stuck = harness(h => h.open(4, 1));
  const waiting = start(stuck, { action: 'click', tabId: 1, selector: '#popup' });
  await stuck.tick(999);
  assert.equal(waiting.settled, false);
  await stuck.tick(1);
  assert.deepEqual(await waiting.result, { ok: true, opened_tabs: [{ tabId: 4, url: 'about:blank' }] });
  stuck.assertClean();

  // A tab that closes while its URL is awaited is not reported.
  const closing = harness(h => h.open(4, 1));
  const closingClick = start(closing, { action: 'click', tabId: 1, selector: '#popup' });
  await closing.tick(10);
  closing.close(4);
  await closing.tick(0);
  assert.deepEqual(await closingClick.result, { ok: true });
  closing.assertClean();

  // #94: the popup blocker dropped a target=_blank link's tab, so the link's URL opens from
  // the clicked tab after the bounded wait, in its window and container.
  const link = { url: 'https://example.com/ok2', opener: false };
  const blocked = harness(() => ({ ok: true, targetBlankLink: link }));
  const blockedClick = start(blocked, { action: 'click', tabId: 1, selector: '#new' });
  await blocked.tick(450);
  assert.deepEqual(blocked.created, [], 'waits for the browser to open the tab first');
  await blocked.tick(50);
  assert.deepEqual(blocked.created, [{
    url: 'https://example.com/ok2', openerTabId: 1, windowId: 3, cookieStoreId: 'firefox-container-1', active: false,
  }]);
  blocked.commit(20, 'https://example.com/ok2');
  assert.deepEqual(await blockedClick.result, {
    ok: true, opened_tabs: [{ tabId: 20, url: 'https://example.com/ok2', via: 'link_fallback' }],
  });
  blocked.assertClean();
  assert.deepEqual(await blocked.send({ action: 'getOpenedTabs', tabId: 1 }), {
    opened_tabs: [{ tabId: 20, url: 'https://example.com/ok2', via: 'link_fallback' }],
  });

  // A link whose tab the browser opened is not opened again.
  const allowed = harness(h => { h.open(4, 1, 'https://example.com/ok2'); return { ok: true, targetBlankLink: link }; });
  assert.deepEqual(await allowed.send({ action: 'click', tabId: 1, selector: '#new' }), {
    ok: true, opened_tabs: [{ tabId: 4, url: 'https://example.com/ok2' }],
  });
  assert.deepEqual(allowed.created, []);
  allowed.assertClean();

  // A tab the browser opens within the wait is not duplicated either.
  const late = harness(() => ({ ok: true, targetBlankLink: link }));
  const lateClick = start(late, { action: 'click', tabId: 1, selector: '#new' });
  await late.tick(200);
  late.open(4, 1, 'https://example.com/ok2');
  await late.tick(500);
  assert.deepEqual(await lateClick.result, { ok: true, opened_tabs: [{ tabId: 4, url: 'https://example.com/ok2' }] });
  assert.deepEqual(late.created, []);
  late.assertClean();

  // A rel=opener link needs window.opener, so it only gets a popup_blocked warning.
  const opener = harness(() => ({ ok: true, targetBlankLink: { ...link, opener: true } }));
  const openerClick = start(opener, { action: 'click', tabId: 1, selector: '#opener' });
  await opener.tick(500);
  const warned = await openerClick.result;
  assert.deepEqual(opener.created, []);
  assert.equal(warned.warning.code, 'popup_blocked');
  assert.deepEqual(warned.warning.state, { url: 'https://example.com/ok2' });
  assert.match(warned.warning.hint, /popup blocker/);
  assert.equal('targetBlankLink' in warned, false, 'the content-script field stays internal');
  opener.assertClean();

  // A failed tabs.create (the clicked tab closed meanwhile) also warns; an earlier warning stays.
  const closed = harness(h => { delete h.tabs[1]; return { ok: true, targetBlankLink: link }; });
  const closedClick = start(closed, { action: 'click', tabId: 1, selector: '#new' });
  await closed.tick(500);
  assert.equal((await closedClick.result).warning.code, 'popup_blocked');
  const hidden = harness(() => ({ ok: true, warning: { code: 'hidden_or_covered' }, targetBlankLink: { ...link, opener: true } }));
  const hiddenClick = start(hidden, { action: 'click', tabId: 1, selector: '#hidden' });
  await hidden.tick(500);
  assert.deepEqual(await hiddenClick.result, { ok: true, warning: { code: 'hidden_or_covered' } });

  // #223: a middle or Ctrl/Cmd click's link opens in a background tab after the bounded wait,
  // as a popup-blocked link does, whatever its target.
  const middle = { url: 'https://example.com/order/2', prevented: false };
  const silent = harness(() => ({ ok: true, button: 'middle', newTabLink: middle }));
  const silentClick = start(silent, { action: 'click', tabId: 1, selector: '#order', button: 'middle' });
  await silent.tick(450);
  assert.deepEqual(silent.created, [], 'waits for a tab the browser or page opens first');
  await silent.tick(50);
  assert.deepEqual(silent.created, [{
    url: middle.url, openerTabId: 1, windowId: 3, cookieStoreId: 'firefox-container-1', active: false,
  }]);
  silent.commit(20, middle.url);
  assert.deepEqual(await silentClick.result, {
    ok: true, button: 'middle', opened_tabs: [{ tabId: 20, url: middle.url, via: 'link_fallback' }],
  });
  silent.assertClean();

  // A tab the page or browser opened for the click is the click's tab, not opened again.
  const native = harness(h => { h.open(4, 1, middle.url); return { ok: true, newTabLink: middle }; });
  assert.deepEqual(await native.send({ action: 'click', tabId: 1, selector: '#order', modifiers: ['ControlOrMeta'] }),
    { ok: true, opened_tabs: [{ tabId: 4, url: middle.url }] });
  assert.deepEqual(native.created, []);
  native.assertClean();

  // A click the page cancelled is the page's: its own tab is reported, and none is opened for it.
  const cancelledLink = { ...middle, prevented: true };
  const scripted = harness(h => { h.open(4, 1, 'https://example.com/popup'); return { ok: true, newTabLink: cancelledLink }; });
  assert.deepEqual(await scripted.send({ action: 'click', tabId: 1, selector: '#order', modifiers: ['ControlOrMeta'] }),
    { ok: true, opened_tabs: [{ tabId: 4, url: 'https://example.com/popup' }] });
  assert.deepEqual(scripted.created, []);

  // When no tab opened, the result says so instead of a bare ok.
  const prevented = harness(() => ({ ok: true, newTabLink: cancelledLink }));
  const none = await prevented.send({ action: 'click', tabId: 1, selector: '#order', modifiers: ['ControlOrMeta'] });
  assert.deepEqual(prevented.created, []);
  assert.deepEqual(none.opened_tabs, []);
  assert.equal(none.warning.code, 'no_tab_opened');
  assert.deepEqual(none.warning.state, { url: middle.url, reason: 'page_cancelled' });
  assert.match(none.warning.hint, /cancelled/);
  assert.match(none.warning.hint, /browser_tabs action=new/);
  assert.equal('newTabLink' in none, false, 'the content-script field stays internal');
  prevented.assertClean();

  // A failed fallback (the clicked tab closed meanwhile) keeps its popup_blocked warning.
  const gone = harness(h => { delete h.tabs[1]; return { ok: true, newTabLink: middle }; });
  const goneClick = start(gone, { action: 'click', tabId: 1, selector: '#order', button: 'middle' });
  await gone.tick(500);
  const goneResult = await goneClick.result;
  assert.deepEqual(goneResult.opened_tabs, []);
  assert.equal(goneResult.warning.code, 'popup_blocked');

  // #304: a page script's window.open that the popup blocker refused during the click warns,
  // naming the URL it tried; nothing is opened in its place.
  const oauth = 'https://accounts.example/o/oauth2/auth?client_id=1';
  const refused = harness(() => ({ ok: true, scriptPopup: { url: oauth } }));
  const refusedResult = await refused.send({ action: 'click', tabId: 1, selector: '#google' });
  assert.deepEqual(refused.created, []);
  assert.equal(refusedResult.warning.code, 'popup_blocked');
  assert.deepEqual(refusedResult.warning.state, { url: oauth, urls: [oauth], reason: 'script_popup' });
  assert.match(refusedResult.warning.hint, /user activation/);
  assert.match(refusedResult.warning.hint, /If this click should open that popup .*ask the user to click it/);
  assert.match(refusedResult.warning.hint, /if not \(an ad, for example\), ignore this warning/);
  assert.match(refusedResult.warning.hint, /browser_trusted_click/);
  assert.equal('scriptPopup' in refusedResult, false, 'the content-script field stays internal');
  refused.assertClean();
  // The URL comes from a page script, so it is redacted like snapshot URLs.
  const secret = harness(() => ({ ok: true,
    scriptPopup: { url: 'https://accounts.example/auth?client_id=1&nonce=abc123&state=s1#access_token=ya29.a0Secret' } }));
  assert.equal((await secret.send({ action: 'click', tabId: 1, selector: '#google' })).warning.state.url,
    'https://accounts.example/auth?client_id=1&nonce=[redacted]&state=s1#access_token=[redacted]');
  const unnamed = harness(() => ({ ok: true, scriptPopup: {} }));
  assert.deepEqual((await unnamed.send({ action: 'click', tabId: 1, selector: '#google' })).warning.state,
    { urls: [], reason: 'script_popup' });
  const tokenUrl = 'https://example.com/auth?access_token=secret&nonce=private';
  const redactedUrl = 'https://example.com/auth?access_token=[redacted]&nonce=[redacted]';
  const multiple = harness(() => ({ ok: true, scriptPopup: { url: oauth, urls: [oauth, tokenUrl] } }));
  assert.deepEqual((await multiple.send({ action: 'click', tabId: 1, selector: '#google' })).warning.state.urls,
    [oauth, redactedUrl]);
  const linkSecret = harness(() => ({ ok: true, targetBlankLink: { url: tokenUrl, opener: true } }));
  const secretClick = start(linkSecret, { action: 'click', tabId: 1, selector: '#link' });
  await linkSecret.tick(500);
  const secretWarning = (await secretClick.result).warning;
  assert.equal(secretWarning.state.url, redactedUrl);
  assert.match(secretWarning.hint, /original page control/);
  assert.match(secretWarning.hint, /must not be used as a navigation URL/);
  assert.doesNotMatch(secretWarning.hint, /Open state.url/);
  const linkFallback = harness(() => ({ ok: true, targetBlankLink: { url: tokenUrl, opener: false } }));
  const fallbackClick = start(linkFallback, { action: 'click', tabId: 1, selector: '#link' });
  await linkFallback.tick(500);
  linkFallback.commit(20, tokenUrl);
  await linkFallback.tick(0);
  assert.equal(fallbackClick.settled, true);
  await fallbackClick.result;
  assert.equal(linkFallback.created[0].url, tokenUrl, 'fallback navigates to the original URL');

  // A tab that did open is reported instead, and an earlier warning stays.
  const popupOpened = harness(h => { h.open(4, 1, oauth); return { ok: true, scriptPopup: { url: oauth } }; });
  assert.deepEqual(await popupOpened.send({ action: 'click', tabId: 1, selector: '#google' }),
    { ok: true, opened_tabs: [{ tabId: 4, url: oauth }] });
  const covered = harness(() => ({ ok: true, warning: { code: 'hidden_or_covered' }, scriptPopup: { url: oauth } }));
  assert.deepEqual(await covered.send({ action: 'click', tabId: 1, selector: '#google' }),
    { ok: true, warning: { code: 'hidden_or_covered' } });
  // A click can open an in-page popup (#329) and have its window.open refused: both are reported.
  const both = harness(() => ({ ok: true, popupWatch: true, scriptPopup: { url: oauth } }));
  both.openedPopup = { role: 'dialog', name: 'Sign in', rendered: true, items: [] };
  const bothResult = await both.send({ action: 'click', tabId: 1, selector: '#google' });
  assert.deepEqual(bothResult.opened_popup, both.openedPopup);
  assert.deepEqual(bothResult.warning.state, { url: oauth, urls: [oauth], reason: 'script_popup' });
  assert.equal('popupWatch' in bothResult || 'scriptPopup' in bothResult, false);

  console.log('Opened tab checks passed');
  finished = true;
}

// A command that never settles empties the event loop; fail instead of exiting 0.
let finished = false;
process.on('exit', () => { if (!finished) process.exitCode = 1; });
main().catch(error => { console.error(error); process.exitCode = 1; });
