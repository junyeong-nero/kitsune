// Run with: node tests/ask_before_act.cjs (no browser or npm dependencies).
// describeTarget: the read-only inspection the bridge's ask-before-act policy relies on.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

class Element {
  constructor(tagName, { type = '', form = null, text = '', label = null, value = '', parent = null,
    role = null, attrs = {} } = {}) {
    Object.assign(this, { tagName, type, form, textContent: text, value, parentElement: parent,
      isConnected: true, hidden: false, style: { display: 'block', visibility: 'visible' } });
    this.label = label;
    this.role = role;
    this.attrs = attrs;
    this.children = [];
    if (parent) parent.children.push(this);
  }
  getAttribute(name) {
    return name === 'aria-label' ? this.label : name === 'role' ? this.role : this.attrs[name] ?? null;
  }
  hasAttribute(name) { return name in this.attrs; }
  closest(selector) {
    const parts = selector.split(',').map(s => s.trim());
    const matches = el => parts.some(part => part === '[role="button"]' ? el.role === 'button'
      : part === '[role="link"]' ? el.role === 'link'
        : part === 'a[href]' ? el.tagName === 'A' && el.hasAttribute('href')
          : part.toUpperCase() === el.tagName);
    for (let el = this; el; el = el.parentElement) if (matches(el)) return el;
    return null;
  }
  // Only the icon lookup of describeTarget: '[aria-label], img[alt], svg title'.
  querySelector(selector) {
    if (selector !== '[aria-label], img[alt], svg title') return null;
    for (const child of this.children) {
      if (child.label != null || (child.tagName === 'IMG' && child.hasAttribute('alt')) || child.tagName === 'TITLE') {
        return child;
      }
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
}

function contentScript(elements, active = null) {
  let listener;
  const context = vm.createContext({
    console, crypto: webcrypto, Event, setTimeout, CSS: { escape: x => x },
    window: { getComputedStyle: el => el.style },
    document: {
      body: new Element('BODY'),
      activeElement: active,
      querySelector: selector => elements[selector] || null,
      querySelectorAll: selector => elements[selector] ? [elements[selector]] : [],
      getElementById: id => elements[`#${id}`] || null,
      elementFromPoint: () => elements['#span'],
    },
    browser: { runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: () => Promise.resolve(null),
    } },
  });
  vm.runInContext(fs.readFileSync('extension/content/content.js', 'utf8'), context);
  return msg => new Promise(resolve => listener(msg, {}, resolve))
    .then(result => JSON.parse(JSON.stringify(result)));
}

async function testContentDescribeTarget() {
  const form = new Element('FORM');
  const submit = new Element('BUTTON', { type: 'submit', form, text: ' Place order ' });
  const span = new Element('SPAN', { text: 'Place order', parent: submit });
  const plain = new Element('BUTTON', { type: 'button', form, text: 'Apply coupon' });
  const outside = new Element('BUTTON', { type: 'submit', text: 'No form' });
  const imageInput = new Element('INPUT', { type: 'image', form, label: 'Pay' });
  const submitInput = new Element('INPUT', { type: 'submit', form, value: 'Buy now' });
  const search = new Element('INPUT', { type: 'search', form, value: 'secret query' });
  const checkbox = new Element('INPUT', { type: 'checkbox', form });
  const textarea = new Element('TEXTAREA', { form });
  const password = new Element('INPUT', { type: 'password', form, value: 'hunter2' });
  const send = contentScript({ '#submit': submit, '#span': span, '#plain': plain, '#outside': outside,
    '#image': imageInput, '#submitInput': submitInput, '#search': search, '#checkbox': checkbox,
    '#textarea': textarea, '#password': password }, search);
  const describe = (selector, extra = {}) => send({ action: 'describeTarget', selector, ...extra });

  assert.deepEqual(await describe('#submit'),
    { ok: true, tag: 'button', type: 'submit', inForm: true, submits: true, submitLabel: true,
      consequentialLabel: false, text: 'Place order' });
  assert.equal((await describe('#span')).submits, true, 'a click inside a submit button submits');
  assert.equal((await send({ action: 'describeTarget', coords: { x: 1, y: 2 } })).submits, true,
    'coordinates resolve like click');
  assert.equal((await describe('#plain')).submits, false);
  assert.equal((await describe('#outside')).submits, false, 'no form to submit');
  assert.equal((await describe('#image')).text, 'Pay');
  assert.deepEqual(await describe('#submitInput'),
    { ok: true, tag: 'input', type: 'submit', inForm: true, submits: true, submitLabel: true,
      consequentialLabel: false, text: 'Buy now' });

  // Enter in a text-like field of a form submits it; Enter elsewhere does not.
  assert.equal((await describe('#search')).submits, false, 'a click on a field does not submit');
  assert.equal((await describe('#search', { key: 'Enter' })).submits, true);
  assert.equal((await describe('#search', { key: 'Return' })).submits, true);
  assert.equal((await describe('#password', { key: 'Return' })).submits, true);
  assert.equal((await describe('#textarea', { key: 'Return' })).submits, false);
  assert.equal((await describe('#password', { key: 'Enter' })).submits, true);
  assert.equal((await describe('#checkbox', { key: 'Enter' })).submits, false);
  assert.equal((await describe('#textarea', { key: 'Enter' })).submits, false);
  assert.equal((await describe('#submit', { key: 'Enter' })).submits, true);
  assert.equal((await send({ action: 'describeTarget', key: 'Enter' })).submits, true,
    'without a selector, the focused element is inspected like keypress');

  // Field values are never reported.
  for (const selector of ['#search', '#password']) {
    const result = await describe(selector, { key: 'Enter' });
    assert.equal(result.text, '');
    assert.doesNotMatch(JSON.stringify(result), /secret query|hunter2/);
  }
  assert.equal((await describe('#missing')).code, 'not_found');

  // Draft mode inspects single fields and the whole fuzzy-fill target set before writing.
  assert.equal((await describe('#password', { draftFill: true })).sensitive, true);
  assert.equal((await describe('#search', { draftFill: true })).sensitive, false);
  assert.deepEqual(await send({ action: 'describeTarget', fieldKeys: ['#search', '#password'] }),
    { ok: true, sensitive: true });
  assert.deepEqual(await send({ action: 'describeTarget', fieldKeys: ['#search'] }),
    { ok: true, sensitive: false });
  assert.equal((await send({ action: 'describeTarget', fieldKeys: ['#missing'] })).code, 'not_found');
  assert.equal(password.value, 'hunter2', 'preflight never fills a sensitive field');
}

// Labels neither submitLabel nor consequentialLabel match, on buttons and links alike.
const NOT_HELD_LABELS = ['Submit search', 'Search', 'Apply coupon', 'Add to cart', 'Order history',
  'Payment methods', 'Pay later options', 'Check out our sale', 'OK', 'Cancel', 'Continue', 'Next', 'Sign in',
  'Subscribe', 'Order', 'Payment', '확인', '취소', '장바구니 담기', '주문내역', '결제수단 변경', '確認', '确认', 'Weiter',
  'Annuler', 'Buscar', 'Place order and some more words that make this label far too long to be a button'];

// JS-driven buttons (type=button, role=button, no form) are held by their label alone.
async function testContentSubmitLabel() {
  const elements = {};
  const add = (id, tagName, options) => { elements[id] = new Element(tagName, options); return elements[id]; };
  const send = contentScript(elements);
  const describe = async selector => (await send({ action: 'describeTarget', selector })).submitLabel;
  const held = ['Place order', 'Place your order', 'Submit order', 'Complete purchase', 'Pay', 'Pay now',
    'Pay $12.00', 'Pay 12,00 €', 'Pay with PayPal', 'Buy now', 'Buy it now', 'Checkout', 'Proceed to checkout',
    'Continue to payment', 'Confirm', 'Confirm and pay', 'Submit', '  PLACE ORDER  ', 'Place order (2 items)',
    'Place order →', '결제하기', '12,000원 결제하기', '결제하기 12,000원', '주문하기', '바로 구매', '바로구매',
    '구매하기', '제출', '결제 진행', '注文を確定する', '購入する', '今すぐ購入', 'レジに進む', '提交订单', '立即购买',
    '立即支付', '去结算', '結帳', 'Jetzt kaufen', 'Zahlungspflichtig bestellen', 'Zur Kasse', 'Payer',
    'Passer la commande', 'Comprar ahora', 'Realizar pedido', 'Finalizar compra'];
  const notHeld = [...NOT_HELD_LABELS, 'Send'];
  for (const [index, text] of [...held, ...notHeld].entries()) {
    add(`#role${index}`, 'DIV', { role: 'button', text });
    add(`#button${index}`, 'BUTTON', { type: 'button', text });
  }
  for (const [index, text] of [...held, ...notHeld].entries()) {
    const expected = index < held.length;
    assert.equal(await describe(`#role${index}`), expected, `role=button ${JSON.stringify(text)}`);
    assert.equal(await describe(`#button${index}`), expected, `type=button ${JSON.stringify(text)}`);
  }

  // aria-label and button input values are labels; a click inside the button resolves to it.
  const pay = add('#ariaPay', 'BUTTON', { type: 'button', label: 'Pay now', text: '' });
  add('#icon', 'SPAN', { text: '', parent: pay });
  add('#inputButton', 'INPUT', { type: 'button', value: '결제하기' });
  add('#anchor', 'A', { text: 'Place order' });
  add('#textField', 'INPUT', { type: 'text', value: 'Place order' });
  add('#roleLink', 'DIV', { role: 'link', text: 'Pay now' });
  assert.equal(await describe('#ariaPay'), true);
  assert.equal(await describe('#icon'), true, 'a click inside the button is labeled by the button');
  assert.deepEqual(await send({ action: 'describeTarget', selector: '#inputButton' }),
    { ok: true, tag: 'input', type: 'button', inForm: false, submits: false, submitLabel: true,
      consequentialLabel: false, text: '결제하기' });
  assert.equal(await describe('#anchor'), false, 'links are not buttons');
  assert.equal(await describe('#roleLink'), false);
  const field = await send({ action: 'describeTarget', selector: '#textField' });
  assert.equal(field.submitLabel, false, 'a field value is not a label');
  assert.doesNotMatch(JSON.stringify(field), /Place order/);
}

// Buttons and links whose accessible name sends, transfers, deletes or changes an account.
async function testContentConsequentialLabel() {
  const elements = {};
  const add = (id, tagName, options) => { elements[id] = new Element(tagName, options); return elements[id]; };
  const send = contentScript(elements);
  const describe = selector => send({ action: 'describeTarget', selector });
  const held = ['Send', 'Send now', 'Send money', 'Send $50', 'Transfer', 'Transfer funds', 'Transfer now',
    'Wire money', 'Confirm transfer', 'Confirm and send', 'Delete', 'Delete permanently', 'Remove',
    'Delete account', 'Delete my account', 'Permanently delete account', 'Delete repository', 'Close account',
    'Deactivate account', 'Cancel subscription', 'Cancel my subscription', 'Cancel order', 'End membership',
    'Unsubscribe', 'Unsubscribe from all', '  DELETE ACCOUNT →', 'Transfer (2 accounts)',
    '송금', '송금하기', '50,000원 송금하기', '이체하기', '바로 이체', '보내기', '전송', '삭제', '삭제하기', '계정 삭제',
    '회원탈퇴', '탈퇴하기', '구독 해지', '구독 취소', '해지하기', '주문 취소', '수신거부',
    '送金する', '振り込む', '送信', 'アカウントを削除', '削除する', '退会する', '解約', '配信停止', 'サブスクリプションを解約',
    '注文をキャンセル', '发送', '确认转账', '轉帳', '汇款', '删除', '删除账号', '刪除帳號', '注销账号', '退订', '取消订阅',
    '取消訂單', 'Senden', 'Jetzt überweisen', 'Löschen', 'Konto löschen', 'Jetzt kündigen', 'Abo kündigen',
    'Newsletter abbestellen', 'Überweisung ausführen', 'Envoyer', 'Supprimer', 'Supprimer mon compte',
    'Effectuer le virement', 'Se désabonner', 'Résilier l’abonnement', 'Résilier', 'Enviar', 'Transferir',
    'Eliminar cuenta', 'Eliminar mi cuenta', 'Darse de baja', 'Cancelar suscripción', 'Realizar transferencia',
    'Borrar'];
  const notHeld = [...NOT_HELD_LABELS, 'Send feedback form preview', 'Send feedback', 'Send code', 'Resend',
    'Sent', 'Transfer history', 'Transfers', 'Remove filter', 'Deleted', 'Undo delete', 'Account settings',
    'My account', 'Manage subscription', 'Cancel anytime', 'Unsubscribed', 'Close', '닫기', '보낸 편지함',
    '계정 설정', '이체 내역', '送信履歴', 'キャンセル', '取消', '设置', 'Abbrechen', 'Mon compte', 'Mi cuenta', 'Cancelar',
    'Delete account and some more words that make this label far too long to be a link'];
  const tags = { role: ['DIV', { role: 'button' }], button: ['BUTTON', { type: 'button' }],
    link: ['A', { attrs: { href: '/x' } }], roleLink: ['SPAN', { role: 'link' }] };
  const labels = [...held, ...notHeld];
  for (const [index, text] of labels.entries()) {
    for (const [kind, [tagName, options]] of Object.entries(tags)) add(`#${kind}${index}`, tagName, { ...options, text });
  }
  for (const [index, text] of labels.entries()) {
    const expected = index < held.length;
    for (const kind of Object.keys(tags)) {
      const result = await describe(`#${kind}${index}`);
      assert.equal(result.consequentialLabel, expected, `${kind} ${JSON.stringify(text)}`);
      if (expected) assert.equal(result.submitLabel, false, `${kind} ${JSON.stringify(text)} is not a submit label`);
    }
  }

  // Links are never submit labels, and an <a> without href is not a link.
  add('#orderLink', 'A', { attrs: { href: '/order' }, text: 'Place order' });
  add('#plainAnchor', 'A', { text: 'Delete account' });
  assert.deepEqual(await describe('#orderLink'), { ok: true, tag: 'a', type: '', inForm: false, submits: false,
    submitLabel: false, consequentialLabel: false, text: 'Place order' });
  assert.equal((await describe('#plainAnchor')).consequentialLabel, false);

  // Icon-only controls are labeled by their accessible name.
  const named = { '#titled': 'Delete', '#imgIcon': 'Remove', '#svgIcon': 'Delete account', '#svgTitle': 'Unsubscribe',
    '#labelledBy': 'Close account', '#iconLink': 'Unsubscribe' };
  add('#titled', 'BUTTON', { type: 'button', attrs: { title: 'Delete' } });
  add('#img', 'IMG', { attrs: { alt: 'Remove' }, parent: add('#imgIcon', 'BUTTON', { type: 'button' }) });
  add('#svg', 'SVG', { label: 'Delete account', parent: add('#svgIcon', 'DIV', { role: 'button' }) });
  const svg = add('#svg2', 'SVG', { parent: add('#svgTitle', 'BUTTON', { type: 'button' }) });
  add('#svg2title', 'TITLE', { text: 'Unsubscribe', parent: svg });
  add('#heading', 'SPAN', { text: 'Close  account' });
  add('#labelledBy', 'BUTTON', { type: 'button', label: 'Menu', attrs: { 'aria-labelledby': 'missing heading' } });
  add('#linkImg', 'IMG', { attrs: { alt: 'Unsubscribe' }, parent: add('#iconLink', 'A', { attrs: { href: '/u' } }) });
  add('#unnamed', 'BUTTON', { type: 'button' });
  add('#unnamedIcon', 'SVG', { parent: elements['#unnamed'] });
  add('#textWins', 'BUTTON', { type: 'button', text: 'Edit', attrs: { title: 'Delete' } });
  for (const [selector, text] of Object.entries(named)) {
    const result = await describe(selector);
    assert.equal(result.consequentialLabel, true, selector);
    assert.equal(result.text.replace(/\s+/g, ' '), text, selector);
  }
  assert.equal((await describe('#img')).consequentialLabel, true, 'a click on the icon is labeled by its button');
  assert.deepEqual(await describe('#unnamed'), { ok: true, tag: 'button', type: 'button', inForm: false,
    submits: false, submitLabel: false, consequentialLabel: false, text: '' });
  assert.equal((await describe('#textWins')).consequentialLabel, false, 'title is the last fallback');
}

async function testBackgroundDescribeTarget() {
  const tabs = { 1: { id: 1, url: 'https://shop.example/checkout', active: true, status: 'complete' },
    2: { id: 2, url: 'https://example.com/', active: false, status: 'complete' } };
  const sent = [];
  const event = () => ({ addListener() {}, removeListener() {} });
  const browser = {
    tabs: {
      onCreated: event(), onUpdated: event(), onRemoved: event(),
      query: async () => Object.values(tabs).filter(tab => tab.active),
      get: async id => { if (!tabs[id]) throw new Error(`Invalid tab ID: ${id}`); return { ...tabs[id] }; },
      sendMessage: async (id, msg) => {
        if (msg.action === 'ping') return { ok: true, version: Infinity };
        sent.push({ tabId: id, ...msg });
        return { ok: true, tag: 'button', submits: true };
      },
      executeScript: async () => {},
    },
    runtime: { onMessage: event(), onStartup: event(), onInstalled: event() },
  };
  const context = vm.createContext({
    browser, URL, setTimeout, clearTimeout, setInterval, clearInterval,
    console: { log() {}, error() {} }, WebSocket: class { close() {} },
  });
  vm.runInContext(fs.readFileSync('extension/background/background.js', 'utf8'), context);
  const send = command => context.handleCommand({ includeSnapshot: false, ...command })
    .then(result => JSON.parse(JSON.stringify(result)));

  assert.deepEqual(await send({ action: 'describeTarget' }), { tabId: 1, url: 'https://shop.example/checkout' });
  assert.equal(sent.length, 0, 'without inspect nothing reaches the page');
  assert.deepEqual(await send({ action: 'describeTarget', tabId: 2, ref: 's1e3', inspect: true }),
    { tabId: 2, url: 'https://example.com/', target: { ok: true, tag: 'button', submits: true } });
  assert.equal(sent[0].action, 'describeTarget');
  assert.equal(sent[0].selector, 's1e3', 'refs resolve like other element commands');
  assert.equal((await send({ action: 'describeTarget', tabId: 5 })).code, 'tab_closed');
}

(async () => {
  await testContentDescribeTarget();
  await testContentSubmitLabel();
  await testContentConsequentialLabel();
  await testBackgroundDescribeTarget();
  console.log('ask_before_act.cjs: ok');
})().catch(error => { console.error(error); process.exit(1); });
