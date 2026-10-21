"""Issue #72 in a real browser: sensitive-field redaction, with disposable values only.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
tests/sensitive_field_redaction.cjs checks the same rules on a mocked DOM; here the
browser decides what the rules see: Firefox's own password-manager autofill and its
`:autofill` state, computed `-webkit-text-security`, and open and closed shadow roots
behind Xray wrappers. Every read tool must hide the sensitive values while ordinary
login and checkout fields stay readable.

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
<script src="/page.js"></script>""",
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
    ("browser_query", "/api/query", {"selector": "input, [role]", "fields": ["id", "text", "value", "attrs", "html"]}),
    ("browser_find", "/api/find", {"query": MARKER}),
    ("browser_html", "/api/html", {"selector": "body"}),
    ("browser_page_text", "/api/page-text", {}),
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

    def assert_hidden(self, tab_id, reads, secrets):
        """No read returns a secret, and find never scores a match on one."""
        for tool, result in reads.items():
            text = json.dumps(result)
            for secret in secrets:
                self.assertNotIn(secret, text, f"{tool} leaks a sensitive value")
        for secret in secrets:
            found = self.call("/api/find", {"query": secret, "tabId": tab_id})
            self.assertNotIn(secret, json.dumps(found))
            self.assertTrue(all(r["score"] == 1 for r in found["results"]),
                            f"find scores only the interactive bonus on a sensitive value: {found}")

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
        self.assertRegex(reads["browser_snapshot"]["snapshot"], r'textbox "Username" \[ref=s\d+e\d+\]\n')
        self.assert_shown(reads, "q", "kittens", tab_id)

        # Editing the field, as a user would, makes Firefox clear its autofill state.
        edited = AUTOFILL_USER + "-edited"
        self.type_into(tab_id, "#username", edited, sensitive=False)
        self.assertEqual(self.autofill_state(tab_id), "false,true")
        reads = self.read_all(tab_id)
        self.assert_hidden(tab_id, reads, [AUTOFILL_PASSWORD])
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
        for selector, value in [("#vault", vault), ("#site-search", "kittens")]:
            result = self.assert_ok(self.call("/api/fill", {"selector": selector, "value": value, "tabId": tab_id}))
            self.assertIn("snapshot", result)
            self.assertNotIn(MARKER, json.dumps(result), "a write result echoes a filled value")
        # browser_fill cannot reach an input inside a closed root, so page code sets this one.
        self.js(tab_id, f"document.getElementById('closed-otp').wrappedJSObject.value = {json.dumps(code)}")
        # The values reached the inner inputs, and the hosts reflect them as page code would.
        self.assertEqual(self.js(tab_id, "['vault', 'closed-otp', 'site-search'].map(id => "
                                         "document.getElementById(id).wrappedJSObject.value).join(',')"),
                         f"{vault},{code},kittens")
        self.assertEqual(self.js(tab_id, "document.getElementById('vault').getAttribute('value')"), vault)

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
        self.assertRegex(reads["browser_snapshot"]["snapshot"], r'textbox "Vault key" \[ref=s\d+e\d+\]\n')
        # A host whose shadow input is not sensitive keeps its reflected value.
        search = self.row(reads, "site-search")["query"]
        self.assertEqual((search.get("redacted"), search["attrs"]["value"]), (None, "kittens"))
        self.assertIn('value="kittens"', reads["browser_html"]["html"])


if __name__ == "__main__":
    unittest.main()
