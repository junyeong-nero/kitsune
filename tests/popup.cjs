// Run with: node tests/popup.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const flush = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor(tag = 'div') { Object.assign(this, { tag, children: [], listeners: {}, textContent: '', hidden: false }); }
  append(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
}

function popup() {
  const elements = new Map();
  const messages = [];
  const responses = {};
  const state = { connected: true, calls: 0, lastActivity: null, requests: [], tabs: [], count: 0, toolApprovals: [] };
  let poll;
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  const context = vm.createContext({
    document: { getElementById: get, createElement: tag => new Element(tag) },
    browser: {
      storage: { local: { get: async () => ({}), set: async () => {} } },
      runtime: { sendMessage: async message => {
        messages.push(JSON.parse(JSON.stringify(message)));
        if (responses[message.action]) return responses[message.action](message);
        if (message.action === 'popupStatus') return state;
        if (message.action === 'handoffs') return { requests: state.requests, tabs: state.tabs };
        if (message.action === 'toolApprovals') return state.toolApprovals;
        if (message.action === 'toolApprovalDecision') { state.toolApprovals = []; return { ok: true }; }
        if (message.action === 'handoffDecision') state.requests = [];
        if (message.action === 'takeBackHandoff') state.tabs = [];
        if (message.action === 'closeAgentTabs') { const closed = state.count; state.count = 0; return { closed }; }
        return { count: state.count };
      } },
    },
    Date: { now: () => 100000 },
    setInterval: fn => { poll = fn; },
  });
  vm.runInContext(fs.readFileSync('extension/popup/popup.js', 'utf8'), context);
  return { get, state, messages, responses, poll: async () => { poll(); await flush(); } };
}

async function testUsageAndCleanup() {
  const h = popup();
  await flush();
  assert.equal(h.get('connection').textContent, 'Connected');
  assert.equal(h.get('connection').className, 'connection connected');
  assert.equal(h.get('calls').textContent, '0');
  assert.equal(h.get('last-active').textContent, '—');
  assert.equal(h.get('close-all').disabled, true);
  h.state.calls = 24;
  h.state.lastActivity = 88000;
  h.state.count = 3;
  await h.poll();
  assert.equal(h.get('calls').textContent, '24');
  assert.equal(h.get('last-active').textContent, '12s ago');
  assert.equal(h.get('close-all').disabled, false);
  await h.get('close-all').listeners.click();
  assert.match(h.get('result').textContent, /Closed 3 tabs/);
  assert.equal(h.get('count').textContent, '0');
  assert.equal(h.get('close-all').disabled, true);
  h.state.calls = null;
  await h.poll();
  assert.equal(h.get('calls').textContent, '—');
  assert.match(h.get('usage-note').textContent, /Update the MCP server/);
  h.state.connected = false;
  await h.poll();
  assert.equal(h.get('connection').textContent, 'Disconnected');
  assert.equal(h.get('connection').className, 'connection disconnected');
  assert.equal(h.get('last-active').textContent, '—');
  assert.match(h.get('usage-note').textContent, /Waiting/);
  h.state.connected = true;
  await h.poll();
  assert.equal(h.get('connection').className, 'connection connected');
}

async function testPendingCleanupStaysDisabledAndRecovers() {
  const h = popup();
  h.state.count = 3;
  await h.poll();
  let reject;
  h.responses.closeAgentTabs = () => new Promise((_, fail) => { reject = fail; });
  const closing = h.get('close-all').listeners.click();
  await h.poll();
  assert.equal(h.get('close-all').disabled, true, 'polling cannot re-enable a pending action');
  reject(new Error('unavailable'));
  await closing;
  assert.equal(h.get('close-all').disabled, false);
  assert.match(h.get('result').textContent, /Try again/);
}

async function testHandoffActionsAndSafeLabels() {
  for (const decision of ['approve', 'decline', 'take back']) {
    const h = popup();
    const record = { id: 'r1', tabId: 7, clientName: '<img onerror=alert(1)>',
      title: '<script>private title</script>', url: 'https://example.test/?q=<img>', reason: '<b>literal text</b>' };
    h.state[decision === 'take back' ? 'tabs' : 'requests'] = [record];
    await h.poll();
    const card = h.get('handoffs').children[0];
    assert.deepEqual(card.children.slice(1, 5).map(el => el.textContent),
      [record.clientName, record.title, record.url, record.reason]);
    assert.ok(card.children.every(el => !('innerHTML' in el) && !('href' in el)));
    await h.poll();
    assert.equal(h.get('handoffs').children[0], card, 'unchanged polling preserves keyboard focus');
    const text = { approve: 'Approve handoff', decline: 'Decline', 'take back': 'Take back' }[decision];
    await card.children.at(-1).children.find(button => button.textContent === text).listeners.click();
    assert.deepEqual(h.messages.find(msg => msg.action === (decision === 'take back' ? 'takeBackHandoff' : 'handoffDecision')),
      decision === 'take back' ? { action: 'takeBackHandoff', tabId: 7 } : { action: 'handoffDecision', id: 'r1', decision });
    assert.equal(h.get('handoffs').children.length, 0);
  }
}

async function testToolApprovalCards() {
  // #525: a tool the user requires approval for asks here; agent-supplied text stays text.
  for (const [label, decision] of [['Allow once', 'once'], ['Allow for this session', 'session'], ['Decline', 'decline']]) {
    const h = popup();
    const record = { id: 'a1', tool: 'browser_cookies', clientName: '<img onerror=alert(1)>', summary: '{"op":"get"}' };
    h.state.toolApprovals = [record];
    await h.poll();
    const card = h.get('tool-approvals').children[0];
    assert.deepEqual(card.children.slice(1, 4).map(el => el.textContent), [record.clientName, 'browser_cookies', '{"op":"get"}']);
    assert.ok(card.children.every(el => !('innerHTML' in el)));
    await card.children.at(-1).children.find(button => button.textContent === label).listeners.click();
    assert.deepEqual(h.messages.find(msg => msg.action === 'toolApprovalDecision'),
      { action: 'toolApprovalDecision', id: 'a1', decision });
    assert.equal(h.get('tool-approvals').children.length, 0);
  }
}

async function testBrowserTheme() {
  let theme = { colors: { popup: '#12221a', popup_text: '#e0eee5' }, properties: { color_scheme: 'dark' } };
  let updated;
  const style = { setProperty(name, value) { this[name] = value; },
    removeProperty(name) { delete this[name]; if (name === 'color-scheme') delete this.colorScheme; } };
  const calls = [];
  const context = vm.createContext({
    document: { documentElement: { style } },
    CSS: { supports: () => true },
    browser: {
      windows: { getCurrent: async () => ({ id: 7 }) },
      theme: {
        getCurrent: async id => { calls.push(id); return theme; },
        onUpdated: { addListener: fn => { updated = fn; } },
      },
    },
  });
  vm.runInContext(fs.readFileSync('extension/popup/theme.js', 'utf8'), context);
  await flush();
  assert.equal(style['--surface'], '#12221a');
  assert.equal(style['--on-surface'], '#e0eee5');
  assert.equal(style.colorScheme, 'dark');
  updated({ windowId: 8 });
  await flush();
  assert.deepEqual(calls, [7]);
  theme = { colors: { toolbar: [250, 251, 249], toolbar_text: '#202722' } };
  updated({ windowId: 7 });
  await flush();
  assert.equal(style['--surface'], 'rgb(250,251,249)');
  assert.equal(style.colorScheme, undefined);
  theme = {};
  updated({});
  await flush();
  assert.equal(style['--surface'], undefined, 'switching back to system clears custom colors');
  assert.equal(style['--on-surface'], undefined);
  theme = { colors: { popup: '#000' } };
  updated({});
  await flush();
  assert.equal(style['--surface'], undefined, 'partial palettes use readable fallback colors');
}

// Firefox sizes a toolbar popup from its content, so a viewport-relative width feeds back into that sizing (#440).
function testFixedWidth() {
  const css = fs.readFileSync('extension/popup/popup.css', 'utf8');
  assert.match(css, /^body \{[^}]*\bwidth: 360px;/m, 'the popup body has a fixed width');
  assert.doesNotMatch(css, /\d(vw|vh|vmin|vmax)\b/, 'popup sizes do not depend on the popup viewport');
}

(async () => {
  testFixedWidth();
  await testUsageAndCleanup();
  await testPendingCleanupStaysDisabledAndRecovers();
  await testHandoffActionsAndSafeLabels();
  await testToolApprovalCards();
  await testBrowserTheme();
  console.log('popup: ok');
})().catch(error => { console.error(error); process.exit(1); });
