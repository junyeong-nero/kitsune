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
  getAttribute(key) { return this.attrs[key.toLowerCase()] ?? null; }
  hasAttribute(key) { return key in this.attrs; }
  removeAttribute(key) { delete this.attrs[key]; }
  get attributes() { return Object.entries(this.attrs).map(([name, value]) => ({ name, value })); }
  get innerHTML() { return this.textContent + this.children.map(child => child.outerHTML).join(''); }
  get outerHTML() {
    const tag = this.tagName.toLowerCase();
    const attrs = Object.entries(this.attrs).map(([name, value]) => ` ${name}="${value}"`).join('');
    return `<${tag}${attrs}>${this.innerHTML}</${tag}>`;
  }
  cloneNode() {
    const { tagName, textContent, attrs, children, parentElement, ...props } = this;
    return Object.assign(new Element(tagName, textContent, { ...attrs },
      children.map(child => child.cloneNode(true))), props);
  }
  querySelectorAll(selector) {
    assert.equal(selector, '*');
    return this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
  }
  closest() { return null; }
  focus() { this.focused = true; }
  dispatchEvent() { return true; }
  getBoundingClientRect() { return { x: 0, y: 0, width: 100, height: 30 }; }
}

(async () => {
  const email = Object.assign(new Element('INPUT', '', { 'aria-label': 'Email', value: 'a@b' }),
    { id: 'email', name: 'email', type: 'email', value: 'a@b' });
  const password = Object.assign(new Element('INPUT'),
    { id: 'password', name: 'password', type: 'password', value: '' });
  const confirm = Object.assign(new Element('INPUT', '', { 'aria-label': 'Confirm' }),
    { id: 'confirm', name: 'confirm', type: 'password', value: '' });
  const button = new Element('BUTTON', 'Log in');
  const inputs = [email, password, confirm];
  const form = new Element('FORM', '', {}, [...inputs, button]);
  const body = new Element('BODY', '', {}, [form]);
  const valueSelectorTargets = {
    'input[type=password][value^="K"]': [password],
    'input[value^="K"]': [password],
    'input[type=password][value^="K"] + button': [button],
    'form:has(input[type=password][value^="K"])': [form],
    '[*|VALUE^="K"]': [password],
    '[val\\ue^="K"]': [password],
    '[\\76 alue^="K"]': [password],
  };
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
        return { '#password': password, '#confirm': confirm, '#email': email, form }[selector] || null;
      },
      querySelectorAll: selector => {
        if (selector === 'input' || selector === 'input, select, textarea') return inputs;
        if (selector === '[value]') return inputs.filter(input => input.hasAttribute('value'));
        if (selector in valueSelectorTargets) return valueSelectorTargets[selector];
        if (selector === 'form') return [form];
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
  // React-controlled inputs mirror the live value into the value attribute.
  password.attrs.value = DUMMY;

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
  assert.equal(probe.results.length, 0, 'the password value matches nothing');
  const byValue = await send({ action: 'find', query: 'a@b' });
  assert.equal(byValue.results[0].selector, '#email');
  assert.ok(!byValue.results.some(r => r.selector === '#confirm'), 'unmatched fields are left out');

  // Markup and attribute paths drop the synced password value attribute only.
  const passwordHtml = await send({ action: 'getHTML', selector: '#password' });
  assertNoLeak('html', passwordHtml);
  assert.equal(passwordHtml.html, '<input></input>');
  const formHtml = await send({ action: 'getHTML', selector: 'form' });
  assertNoLeak('formHtml', formHtml);
  assert.match(formHtml.html, /value="a@b"/);
  assert.equal(password.getAttribute('value'), DUMMY, 'redaction does not mutate the page');

  const markup = await send({ action: 'query', selector: 'input', fields: ['html', 'attrs', 'Value', 'value'] });
  assertNoLeak('markup', markup);
  const [emailRow, passwordRow] = markup.items;
  assert.equal(emailRow.attrs.value, 'a@b');
  assert.equal(emailRow.Value, 'a@b');
  assert.equal('value' in passwordRow.attrs, false);
  assert.equal('Value' in passwordRow, false);
  assert.deepEqual([passwordRow.redacted, passwordRow.hasValue], [true, true]);
  const formRow = await send({ action: 'query', selector: 'form', fields: ['html'] });
  assertNoLeak('formRow', formRow);
  assert.match(formRow.items[0].html, /value="a@b"/);

  // Value-attribute selectors cannot be used as a character-by-character oracle.
  for (const selector of Object.keys(valueSelectorTargets)) {
    const queried = await send({ action: 'query', selector, fields: ['tag'] });
    assertNoLeak(`query ${selector}`, queried);
    assert.match(queried.error, /value-attribute selectors are disabled/, selector);
    const explained = await send({ action: 'explainSelector', selector });
    assertNoLeak(`explain ${selector}`, explained);
    assert.match(explained.error, /value-attribute selectors are disabled/, selector);
    const found = await send({ action: 'find', query: selector });
    assertNoLeak(`find ${selector}`, found);
    assert.ok(found.results.every(r => r.score !== undefined), `find ${selector} skips the CSS branch`);
    // Found vs. not-found on any selector-taking tool is the same oracle.
    for (const msg of [
      { action: 'getHTML', selector },
      { action: 'getBounds', selector },
      { action: 'focusElement', selector },
      { action: 'getPageText', selector },
      { action: 'formFill', fields: { [selector]: 'x' } },
    ]) {
      const res = await send(msg);
      assert.match(res.error, /value-attribute selectors are disabled/, `${msg.action} ${selector}`);
    }
  }
  delete password.attrs.value;
  const unsynced = await send({ action: 'query', selector: 'input[value^="K"]', fields: ['tag'] });
  assert.equal(unsynced.error, undefined, 'value selectors work when no password value attribute exists');

  assert.deepEqual(Object.keys(outputs).slice(0, 5), ['snapshot', 'forms', 'dom', 'query', 'find']);
  console.log('Password redaction checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
