// #525: per-tool approvals in the background page: the popup asks while the bridge waits, or the MCP
// server holds a token and its client's prompt asks too; the first answer counts.
// Run with: node tests/tool_approvals.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { harness } = require('./shared_browser.cjs');

async function testPopupAnswersARequestTheBridgeWaitsFor() {
  const h = harness({ userTabsWritable: false });
  const ask = (extra = {}) => h.send({ action: 'toolApproval', tool: 'browser_cookies', clientName: '<Agent>',
    clientId: 'c1', summary: '{"op":"get"}', ...extra });
  const popup = () => h.popup({ action: 'toolApprovals' });
  for (const [decision, expected] of [['once', { ok: true, tool: 'browser_cookies', scope: 'once' }],
    ['session', { ok: true, tool: 'browser_cookies', scope: 'session' }]]) {
    const pending = ask();
    await h.tick(0);
    const [card] = await popup();
    assert.deepEqual(card, { id: card.id, tool: 'browser_cookies', clientName: '<Agent>', summary: '{"op":"get"}' });
    await h.popup({ action: 'toolApprovalDecision', id: card.id, decision });
    assert.deepEqual(await pending, expected);
    assert.deepEqual(await popup(), []);
  }
  let pending = ask();
  await h.tick(0);
  await h.popup({ action: 'toolApprovalDecision', id: (await popup())[0].id, decision: 'decline' });
  let refused = await pending;
  assert.equal(refused.code, 'needs_user');
  assert.deepEqual(refused.state, { reason: 'approval', detail: 'tool_approval', tool: 'browser_cookies',
    decision: 'declined' });
  // Unanswered, the request expires; a container step waits less.
  pending = ask({ timeoutMs: 30000 });
  await h.tick(30000);
  assert.equal((await pending).state.decision, 'cancelled');
  assert.equal((await ask({ tool: 'not a tool' })).code, 'invalid_argument');
}

async function testTheClientsPromptAndThePopupAskTogether() {
  const h = harness({ userTabsWritable: false });
  const send = extra => h.send({ action: 'toolApproval', tool: 'browser_storage', clientId: 'c1', ...extra });
  const held = await send({ together: true });
  assert.equal(held.code, 'needs_user');
  assert.equal(held.state.together, true);
  const token = held.state.confirmToken;
  assert.deepEqual(await h.popup({ action: 'toolApprovals' }), [], 'the popup asks once the server waits');
  // Another client or tool cannot use the token.
  assert.equal((await send({ confirmToken: token, clientId: 'c2', decision: 'once' })).code, 'needs_user');
  assert.equal((await h.send({ action: 'toolApproval', tool: 'browser_cookies', clientId: 'c1', confirmToken: token,
    decision: 'once' })).code, 'needs_user');
  // The popup answers first.
  const waiting = send({ confirmToken: token, wait: true });
  await h.tick(0);
  const [card] = await h.popup({ action: 'toolApprovals' });
  assert.equal(card.id, token);
  await h.popup({ action: 'toolApprovalDecision', id: card.id, decision: 'session' });
  assert.deepEqual(await waiting, { ok: true, tool: 'browser_storage', scope: 'session' });
  // The client's prompt answers first: the popup's wait returns that answer too.
  const second = (await send({ together: true })).state.confirmToken;
  const wait2 = send({ confirmToken: second, wait: true });
  await h.tick(0);
  const answered = await send({ confirmToken: second, decision: 'declined' });
  assert.equal(answered.state.decision, 'declined');
  assert.equal((await wait2).state.decision, 'declined');
  assert.deepEqual(await h.popup({ action: 'toolApprovals' }), []);
}

function testOptionsPageSavesTheCheckedTools() {
  const elements = {};
  const element = id => (elements[id] ??= { id, textContent: '', children: [], listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; }, replaceChildren() { this.children = []; },
    append(...items) { this.children.push(...items); } });
  let stored = {};
  const event = () => ({ addListener() {} });
  const browser = {
    storage: { local: { get: async () => ({ ...stored }), set: async value => { stored = { ...stored, ...value }; } } },
    permissions: { contains: async () => false, onAdded: event(), onRemoved: event() },
  };
  const create = tag => ({ tag, textContent: '', children: [], listeners: {},
    append(...items) { this.children.push(...items); }, addEventListener(type, fn) { this.listeners[type] = fn; } });
  const context = vm.createContext({ browser, document: { getElementById: element, createElement: create } });
  vm.runInContext(fs.readFileSync('extension/background/tool-approvals.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('extension/options/options.js', 'utf8'), context);
  // Arrays the page made belong to its realm: copy them for deepEqual.
  return { context, elements, stored: () => JSON.parse(JSON.stringify(stored)) };
}

async function testOptionsPage() {
  const page = testOptionsPageSavesTheCheckedTools();
  const flush = () => new Promise(resolve => setImmediate(resolve));
  await flush();
  const boxes = () => page.elements['approval-tools'].children.flatMap(fieldset => fieldset.children.slice(1))
    .map(label => ({ name: label.children[1].trim(), box: label.children[0] }));
  const checked = () => boxes().filter(({ box }) => box.checked).map(({ name }) => name).sort();
  const defaults = [...vm.runInContext('DEFAULT_APPROVAL_TOOLS', page.context)].sort();
  assert.deepEqual(checked(), defaults, 'nothing stored: the defaults are checked');
  const python = boxes().find(({ name }) => name === 'browser_run_python').box;
  python.checked = true;
  await python.listeners.change();
  await flush();
  assert.deepEqual(page.stored().approvalTools, [...defaults, 'browser_run_python'].sort());
  await page.elements['approval-none'].listeners.click();
  await flush();
  assert.deepEqual(page.stored().approvalTools, []);
  assert.deepEqual(checked(), []);
  await page.elements['approval-defaults'].listeners.click();
  await flush();
  assert.deepEqual(page.stored().approvalTools.sort(), defaults);
}

(async () => {
  await testPopupAnswersARequestTheBridgeWaitsFor();
  await testTheClientsPromptAndThePopupAskTogether();
  await testOptionsPage();
  console.log('tool_approvals: ok');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
