"""Issue #72 in a real browser: sensitive-field redaction, with disposable values only.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
tests/sensitive_field_redaction.cjs checks the same rules on a mocked DOM; here the
browser decides what the rules see: Firefox's own password-manager autofill and its
`:autofill` state, computed `-webkit-text-security` (on inputs, textareas, editable
regions and static text), and open and closed shadow roots behind Xray wrappers. Every read tool must hide the sensitive values while ordinary
login and checkout fields and ordinary text stay readable.

The password manager is real: setUpClass saves one disposable login, for the login
origin only, in the temporary profile, and Firefox fills the fixture login form on load.
"""
from http.server import BaseHTTPRequestHandler
import json
import re
import time
import unittest

import real_browser_harness as harness

MARKER = "KITSUNE_FAKE"


def fake(label):
    return f"{MARKER}_{label}_000"


AUTOFILL_USER = fake("AUTOFILL_USER")
AUTOFILL_PASSWORD = fake("AUTOFILL_PASSWORD")
CARD_NUMBER = fake("CARD_NUMBER")
TEXTAREA_INITIAL = fake("TEXTAREA_INITIAL")
EDITABLE_TEXT = fake("EDITABLE")
EDITABLE_LINE = fake("EDITABLE_LINE")
# Masked static text, and the contenteditable="false" parts of a masked editing region.
STATIC_TEXT = {label: fake(label) for label in (
    "STATIC", "STATIC_CODE", "STATIC_BOX", "STATIC_INHERITED", "STATIC_BUTTON", "STATIC_CELL", "STATIC_PRE")}
REGION_TEXT = {label: fake(label) for label in ("REGION", "REGION_CHIP", "REGION_CHIP_CLEAR")}

PAGES = {
    "/login": """<!doctype html><title>Sign in</title>
<form role="search" action="/search"><input id="q" type="search" name="q" aria-label="Search" value="kittens"></form>
<h1>Sign in</h1>
<form id="login" action="/session" method="post">
<label for="username">Username</label><input id="username" name="username" autocomplete="username">
<label for="password">Password</label><input id="password" type="password" name="password">
<button type="button" id="toggle">Show password</button>
<label><input id="remember" type="checkbox" name="remember"> Remember me</label>
<button id="sign-in">Sign in</button>
</form>
<form id="newsletter"><label for="newsletter-email">Email</label><input id="newsletter-email" type="email" name="email"></form>
<script src="/page.js"></script>""",
    "/checkout": """<!doctype html><title>Checkout</title>
<style>.masked { -webkit-text-security: disc; }</style>
<form role="search" action="/search"><input id="q" type="search" name="q" aria-label="Search products" value="kittens"></form>
<h1>Checkout</h1>
<form id="checkout" action="/order" method="post">
<label for="email">Email</label><input id="email" type="email" name="email" autocomplete="email">
<label for="phone">Phone</label><input id="phone" type="tel" name="phone" autocomplete="tel">
<label for="cc-name">Name on card</label><input id="cc-name" name="ccname" autocomplete="cc-name">
<label for="card">Card number</label><input id="card" name="cardnumber" autocomplete="cc-number"
  inputmode="numeric" value="%s">
<label for="cc-exp">Expiry</label><input id="cc-exp" name="exp" autocomplete="cc-exp" placeholder="MM / YY">
<label for="cvc">Security code</label><input id="cvc" type="tel" name="cvc" maxlength="4">
<label for="postal">ZIP / postal code</label><input id="postal" name="postalCode" inputmode="numeric"
  autocomplete="postal-code">
<label for="promo">Promo code</label><input id="promo" name="promoCode">
<div class="masked"><label for="account">Account number</label><input id="account" name="account"></div>
<label for="routing">Routing number</label><input id="routing" name="routing"
  style="-webkit-text-security: square">
<label for="otp">Verification code</label><input id="otp" name="smsToken" autocomplete="one-time-code"
  inputmode="numeric">
<label for="code">Code from your app</label><input id="code" name="code" inputmode="numeric" maxlength="6">
<button>Pay</button>
</form>""" % CARD_NUMBER,
    "/widgets": """<!doctype html><title>Widgets</title>
<h1>Account vault</h1>
<x-secret-input id="vault" role="textbox" aria-label="Vault key"></x-secret-input>
<x-code-input id="closed-otp" role="textbox" aria-label="Login code"></x-code-input>
<x-search-input id="site-search" role="searchbox" aria-label="Search"></x-search-input>
<x-nested-input id="nested" aria-label="Nickname"></x-nested-input>
<x-nested-code id="nested-code" role="textbox" aria-label="Backup code"></x-nested-code>
<x-deep-input id="too-deep" levels="9" aria-label="Deep field"></x-deep-input>
<script src="/page.js"></script>""",
    "/notes": """<!doctype html><title>Notes</title>
<style>.masked { -webkit-text-security: disc; }</style>
<h1>Account notes</h1>
<form id="notes-form">
<label>Recovery codes <textarea id="recovery" name="recovery" class="masked">%s</textarea></label>
<label for="notes">Notes</label><textarea id="notes" name="notes">plain notes</textarea>
</form>
<section><h2>Secret note</h2>
<div id="secret-note" contenteditable="true" style="-webkit-text-security: square">%s<p id="secret-note-line">%s</p></div>
</section>
<section><h2>Draft</h2><div id="draft" contenteditable="true">draft text</div></section>"""
    % (TEXTAREA_INITIAL, EDITABLE_TEXT, EDITABLE_LINE),
    "/masked-text": """<!doctype html><title>Masked text</title>
<style>.masked { -webkit-text-security: disc; } .unmasked { -webkit-text-security: none; }</style>
<h1>Recovery</h1>
<p id="masked-static" class="masked">%(STATIC)s</p>
<p id="code-line">Backup code: <span id="code-span" class="masked">%(STATIC_CODE)s</span></p>
<div id="masked-box" class="masked">%(STATIC_BOX)s <span id="masked-inherited">%(STATIC_INHERITED)s</span>
<b id="mask-none" class="unmasked">shown inside mask</b></div>
<p id="mask-none-p" class="unmasked">ordinary text</p>
<p id="plain">plain paragraph</p>
<button id="masked-button" class="masked">%(STATIC_BUTTON)s</button>
<label id="labelled-by">Recovery code</label>
<input id="labelled" aria-labelledby="labelled-by masked-static">
<table><tr><th>Name</th><th>Token</th></tr><tr><td>primary</td><td id="masked-cell" class="masked">%(STATIC_CELL)s</td></tr></table>
<pre id="token-pre">token: <span class="masked">%(STATIC_PRE)s</span></pre>
<section><h2>Secret note</h2>
<div id="secret-region" contenteditable="true" class="masked">%(REGION)s
<span id="region-chip" contenteditable="false">%(REGION_CHIP)s</span>
<span id="region-chip-clear" contenteditable="false" class="unmasked">%(REGION_CHIP_CLEAR)s</span></div>
</section>""" % {**STATIC_TEXT, **REGION_TEXT},
}

# Fixture scripts stay out of the pages' markup; they hold no sensitive values.
PAGE_SCRIPT = """'use strict';
const toggle = document.getElementById('toggle');
if (toggle) toggle.onclick = () => {
  const password = document.getElementById('password');
  password.type = password.type === 'password' ? 'text' : 'password';
};
// Each control reflects its inner input's value into a value attribute on the host.
function defineControl(name, mode, inner) {
  customElements.define(name, class extends HTMLElement {
    constructor() {
      super();
      const root = this.attachShadow({ mode });
      root.innerHTML = inner;
      this.input = root.querySelector('input');
      this.input.addEventListener('input', () => this.setAttribute('value', this.input.value));
    }
    get value() { return this.input.value; }
    set value(value) { this.input.value = value; this.setAttribute('value', value); }
  });
}
defineControl('x-secret-input', 'open', '<input type="password">');
defineControl('x-code-input', 'closed', '<input autocomplete="one-time-code" inputmode="numeric">');
defineControl('x-search-input', 'open', '<input type="search">');
// Inputs two closed roots deep: an ordinary one, and a one-time code inside x-code-input.
defineControl('x-inner-field', 'closed', '<input>');
function defineNested(name, inner) {
  customElements.define(name, class extends HTMLElement {
    constructor() {
      super();
      const root = this.attachShadow({ mode: 'closed' });
      root.innerHTML = `<${inner}></${inner}>`;
      this.inner = root.firstChild;
    }
    get value() { return this.inner.value; }
  });
}
defineNested('x-nested-input', 'x-inner-field');
defineNested('x-nested-code', 'x-code-input');
// An input `levels` closed roots deep; nine is one more than browser_fill searches.
customElements.define('x-deep-input', class extends HTMLElement {
  constructor() {
    super();
    this.root = this.attachShadow({ mode: 'closed' });
  }
  connectedCallback() {
    if (this.root.firstChild) return;
    const levels = Number(this.getAttribute('levels'));
    const child = document.createElement(levels > 1 ? 'x-deep-input' : 'input');
    if (levels > 1) child.setAttribute('levels', levels - 1);
    this.root.append(child);
  }
  get value() { return this.root.firstChild.value; }
});
"""


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/page.js":
            content_type, body = "text/javascript", PAGE_SCRIPT
        elif self.path in PAGES:
            content_type, body = "text/html", PAGES[self.path]
        else:
            self.send_error(404)
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


# Saves one login in the temporary profile; returns how many logins that origin has.
SAVE_LOGIN_SCRIPT = """(async () => {
  const [origin, username, password] = %s;
  const login = Cc['@mozilla.org/login-manager/loginInfo;1'].createInstance(Ci.nsILoginInfo);
  login.init(origin, origin, null, username, password, 'username', 'password');
  await Services.logins.addLoginAsync(login);
  return (await Services.logins.searchLoginsAsync({ origin })).length;
})()"""

# Every read tool checked, as (MCP tool, bridge endpoint, body without tabId).
READS = [
    ("browser_snapshot", "/api/snapshot", {}),
    ("browser_snapshot text", "/api/snapshot", {"text": True}),
    ("browser_forms", "/api/forms", {}),
    ("browser_dom", "/api/dom", {}),
    ("browser_query", "/api/query", {"selector": "input, textarea, [contenteditable], [contenteditable] *, [role]",
                                     "fields": ["id", "text", "value", "attrs", "html"]}),
    ("browser_find", "/api/find", {"query": MARKER}),
    ("browser_html", "/api/html", {"selector": "body"}),
    ("browser_page_text", "/api/page-text", {}),
    ("browser_markdown", "/api/markdown", {}),
    ("browser_readability", "/api/readability", {}),
]


def dom_nodes(node):
    yield node
    for child in node.get("children", []):
        yield from dom_nodes(child)


class RealBrowserRedactionTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        # Only this origin has a saved login, so the other pages are never autofilled.
        cls.login_site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.login_site.stop)
        super().setUpClass()
        debugger = harness.RemoteDebugger(cls.browser.rdp_port)
        try:
            saved = debugger.evaluate_in_parent(
                SAVE_LOGIN_SCRIPT % json.dumps([cls.login_site.origin, AUTOFILL_USER, AUTOFILL_PASSWORD]))
        finally:
            debugger.close()
        if saved != 1:
            raise AssertionError(f"expected one disposable login, found {saved}")

    def js(self, tab_id, code):
        """Page state through browser_js, which redaction deliberately does not cover."""
        result = self.call("/api/js", {"code": code, "tabId": tab_id})
        self.assertNotIn("error", result)
        return result["result"]

    def read_all(self, tab_id):
        reads = {}
        for tool, path, body in READS:
            result = self.call(path, {**body, "tabId": tab_id})
            self.assertNotIn("error", result, f"{tool}: {result}")
            reads[tool] = result
        return reads

    def assert_hidden(self, tab_id, reads, secrets, shown=()):
        """No read returns a secret, and find never scores a match on one.

        shown: ids of ordinary fields whose values share words with a secret, so find matches them.
        """
        for tool, result in reads.items():
            text = json.dumps(result)
            for secret in secrets:
                self.assertNotIn(secret, text, f"{tool} leaks a sensitive value")
        for secret in secrets:
            found = self.call("/api/find", {"query": secret, "tabId": tab_id})
            self.assertNotIn(secret, json.dumps(found))
            # A secret's label words may name its field; a decoy with the same label finds the same.
            decoy = self.call("/api/find", {"query": secret.replace(MARKER, "KITSUNE_DECOY"), "tabId": tab_id})
            summary = lambda result: [(r["selector"], r["text"], r["score"]) for r in result["results"]
                                      if r["selector"] not in {f"#{field_id}" for field_id in shown}]
            self.assertEqual(summary(found), summary(decoy), f"find scores a match on a sensitive value: {found}")

    def row(self, reads, field_id):
        return {
            "forms": next((f for f in reads["browser_forms"]["fields"] if f["id"] == field_id), None),
            "dom": next((n for n in dom_nodes(reads["browser_dom"]["tree"]) if n.get("id") == field_id), None),
            "query": next(r for r in reads["browser_query"]["items"] if r.get("id") == field_id),
        }

    def assert_redacted(self, reads, field_id, listed_in_forms=True):
        """Every structured read marks the field redacted and returns no value."""
        rows = self.row(reads, field_id)
        if listed_in_forms:
            self.assertEqual({k: rows["forms"].get(k) for k in ("redacted", "hasValue", "value")},
                             {"redacted": True, "hasValue": True, "value": None}, field_id)
        else:
            self.assertIsNone(rows["forms"], f"{field_id} is a shadow host, not a form field")
        self.assertIs(rows["dom"]["redacted"], True, field_id)
        self.assertNotIn("value", rows["dom"], field_id)
        self.assertIs(rows["query"]["redacted"], True, field_id)
        self.assertNotIn("value", rows["query"], field_id)
        self.assertNotIn("value", rows["query"]["attrs"], field_id)

    def assert_shown(self, reads, field_id, value, tab_id):
        """An ordinary field keeps its value in every read that reports values."""
        rows = self.row(reads, field_id)
        self.assertEqual((rows["forms"]["value"], rows["forms"].get("redacted")), (value, None), field_id)
        self.assertEqual((rows["dom"]["value"], rows["dom"].get("redacted")), (value, None), field_id)
        self.assertEqual((rows["query"]["value"], rows["query"].get("redacted")), (value, None), field_id)
        for tool in ("browser_snapshot", "browser_snapshot text"):
            self.assertIn(f"[value={json.dumps(value)}]", reads[tool]["snapshot"], f"{tool}: {field_id}")
        found = self.call("/api/find", {"query": value, "tabId": tab_id})["results"]
        self.assertTrue(any(r["selector"] == f"#{field_id}" and r["score"] > 1 for r in found), found)

    def type_into(self, tab_id, selector, text, sensitive=True):
        """Type as an agent would; a sensitive value must not come back in the post-action snapshot."""
        result = self.assert_ok(self.call("/api/type", {"selector": selector, "text": text, "clear": True,
                                                        "tabId": tab_id, "includeSnapshot": sensitive}))
        if sensitive:
            self.assertIn("snapshot", result)
            self.assertNotIn(text, json.dumps(result), "a write result echoes the typed value")

    def autofill_state(self, tab_id):
        return self.js(tab_id, "['username', 'password'].map(id => "
                               "document.getElementById(id).matches(':autofill')).join(',')")

    # ── Password manager ──

    def test_password_manager_autofill_is_redacted_until_the_browser_clears_it(self):
        tab_id = self.open_tab(self.login_site.origin + "/login")
        # New tabs open in the background (#139); Firefox autofills a login form only once it is shown.
        self.assert_ok(self.call("/api/switch-tab", {"tabId": tab_id}))
        deadline = time.monotonic() + 10
        while self.autofill_state(tab_id) != "true,true":
            self.assertLess(time.monotonic(), deadline, "Firefox did not autofill the disposable login")
            time.sleep(0.1)
        self.assertEqual(self.js(tab_id, "document.getElementById('username').value"), AUTOFILL_USER)

        reads = self.read_all(tab_id)
        self.assert_hidden(tab_id, reads, [AUTOFILL_USER, AUTOFILL_PASSWORD])
        # The username is only sensitive because the password manager filled it.
        self.assert_redacted(reads, "username")
        self.assert_redacted(reads, "password")
        self.assertRegex(reads["browser_snapshot"]["snapshot"], r'textbox "Username" \[ref=e\d+\]\n')
        self.assert_shown(reads, "q", "kittens", tab_id)

        # Editing the field, as a user would, makes Firefox clear its autofill state.
        edited = AUTOFILL_USER + "-edited"
        self.type_into(tab_id, "#username", edited, sensitive=False)
        self.assertEqual(self.autofill_state(tab_id), "false,true")
        reads = self.read_all(tab_id)
        self.assert_hidden(tab_id, reads, [AUTOFILL_PASSWORD], shown=["username"])
        self.assert_shown(reads, "username", edited, tab_id)
        self.assert_redacted(reads, "password")

    # ── Login and checkout pages: what is redacted, and what is not ──

    def test_login_page_redacts_the_password_but_not_the_username_search_or_email(self):
        tab_id = self.open_tab(self.site.origin + "/login")
        self.assertEqual(self.autofill_state(tab_id), "false,false", "no login is saved for this origin")
        password = fake("TYPED_PASSWORD")
        self.type_into(tab_id, "#username", "fixture-user", sensitive=False)
        self.type_into(tab_id, "#password", password)
        self.type_into(tab_id, "#newsletter-email", "reader@example.test", sensitive=False)

        reads = self.read_all(tab_id)
        self.assert_hidden(tab_id, reads, [password])
        self.assert_redacted(reads, "password")
        for field_id, value in [("username", "fixture-user"), ("newsletter-email", "reader@example.test"),
                                ("q", "kittens")]:
            with self.subTest(field=field_id):
                self.assert_shown(reads, field_id, value, tab_id)

        # A show-password toggle switches the field to type=text; it stays redacted.
        self.assert_ok(self.call("/api/click", {"selector": "#toggle", "tabId": tab_id, "includeSnapshot": False}))
        self.assertEqual(self.js(tab_id, "document.getElementById('password').type"), "text")
        reads = self.read_all(tab_id)
        self.assert_hidden(tab_id, reads, [password])
        self.assert_redacted(reads, "password")

    def test_checkout_page_redacts_card_code_and_masked_fields_only(self):
        tab_id = self.open_tab(self.site.origin + "/checkout")
        secrets = {"cvc": fake("CVC"), "account": fake("MASKED_ACCOUNT"), "routing": fake("MASKED_ROUTING"),
                   "otp": fake("OTP"), "code": fake("APP_CODE")}
        for field_id, value in secrets.items():
            self.type_into(tab_id, f"#{field_id}", value)
        shown = {"email": "buyer@example.test", "phone": "555-0100", "cc-name": "Fixture Buyer",
                 "cc-exp": "12 / 30", "postal": "12345", "promo": "SPRING10"}
        for field_id, value in shown.items():
            self.type_into(tab_id, f"#{field_id}", value, sensitive=False)
        # The masks come from computed style: one inherited from a class, one inline.
        self.assertEqual(self.js(tab_id, "['account', 'routing'].map(id => getComputedStyle("
                                         "document.getElementById(id)).webkitTextSecurity).join(',')"), "disc,square")

        reads = self.read_all(tab_id)
        self.assert_hidden(tab_id, reads, [CARD_NUMBER, *secrets.values()])
        for field_id in ["card", *secrets]:
            with self.subTest(field=field_id):
                self.assert_redacted(reads, field_id)
        for field_id, value in {**shown, "q": "kittens"}.items():
            with self.subTest(field=field_id):
                self.assert_shown(reads, field_id, value, tab_id)
        # The card number's value attribute is dropped from markup, so it cannot be probed either.
        self.assertIn('id="card"', reads["browser_html"]["html"])
        probe = self.call("/api/query", {"selector": 'input[value^="K"]', "tabId": tab_id})
        self.assertEqual(probe.get("error"),
                         "value-attribute selectors are disabled while a sensitive field has a value attribute")

    # ── Shadow DOM ──

    def test_open_and_closed_shadow_dom_fields_are_redacted_on_their_hosts(self):
        tab_id = self.open_tab(self.site.origin + "/widgets")
        vault, code = fake("SHADOW_VAULT"), fake("CLOSED_SHADOW_CODE")
        # browser_type (mode="type") also reaches the input inside the closed root.
        self.type_into(tab_id, "#closed-otp", fake("CLOSED_SHADOW_TYPED"))
        self.assertEqual(self.js(tab_id, "document.getElementById('closed-otp').wrappedJSObject.value"),
                         fake("CLOSED_SHADOW_TYPED"))
        # browser_fill reaches inputs inside open and closed roots (#107).
        for selector, value in [("#vault", vault), ("#closed-otp", code), ("#site-search", "kittens")]:
            result = self.assert_ok(self.call("/api/fill", {"selector": selector, "value": value, "tabId": tab_id}))
            self.assertEqual((result["pierced"], result["tag"]), (True, "input"), selector)
            self.assertIn("snapshot", result)
            self.assertNotIn(MARKER, json.dumps(result), "a write result echoes a filled value")
        # The values reached the inner inputs, and the hosts reflect them as page code would.
        self.assertEqual(self.js(tab_id, "['vault', 'closed-otp', 'site-search'].map(id => "
                                         "document.getElementById(id).wrappedJSObject.value).join(',')"),
                         f"{vault},{code},kittens")
        self.assertEqual(self.js(tab_id, "['vault', 'closed-otp'].map(id => "
                                         "document.getElementById(id).getAttribute('value')).join(',')"),
                         f"{vault},{code}")

        reads = self.read_all(tab_id)
        self.assert_hidden(tab_id, reads, [vault, code])
        for field_id in ("vault", "closed-otp"):
            with self.subTest(field=field_id):
                rows = self.row(reads, field_id)
                self.assertIsNone(rows["forms"], "shadow inputs are not light-DOM form fields")
                # Xray wrappers hide the page-defined value property, so hasValue is false.
                self.assertEqual((rows["dom"]["redacted"], rows["dom"]["hasValue"]), (True, False))
                self.assertEqual((rows["query"]["redacted"], rows["query"]["hasValue"]), (True, False))
                self.assertNotIn("value", rows["query"]["attrs"])
        self.assertRegex(reads["browser_snapshot"]["snapshot"], r'textbox "Vault key" \[ref=e\d+\]\n')
        # A host whose shadow input is not sensitive keeps its reflected value.
        search = self.row(reads, "site-search")["query"]
        self.assertEqual((search.get("redacted"), search["attrs"]["value"]), (None, "kittens"))
        self.assertIn('value="kittens"', reads["browser_html"]["html"])

    def host_value(self, tab_id, field_id):
        return self.js(tab_id, f"document.getElementById('{field_id}').wrappedJSObject.value")

    def test_fill_type_and_form_fill_reach_inputs_two_closed_shadow_roots_deep(self):
        tab_id = self.open_tab(self.site.origin + "/widgets")
        # #117: each tool writes through two nested closed roots.
        filled, typed, form = fake("NESTED_FILLED"), fake("NESTED_TYPED"), fake("NESTED_FORM")
        result = self.assert_ok(self.call("/api/fill", {"selector": "#nested-code", "value": filled,
                                                        "tabId": tab_id, "includeSnapshot": True}))
        self.assertEqual((result["pierced"], result["tag"]), (True, "input"))
        self.assertIn("snapshot", result)
        self.assertNotIn(MARKER, json.dumps(result), "a write result echoes a filled value")
        self.assertEqual(self.host_value(tab_id, "nested-code"), filled)
        self.type_into(tab_id, "#nested-code", typed)
        self.assertEqual(self.host_value(tab_id, "nested-code"), typed)
        # form_fill returns no post-action snapshot; the reads below cover it.
        result = self.call("/api/form-fill", {"fields": {"#nested-code": form}, "tabId": tab_id})
        self.assertNotIn("error", result)
        self.assertEqual(result["results"], {"#nested-code": {"ok": True, "tag": "input"}})
        self.assertNotIn(MARKER, json.dumps(result), "a write result echoes a filled value")
        self.assertEqual(self.host_value(tab_id, "nested-code"), form)
        # An ordinary field two closed roots deep, through each tool.
        self.assert_ok(self.call("/api/fill", {"selector": "#nested", "value": "fixture-fill", "tabId": tab_id}))
        self.assertEqual(self.host_value(tab_id, "nested"), "fixture-fill")
        self.type_into(tab_id, "#nested", "fixture-type", sensitive=False)
        self.assertEqual(self.host_value(tab_id, "nested"), "fixture-type")
        result = self.call("/api/form-fill", {"fields": {"#nested": "fixture-form"}, "tabId": tab_id})
        self.assertEqual(result.get("results"), {"#nested": {"ok": True, "tag": "input"}}, result)
        self.assertEqual(self.host_value(tab_id, "nested"), "fixture-form")

        # The one-time code stays redacted on its host, two roots up.
        reads = self.read_all(tab_id)
        self.assert_hidden(tab_id, reads, [filled, typed, form])
        rows = self.row(reads, "nested-code")
        self.assertIsNone(rows["forms"], "shadow inputs are not light-DOM form fields")
        self.assertIs(rows["dom"]["redacted"], True)
        self.assertIs(rows["query"]["redacted"], True)
        self.assertRegex(reads["browser_snapshot"]["snapshot"],
                         re.compile(r'textbox "Backup code" \[ref=e\d+\]$', re.M))

    def test_a_field_deeper_than_the_shadow_search_fails_as_not_fillable(self):
        tab_id = self.open_tab(self.site.origin + "/widgets")
        self.assertEqual(self.js(tab_id, "document.getElementById('too-deep').wrappedJSObject.value"), "",
                         "the fixture's input exists nine closed roots deep")
        value = "fixture-deep"
        results = {
            "fill": self.call("/api/fill", {"selector": "#too-deep", "value": value, "tabId": tab_id}),
            "type": self.call("/api/type", {"selector": "#too-deep", "text": value, "clear": True,
                                            "tabId": tab_id}),
        }
        form = self.call("/api/form-fill", {"fields": {"#too-deep": value}, "tabId": tab_id})
        self.assertEqual(form.get("error"), "No field was filled", form)
        results["form_fill"] = form["results"]["#too-deep"]
        for tool, result in results.items():
            with self.subTest(tool=tool):
                self.assert_error(result, "not_fillable", True)
                self.assertRegex(result["error"], r"^Element is not a fillable field: .* within 8 nested shadow roots")
                # Direct fill/type errors also carry the tab state (#65); form_fill keys do not.
                self.assertEqual({k: result["state"][k] for k in ("tag", "reason")},
                                 {"tag": "x-deep-input", "reason": "shadow_depth"}, result)
                self.assertNotIn(value, json.dumps(result))
        self.assertNotIn(value, json.dumps(form))
        self.assertEqual(self.host_value(tab_id, "too-deep"), "")

    # ── Textareas and editable regions ──

    def test_masked_textarea_and_editable_region_are_redacted_but_ordinary_ones_are_not(self):
        tab_id = self.open_tab(self.site.origin + "/notes")
        typed = fake("TEXTAREA_TYPED")
        self.type_into(tab_id, "#recovery", typed)
        # The masks come from computed style, and the nested paragraph inherits its region's mask.
        self.assertEqual(self.js(tab_id, "['recovery', 'secret-note', 'secret-note-line'].map(id => getComputedStyle("
                                         "document.getElementById(id)).webkitTextSecurity).join(',')"),
                         "disc,square,square")
        # A textarea's initial content stays in its text and markup after typing changes the value.
        self.assertEqual(self.js(tab_id, "document.getElementById('recovery').textContent"), TEXTAREA_INITIAL)

        reads = self.read_all(tab_id)
        self.assert_hidden(tab_id, reads, [typed, TEXTAREA_INITIAL, EDITABLE_TEXT, EDITABLE_LINE])
        self.assert_redacted(reads, "recovery")
        for field_id in ("secret-note", "secret-note-line"):
            with self.subTest(field=field_id):
                self.assert_redacted(reads, field_id, listed_in_forms=False)
                rows = self.row(reads, field_id)
                self.assertIs(rows["dom"]["hasValue"], True)
                self.assertIs(rows["query"]["hasValue"], True)
        for field_id in ("recovery", "secret-note", "secret-note-line"):
            with self.subTest(field=field_id):
                rows = self.row(reads, field_id)
                self.assertNotIn("text", rows["dom"])
                self.assertEqual((rows["query"]["text"], rows["query"]["html"]), ("", ""))
        html = reads["browser_html"]["html"]
        self.assertIn('<textarea id="recovery" name="recovery" class="masked"></textarea>', html)
        self.assertIn('style="-webkit-text-security: square"></div>', html)
        # The label wrapping the masked textarea names it without the textarea's content.
        self.assertRegex(reads["browser_snapshot"]["snapshot"], r'textbox "Recovery codes" \[ref=e\d+\]\n')
        # Page text is rendered text, where the browser shows the square mask's characters.
        self.assertIn("\u25a0" * len(EDITABLE_TEXT), reads["browser_page_text"]["text"])

        # No false positives: the ordinary textarea and editable region keep their content.
        self.assert_shown(reads, "notes", "plain notes", tab_id)
        draft = self.row(reads, "draft")
        self.assertEqual((draft["dom"]["text"], draft["dom"].get("redacted")), ("draft text", None))
        self.assertEqual((draft["query"]["text"], draft["query"]["html"], draft["query"].get("redacted")),
                         ("draft text", "draft text", None))
        notes_query = self.row(reads, "notes")["query"]
        self.assertEqual((notes_query["text"], notes_query["html"]), ("plain notes", "plain notes"))
        self.assertIn('<textarea id="notes" name="notes">plain notes</textarea>', html)
        self.assertIn('<div id="draft" contenteditable="true">draft text</div>', html)
        for tool in ("browser_snapshot", "browser_snapshot text"):
            self.assertIn('textbox "draft text"', reads[tool]["snapshot"], tool)
        self.assertIn("draft text", reads["browser_page_text"]["text"])
        self.assertIn("draft text", reads["browser_markdown"]["markdown"])
        self.assertIn("draft text", reads["browser_readability"]["textContent"])
        found = self.call("/api/find", {"query": "draft text", "tabId": tab_id})["results"]
        self.assertTrue(any(r["selector"] == "#draft" and r["score"] > 1 for r in found), found)

    # ── Masked static text ──

    def test_masked_static_text_and_masked_region_parts_are_left_out_but_ordinary_text_is_not(self):
        tab_id = self.open_tab(self.site.origin + "/masked-text")
        # The masks come from computed style: inherited, and overridden with `none`.
        self.assertEqual(self.js(tab_id, "['masked-static', 'code-span', 'masked-inherited', 'mask-none', "
                                         "'mask-none-p', 'plain', 'region-chip', 'region-chip-clear'].map(id => "
                                         "getComputedStyle(document.getElementById(id)).webkitTextSecurity).join(',')"),
                         "disc,disc,disc,none,none,none,disc,none")

        reads = self.read_all(tab_id)
        reads["browser_query static"] = self.call("/api/query", {
            "selector": "p, span, b, div, button, td, pre", "fields": ["id", "text", "html"], "tabId": tab_id})
        reads["browser_explain_selector"] = self.call("/api/explain-selector", {"selector": "p", "tabId": tab_id})
        for selector in ("#masked-static", "#masked-box", "#region-chip-clear"):
            reads[f"browser_wait_for {selector}"] = self.call(
                "/api/wait-for-element", {"selector": selector, "timeout": 1000, "tabId": tab_id})
        reads["browser_click"] = self.assert_ok(self.call("/api/click", {
            "selector": "#masked-button", "tabId": tab_id, "includeSnapshot": True}))
        page_text = reads.pop("browser_page_text")["text"]
        self.assert_hidden(tab_id, reads, [*STATIC_TEXT.values(), *REGION_TEXT.values()])

        query = {row.get("id"): row for row in reads["browser_query static"]["items"] if row.get("id")}
        for field_id in ("masked-static", "code-span", "masked-inherited", "masked-button", "masked-cell",
                         "region-chip", "region-chip-clear"):
            with self.subTest(field=field_id):
                self.assertEqual((query[field_id]["text"], query[field_id]["html"]), ("", ""))
        dom = {node.get("id"): node for node in dom_nodes(reads["browser_dom"]["tree"]) if node.get("id")}
        for field_id in ("masked-static", "code-span", "masked-button", "region-chip", "region-chip-clear"):
            with self.subTest(field=field_id):
                self.assertNotIn("text", dom[field_id])
        self.assertEqual(reads["browser_wait_for #masked-static"]["text"], "")
        self.assertEqual(reads["browser_click"]["text"], "")
        self.assertRegex(reads["browser_snapshot"]["snapshot"], r'textbox "Recovery code" \[ref=e\d+\]')
        self.assertIn('<p id="masked-static" class="masked"></p>', reads["browser_html"]["html"])

        # No false positives: ordinary text, unmasked elements and elements whose mask is
        # `none` keep their content, and masked text is left out rather than shown as bullets.
        self.assertEqual((query["code-line"]["text"], query["code-line"]["html"]),
                         ("Backup code:", 'Backup code: <span id="code-span" class="masked"></span>'))
        self.assertEqual(query["masked-box"]["text"], "shown inside mask")
        for field_id, text in (("mask-none", "shown inside mask"), ("mask-none-p", "ordinary text"),
                               ("plain", "plain paragraph")):
            with self.subTest(field=field_id):
                self.assertEqual((query[field_id]["text"], query[field_id]["html"]), (text, text))
                self.assertEqual(dom[field_id]["text"], text)
        self.assertEqual(reads["browser_wait_for #masked-box"]["text"], "shown inside mask")
        text_snapshot = reads["browser_snapshot text"]["snapshot"]
        for line in ("Backup code:", "shown inside mask", "ordinary text", "plain paragraph"):
            self.assertRegex(text_snapshot, rf'- text "{line}"\n')
        markdown = reads["browser_markdown"]["markdown"]
        for text in ("Backup code:", "**shown inside mask**", "ordinary text", "plain paragraph", "| primary |  |",
                     "token: "):
            self.assertIn(text, markdown)
        for text in ("Backup code:", "shown inside mask", "ordinary text", "plain paragraph"):
            self.assertIn(text, reads["browser_readability"]["textContent"])
            self.assertIn(text, page_text)
        for query_text, selector in (("ordinary text", "#mask-none-p"), ("plain paragraph", "#plain"),
                                     ("Backup code", "#code-line")):
            found = self.call("/api/find", {"query": query_text, "tabId": tab_id})["results"]
            self.assertTrue(any(r["selector"] == selector and r["score"] > 1 for r in found), found)

        # Page text is rendered text: masked text shows as mask characters, and the masked
        # region's part that opts out with `none` is drawn in clear, so it is the one value there.
        self.assertIn("\u2022" * len(STATIC_TEXT["STATIC"]), page_text)
        self.assertEqual(re.findall(r"KITSUNE_FAKE_[A-Z_]+?_000", page_text), [REGION_TEXT["REGION_CHIP_CLEAR"]])

if __name__ == "__main__":
    unittest.main()
