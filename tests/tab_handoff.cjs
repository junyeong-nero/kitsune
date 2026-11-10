// #396: browser-first approval, deterministic timers, ephemeral ownership and popup isolation;
// #494: the popup and the client's prompt at once for a client that asks the user itself.
const assert = require('node:assert/strict');
const { harness } = require('./shared_browser.cjs');

async function main() {
  const h = harness({ userTabsWritable: false });
  const a = { ownerId: 'owner', sessionId: 'session-a', clientName: '<Client A>' };
  const b = { ownerId: 'owner', sessionId: 'session-b', clientName: 'Client B' };
  await h.send({ action: 'openHandoffSession', ...a });
  await h.send({ action: 'openHandoffSession', ...b });
  const request = (who = a, tabId = 1, extra = {}) => h.send({ action: 'requestTab', ...who, tabId,
    reason: 'Work on this page', ...extra });
  const write = who => h.send({ action: 'click', ...who, tabId: 1, selector: '#go' });
  // Core browser_tabs select admits its target with this check before it makes the tab current (#399).
  const selectable = who => h.send({ action: 'checkTabWrite', writeAction: 'switchTab', ...who, tabId: 1 });
  const popup = () => h.popup({ action: 'handoffs' });
  const decide = (id, decision = 'approve') => h.popup({ action: 'handoffDecision', id, decision });
  const owned = async who => (await h.send({ action: 'getTabs', ...who })).tabs.find(t => t.id === 1).owned;
  let pending = request();
  await h.tick(0);
  let prompt = (await popup()).requests[0];
  assert.equal(prompt.clientName, '<Client A>');
  assert.equal(prompt.url, h.tabs[1].url);
  assert.equal(prompt.reason, 'Work on this page');
  assert.equal(prompt.sessionId, undefined);
  assert.equal(await h.popup({ action: 'handoffDecision', id: prompt.id, decision: 'approve' },
    { id: 'kitsune-test', tab: { id: 1 }, url: h.tabs[1].url }), undefined);
  assert.equal(await owned(a), false);
  assert.equal((await selectable(a)).code, 'user_tab_read_only');
  await decide(prompt.id);
  assert.equal((await pending).ok, true);
  assert.equal(await owned(a), true);
  assert.equal(await owned(b), false, 'same stable owner does not share a handoff');
  assert.equal((await write(a)).ok, true);
  assert.equal((await write(b)).code, 'user_tab_read_only');
  assert.deepEqual(await selectable(a), { ok: true }, 'a handed-off tab can be selected');
  assert.equal((await selectable(b)).code, 'user_tab_read_only');
  assert.equal((await request(b)).code, 'user_tab_read_only', 'no silent transfer');
  h.activity[1] = { reason: 'input', input: 'keydown' };
  assert.equal((await write(a)).code, 'user_active');
  delete h.activity[1];
  h.load(1, 'https://user.example/after-approval');
  assert.equal(await owned(a), true, 'ownership survives navigation after approval');
  h.tabs[1].status = 'complete';
  const child = h.open(1, { foreground: false });
  assert.equal((await h.send({ action: 'getTabs', ...a })).tabs.find(t => t.id === child).owned, true);
  await h.popup({ action: 'takeBackHandoff', tabId: 1 });
  assert.equal((await write(a)).code, 'user_tab_read_only');
  assert.equal((await selectable(a)).code, 'user_tab_read_only');
  assert.equal((await decide(prompt.id)).code, 'needs_user', 'old popup cannot regrant');
  assert.equal((await request(a, 1, { confirmToken: prompt.id })).code, 'needs_user');
  pending = request();
  await h.tick(0);
  prompt = (await popup()).requests[0];
  await decide(prompt.id, 'decline');
  const declined = await pending;
  assert.equal(declined.state.decision, 'declined');
  assert.match(declined.hint, /own/);
  assert.equal(declined.state.confirmToken, undefined);
  pending = request();
  let answered = false;
  pending.then(() => answered = true);
  await h.tick(29999);
  assert.equal(answered, false);
  await h.tick(1);
  const fallback = await pending;
  assert.equal(fallback.state.approval, 'token');
  assert.equal(fallback.state.expiresInSeconds, 120);
  assert.deepEqual((await popup()).requests, [], 'browser prompt retires before client approval');
  const token = fallback.state.confirmToken;
  assert.equal((await decide(token)).code, 'needs_user');
  assert.equal((await request(b, 1, { confirmToken: token })).code, 'needs_user');
  assert.equal((await request(a, 2, { confirmToken: token })).code, 'needs_user');
  assert.equal((await request(a, 1, { confirmToken: token, reason: 'different' })).code, 'needs_user');
  assert.equal((await request(a, 1, { confirmToken: token })).ok, true);
  assert.equal((await request(a, 1, { confirmToken: token })).code, 'needs_user', 'single use');
  const created = await h.send({ action: 'newTab', ...a });
  await h.send({ action: 'endHandoffSession', ...a });
  assert.equal((await write(a)).code, 'user_tab_read_only');
  const remaining = (await h.send({ action: 'getTabs', ...a })).tabs.filter(t => t.owned).map(t => t.id);
  assert.ok(remaining.includes(created.tabId));
  assert.ok(remaining.includes(child));
  assert.equal((await request()).code, 'needs_user', 'ended session cannot request');
  await h.send({ action: 'openHandoffSession', ...a });
  for (const invalidate of [() => h.load(1, h.tabs[1].url), () => h.send({ action: 'endHandoffSession', ...a }),
    () => h.disconnect()]) {
    await h.send({ action: 'openHandoffSession', ...a });
    h.tabs[1].status = 'complete';
    pending = request();
    await h.tick(0);
    prompt = (await popup()).requests[0];
    await invalidate();
    assert.equal((await pending).code, 'needs_user');
    assert.equal((await decide(prompt.id)).code, 'needs_user');
  }
  await h.send({ action: 'openHandoffSession', ...a });
  const expiring = request(a, 2);
  await h.tick(30000);
  const expiredToken = (await expiring).state.confirmToken;
  await h.tick(120000);
  assert.equal((await request(a, 2, { confirmToken: expiredToken })).code, 'needs_user');
  const closing = request(a, 2);
  await h.tick(0);
  const closingPrompt = (await popup()).requests[0];
  await h.remove(2);
  assert.equal((await closing).code, 'needs_user');
  assert.equal((await decide(closingPrompt.id)).code, 'needs_user');
  // #494: a client that asks the user itself gets the token at once; the popup asks once it waits, and
  // the first answer, in the popup or the client's prompt, settles the wait.
  const together = async () => {
    const held = await request(a, 1, { together: true });
    assert.equal(held.state.together, true);
    assert.equal(held.state.expiresInSeconds, 120);
    assert.deepEqual((await popup()).requests, [], 'the popup asks once the client waits');
    const token = held.state.confirmToken;
    assert.equal((await request(a, 1, { confirmToken: token, wait: true, reason: 'different' })).code, 'needs_user');
    const waiting = request(a, 1, { confirmToken: token, wait: true });
    await h.tick(0);
    assert.deepEqual((await popup()).requests.map(r => r.id), [token]);
    assert.equal((await request(a, 1, { confirmToken: token, wait: true })).code, 'needs_user', 'one wait');
    return { token, waiting };
  };
  let asked = await together();
  await decide(asked.token);
  assert.equal((await asked.waiting).ok, true, 'the popup answered');
  assert.equal((await request(a, 1, { confirmToken: asked.token })).code, 'needs_user', 'the token is spent');
  await h.popup({ action: 'takeBackHandoff', tabId: 1 });
  asked = await together();
  assert.equal((await request(a, 1, { confirmToken: asked.token })).ok, true, 'the client answered');
  assert.equal((await asked.waiting).ok, true);
  assert.deepEqual((await popup()).requests, []);
  await h.popup({ action: 'takeBackHandoff', tabId: 1 });
  for (const decision of ['declined', 'cancelled']) {
    asked = await together();
    assert.equal((await request(a, 1, { confirmToken: asked.token, decision })).state.decision, decision);
    assert.equal((await asked.waiting).state.decision, decision);
    assert.deepEqual((await popup()).requests, [], 'the client declined: the popup stops asking');
    assert.equal((await decide(asked.token)).code, 'needs_user');
  }
  asked = await together();
  await decide(asked.token, 'decline');
  assert.equal((await asked.waiting).state.decision, 'declined');
  assert.equal((await request(a, 1, { confirmToken: asked.token })).code, 'needs_user', 'the client cannot approve then');
  asked = await together();
  await h.tick(120000);
  assert.equal((await asked.waiting).state.decision, 'cancelled', 'expired');
  const unasked = request(a, 1);  // without together: browser first, and its prompt is not waited for
  await h.tick(0);
  assert.equal((await request(a, 1, { confirmToken: (await popup()).requests[0].id, wait: true })).code, 'needs_user');
  await h.tick(30000);
  const late = (await unasked).state.confirmToken;
  assert.equal((await request(a, 1, { confirmToken: late, wait: true })).code, 'needs_user');
  assert.equal(await owned(a), false);
  assert.equal(fallback.call, undefined);
  assert.equal(fallback.resume_token, undefined);
  assert.doesNotMatch(fallback.hint, /browser_resume/);
  assert.deepEqual(h.shown(), [1], 'handoff never changes user view');
  assert.deepEqual(h.activations(), []);
  console.log('tab_handoff: passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
