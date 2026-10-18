// Run with: node tests/password_redaction.cjs (no browser or npm dependencies).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const DUMMY = 'KITSUNE_DUMMY_ONLY_123!';

class Element {
  constructor(tagName, textContent = '', attrs = {}, children = []) {
    Object.assign(this, { tagName, textContent, attrs, children, nodeType: 1,
      childNodes: [{ nodeType: 3, textContent }], isConnected: true });
    children.forEach(child => { child.parentElement = this; });
  }
  getAttribute(key) { return this.attrs[key] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  closest() { return null; }
  focus() { this.focused = true; }
  dispatchEvent() { return true; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}

(async () => {
  const email = Object.assign(new Element('INPUT', '', { 'aria-label': 'Email' }),
    { id: 'email', name: 'email', type: 'email', value: 'a@b' });
  const password = Object.assign(new Element('INPUT'),
    { id: 'password', name: 'password', type: 'password', value: '' });
  const confirm = Object.assign(new Element('INPUT', '', { 'aria-label': 'Confirm' }),
    { id: 'confirm', name: 'confirm', type: 'password', value: '' });
  const button = new Element('BUTTON', 'Log in');
  const inputs = [email, password, confirm];
  const body = new Element('BODY', '', {}, [new Element('FORM', '', {}, [...inputs, button])]);
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, CSS: { escape: x => x }, setTimeout,
    Event: class { constructor(type, options) { Object.assign(this, { type }, options); } },
    HTMLInputElement: class {}, HTMLTextAreaElement: class {},
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible' }) },
    document: { body,
      getElementById: () => null,
      querySelector: selector => {
        if (selector === 'label[for="password"]') return { textContent: 'Password' };
        return { '#password': password, '#confirm': confirm }[selector] || null;
      },
      querySelectorAll: selector => {
        if (selector === 'input' || selector === 'input, select, textarea') return inputs;
        return selector.startsWith('a, button, input') ? [...inputs, button] : [];
      },
    },
    browser: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null) } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  const send = msg => new Promise(resolve => listener(msg, {}, resolve));

  // Populate the field the same way browser_type(mode="fill") does.
  assert.equal((await send({ action: 'fill', selector: '#password', value: DUMMY })).ok, true);
  assert.equal(password.value, DUMMY);

  const outputs = {};
  const assertNoLeak = (label, result) => {
    outputs[label] = result;
    const text = JSON.stringify(result);
    assert.ok(!text.includes(DUMMY), `${label} leaks the password value`);
    assert.ok(!/KITSUNE/i.test(text), `${label} leaks part of the password value`);
  };

  const snapshot = await send({ action: 'getSnapshot' });
  assertNoLeak('snapshot', snapshot);
  assert.match(snapshot.snapshot, /textbox "Email" \[ref=s\d+e\d+\] \[value="a@b"\]/);
  assert.match(snapshot.snapshot, /textbox "Password" \[ref=s\d+e\d+\]$/m);

  const forms = await send({ action: 'getFormFields' });
  assertNoLeak('forms', forms);
  const [emailField, passwordField, confirmField] = forms.fields;
  assert.equal(emailField.value, 'a@b');
  assert.equal('value' in passwordField, false);
  assert.equal(passwordField.redacted, true);
  assert.equal(passwordField.hasValue, true);
  assert.equal(confirmField.redacted, true);
  assert.equal(confirmField.hasValue, false);
  // Interaction metadata survives redaction.
  for (const [key, expected] of Object.entries({ tag: 'input', type: 'password', name: 'password',
    id: 'password', label: 'Password', selector: '#password' })) {
    assert.equal(passwordField[key], expected, `forms keeps ${key}`);
  }
  assert.match(passwordField.ref, /^s\d+e\d+$/);
  assert.equal((await send({ action: 'focusElement', selector: passwordField.ref })).ok, true);

  const dom = await send({ action: 'getAccessibilityTree', depth: 6 });
  assertNoLeak('dom', dom);
  const domNodes = dom.tree.children[0].children;
  const [emailNode, passwordNode, confirmNode] = domNodes;
  assert.equal(emailNode.value, 'a@b');
  assert.equal('value' in passwordNode, false);
  assert.deepEqual([passwordNode.redacted, passwordNode.hasValue], [true, true]);
  assert.deepEqual([confirmNode.redacted, confirmNode.hasValue], [true, false]);
  assert.deepEqual([passwordNode.type, passwordNode.name, passwordNode.id, passwordNode.role],
    ['password', 'password', 'password', 'textbox']);
  assert.equal((await send({ action: 'focusElement', selector: passwordNode.ref })).ok, true);

  const query = await send({ action: 'query', selector: 'input' });
  assertNoLeak('query', query);
  assert.equal(query.items[0].value, 'a@b');
  assert.equal('value' in query.items[1], false);
  assert.deepEqual([query.items[1].redacted, query.items[1].hasValue], [true, true]);

  // find must not use the password as a match oracle, but still matches other values.
  const probe = await send({ action: 'find', query: DUMMY.slice(0, 8) });
  assertNoLeak('find', probe);
  const scoreOf = (result, selector) => result.results.find(r => r.selector === selector).score;
  assert.equal(scoreOf(probe, '#password'), scoreOf(probe, '#confirm'), 'filled password scores like an empty one');
  const byValue = await send({ action: 'find', query: 'a@b' });
  assert.equal(byValue.results[0].selector, '#email');
  assert.ok(scoreOf(byValue, '#email') > scoreOf(byValue, '#confirm'));

  assert.deepEqual(Object.keys(outputs), ['snapshot', 'forms', 'dom', 'query', 'find']);
  console.log('Password redaction checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
