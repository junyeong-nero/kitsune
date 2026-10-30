"""Iframe forms in a real browser (issues #157 and #343).

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
checkout page embeds the address editor from #157 in a same-origin
<iframe title="배송지 편집"> and a payment widget from another origin (localhost instead of
127.0.0.1). Its server records every form it receives, which is how the tests prove that
typing and clicking through refs reached the frame's form.
"""
from http.server import BaseHTTPRequestHandler
import json
import re
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse

import real_browser_harness as harness

ZIP = "06236"
DETAIL = "KITSUNE_FAKE_DETAIL_157"
HOLDER = "KITSUNE_FAKE_HOLDER_343"
CARD = "4111111111111111"


class AddressSite:
    def __init__(self):
        self.lock = threading.Lock()
        self.saved = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                port = self.server.server_address[1]
                pages = {
                    "/checkout": "<!doctype html><title>checkout</title><h1>주문서</h1>"
                                 '<iframe title="배송지 편집" src="/address-editor" width="500" height="250"></iframe>'
                                 f'<iframe title="결제 위젯" src="http://localhost:{port}/widget?session=KITSUNE_FAKE_TOKEN"'
                                 ' width="300" height="160" style="border: 5px solid; padding: 7px"></iframe>'
                                 # A page control that changes the widget's content (#354).
                                 '<button type="button" id="coupon">쿠폰 적용</button><script>'
                                 "document.getElementById('coupon').onclick = () => document.querySelector("
                                 "'iframe[title=\"결제 위젯\"]').contentWindow.postMessage('coupon', '*');</script>",
                    "/address-editor": "<!doctype html><title>address</title>"
                                       '<form method="post" action="/address">'
                                       '<label>우편번호 <input name="zip" value="04524"></label>'
                                       '<label>상세주소 <input name="detail"></label>'
                                       "<button>저장</button></form>",
                    "/widget": '<!doctype html><title>widget</title><form method="post" action="/pay">'
                               f'<label>카드 번호 <input name="card" autocomplete="cc-number" value="{CARD}"></label>'
                               '<label>카드 소유자 <input name="holder"></label>'
                               '<button>결제</button><button type="button" id="check">카드 확인</button></form>'
                               # Controls the widget adds: after its own button, and on the page's message.
                               "<script>const add = name => { const b = document.createElement('button');"
                               " b.type = 'button'; b.textContent = name; document.body.append(b); };"
                               " document.getElementById('check').onclick = () => add('확인 취소');"
                               " addEventListener('message', e => { if (e.data === 'coupon') add('쿠폰 해제'); });</script>",
                }
                page = pages.get(urlparse(self.path).path)
                if page is None:
                    self.send_error(404)
                    return
                self.reply(page)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                with site.lock:
                    site.saved.append((urlparse(self.path).path, form))
                self.reply("<!doctype html><p>저장되었습니다</p>")

            def reply(self, text):
                data = text.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = harness.FixtureServer(Handler)
        self.origin = self.server.origin

    def wait_for_save(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if self.saved:
                    return list(self.saved)
            time.sleep(0.1)
        with self.lock:
            return list(self.saved)

    def stop(self):
        self.server.stop()


# Test-only: a screen point (click false, so nothing is clicked) asked for from the page.
SCREEN_POINT_SCRIPT = """
browser.runtime.onMessage.addListener((msg, sender) => {
  if (msg.action !== 'testScreenPoint') return;
  return forwardToContent('screenPoint', { tabId: sender.tab.id, selector: msg.selector, zoom: 1 });
});
"""


class RealBrowserIframeTests(harness.RealBrowserTestCase):
    @classmethod
    def extension_scripts(cls):
        return {"test-screen-point.js": SCREEN_POINT_SCRIPT}

    @classmethod
    def setUpClass(cls):
        cls.site = AddressSite()
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def ref(self, tab, line):
        snapshot = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        self.assertNotIn(CARD, snapshot)
        match = re.search(re.escape(line) + r" \[ref=(e\d+)\]", snapshot)
        self.assertIsNotNone(match, f"{line} in\n{snapshot}")
        return match.group(1), snapshot

    def test_same_origin_frame_form_is_observed_filled_and_saved_through_refs(self):
        with self.site.lock:
            self.site.saved.clear()
        tab = self.open_tab(f"{self.site.origin}/checkout")
        _, snapshot = self.ref(tab, 'iframe "배송지 편집"')
        lines = snapshot.splitlines()
        frame = next(i for i, line in enumerate(lines) if 'iframe "배송지 편집"' in line)
        self.assertTrue(any('textbox "우편번호"' in line for line in lines[frame:]), snapshot)
        self.assertRegex(snapshot, r'textbox "상세주소" \[ref=e\d+\]')
        self.assertRegex(snapshot, r'button "저장" \[ref=e\d+\]')
        # The cross-origin widget is reported without its query.
        self.assertRegex(snapshot, r'iframe "결제 위젯" \[ref=e\d+\] '
                                   r'\[frame=cross_origin src="http://localhost:\d+/widget"\]')
        self.assertNotIn("KITSUNE_FAKE_TOKEN", snapshot)

        forms = self.call("/api/forms", {"tabId": tab})
        fields = {f["name"]: f for f in forms["fields"]}
        self.assertEqual(fields["zip"]["frame"], "배송지 편집", forms)
        self.assertEqual((fields["zip"]["label"], fields["zip"]["value"]), ("우편번호", "04524"))
        self.assertEqual(fields["detail"]["frame"], "배송지 편집", forms)
        # The cross-origin widget's fields are read too (#354), so it is not unavailable.
        self.assertEqual(fields["card"]["frame"], "결제 위젯", forms)
        self.assertNotIn("unavailableFrames", forms)

        zip_ref, _ = self.ref(tab, 'textbox "우편번호"')
        self.assert_ok(self.call("/api/fill", {"tabId": tab, "ref": zip_ref, "value": ZIP}))
        detail_ref, _ = self.ref(tab, 'textbox "상세주소"')
        self.assert_ok(self.call("/api/type", {"tabId": tab, "ref": detail_ref, "text": DETAIL}))
        save_ref, _ = self.ref(tab, 'button "저장"')
        self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": save_ref}))
        self.assertEqual(self.site.wait_for_save(), [("/address", {"zip": ZIP, "detail": DETAIL})])

    def test_cross_origin_frame_is_listed_under_its_line_and_acted_on_through_refs(self):
        with self.site.lock:
            self.site.saved.clear()
        tab = self.open_tab(f"{self.site.origin}/checkout")
        _, snapshot = self.ref(tab, 'button "결제"')
        lines = snapshot.splitlines()
        frame = next(i for i, line in enumerate(lines) if 'iframe "결제 위젯"' in line)
        indent = len(lines[frame]) - len(lines[frame].lstrip())
        # The frame's content follows its line, one level deeper; the card number stays redacted.
        inner = []
        for line in lines[frame + 1:]:
            if len(line) - len(line.lstrip()) <= indent:
                break
            inner.append(line.strip())
        self.assertTrue(any(line.startswith('- textbox "카드 번호"') for line in inner), snapshot)
        self.assertTrue(any(line.startswith('- textbox "카드 소유자"') for line in inner), snapshot)
        self.assertTrue(any(line.startswith('- button "결제"') for line in inner), snapshot)
        self.assertNotIn(CARD, snapshot)

        holder_ref, _ = self.ref(tab, 'textbox "카드 소유자"')
        self.assert_ok(self.call("/api/fill", {"tabId": tab, "ref": holder_ref, "value": HOLDER}))
        pay_ref, _ = self.ref(tab, 'button "결제"')
        self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": pay_ref}))
        self.assertEqual(self.site.wait_for_save(), [("/pay", {"card": CARD, "holder": HOLDER})])

    def test_cross_origin_frame_screen_point_is_converted_by_the_page_showing_the_frame(self):
        # browser_trusted_click clicks at screenPoint's screenX/Y. A cross-origin frame's own
        # mozInnerScreenX/Y are 0, so its point must be converted by the page showing it.
        tab = self.open_tab(f"{self.site.origin}/checkout")
        pay_ref, _ = self.ref(tab, 'button "결제"')
        inner = self.call("/api/js", {"tabId": tab, "selector": pay_ref, "code":
            "JSON.stringify([mozInnerScreenX, mozInnerScreenY])"})
        self.assertEqual(json.loads(inner["result"]), [0, 0], "Fission no longer hides the frame's position")
        point = self.call("/api/js", {"tabId": tab, "code":
            f"browser.runtime.sendMessage({{ action: 'testScreenPoint', selector: '{pay_ref}' }})"
            ".then(JSON.stringify)"})
        point = json.loads(point["result"])
        self.assertNotIn("error", point)
        local = json.loads(self.call("/api/js", {"tabId": tab, "selector": pay_ref, "code":
            "(r => JSON.stringify([r.left + r.width / 2, r.top + r.height / 2]))"
            "(__kitsuneElement.getBoundingClientRect())"})["result"])
        origin = json.loads(self.call("/api/js", {"tabId": tab, "code":
            "(() => { const f = document.querySelector('iframe[title=\"결제 위젯\"]');"
            " const r = f.getBoundingClientRect(); const s = getComputedStyle(f);"
            " return JSON.stringify([mozInnerScreenX + r.left + f.clientLeft + parseFloat(s.paddingLeft),"
            " mozInnerScreenY + r.top + f.clientTop + parseFloat(s.paddingTop)]); })()"})["result"])
        self.assertGreater(origin[0], 0)
        self.assertAlmostEqual(point["screenX"], origin[0] + local[0], delta=1)
        self.assertAlmostEqual(point["screenY"], origin[1] + local[1], delta=1)

    def widget_origin(self, tab):
        """Where the widget's viewport starts in the page's viewport: inside its border and padding."""
        return json.loads(self.call("/api/js", {"tabId": tab, "code":
            "(() => { const f = document.querySelector('iframe[title=\"결제 위젯\"]');"
            " const r = f.getBoundingClientRect(); const s = getComputedStyle(f);"
            " return JSON.stringify([r.left + f.clientLeft + parseFloat(s.paddingLeft),"
            " r.top + f.clientTop + parseFloat(s.paddingTop)]); })()"})["result"])

    def frame_position(self, tab, ref):
        """The element's top left corner in its own frame's viewport."""
        return json.loads(self.call("/api/js", {"tabId": tab, "selector": ref, "code":
            "(r => JSON.stringify([r.left, r.top]))(__kitsuneElement.getBoundingClientRect())"})["result"])

    def assert_page_bounds(self, tab, result, origin):
        local = self.frame_position(tab, result["ref"])
        self.assertAlmostEqual(result["bounds"][0], origin[0] + local[0], delta=1, msg=result)
        self.assertAlmostEqual(result["bounds"][1], origin[1] + local[1], delta=1, msg=result)

    def test_find_forms_and_dom_read_the_cross_origin_frame_and_their_refs_act_there(self):
        # #354: as browser_snapshot does since #343. Bounds are in the page's viewport, and
        # results from the frame carry no CSS selector, which would run in the page.
        with self.site.lock:
            self.site.saved.clear()
        tab = self.open_tab(f"{self.site.origin}/checkout")
        origin = self.widget_origin(tab)

        found = self.call("/api/find", {"tabId": tab, "query": "결제"})
        pay = next((r for r in found["results"] if (r["tag"], r["text"]) == ("button", "결제")), None)
        self.assertIsNotNone(pay, found)
        self.assertRegex(pay["ref"], r"^e\d+$")
        self.assertNotIn("selector", pay)
        self.assertNotIn("hint", found)
        self.assert_page_bounds(tab, pay, origin)

        forms = self.call("/api/forms", {"tabId": tab})
        self.assertNotIn(CARD, json.dumps(forms))
        fields = {f["name"]: f for f in forms["fields"]}
        self.assertEqual([fields[name]["frame"] for name in ("zip", "card", "holder")],
                         ["배송지 편집", "결제 위젯", "결제 위젯"], forms)
        self.assertEqual(fields["holder"]["label"], "카드 소유자")
        self.assertEqual((fields["card"].get("redacted"), fields["card"].get("hasValue")), (True, True), forms)
        self.assertNotIn("selector", fields["holder"])
        self.assertIn("selector", fields["zip"])
        self.assertNotIn("unavailableFrames", forms)
        self.assert_page_bounds(tab, fields["holder"], origin)

        dom = self.call("/api/dom", {"tabId": tab})
        self.assertNotIn(CARD, json.dumps(dom))

        def nodes(node):
            yield node
            for child in node.get("children", []):
                yield from nodes(child)

        frame = next((n for n in nodes(dom["tree"]) if n.get("title") == "결제 위젯"), None)
        self.assertIsNotNone(frame, dom)
        self.assertEqual(frame.get("frame"), "cross_origin", frame)
        self.assertNotIn("content", frame)
        inner = list(nodes(frame))[1:]
        self.assertIn("holder", [n.get("name") for n in inner])
        button = next((n for n in inner if (n["tag"], n.get("text")) == ("button", "결제")), None)
        self.assertIsNotNone(button, frame)
        self.assert_page_bounds(tab, button, origin)

        # Refs from these reads act inside the frame.
        self.assert_ok(self.call("/api/fill", {"tabId": tab, "ref": fields["holder"]["ref"], "value": HOLDER}))
        self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": pay["ref"]}))
        self.assertEqual(self.site.wait_for_save(), [("/pay", {"card": CARD, "holder": HOLDER})])

    def test_action_deltas_list_what_changed_inside_the_cross_origin_frame(self):
        tab = self.open_tab(f"{self.site.origin}/checkout")
        check_ref, snapshot = self.ref(tab, 'button "카드 확인"')
        coupon_ref = re.search(r'button "쿠폰 적용" \[ref=(e\d+)\]', snapshot).group(1)

        def in_widget(result):
            """The lines below the widget's line, which reads frame=cross_origin."""
            self.assertTrue(result.get("snapshot_delta"), result)
            self.assertNotIn(CARD, result["snapshot"])
            lines = result["snapshot"].splitlines()
            frame = next((i for i, line in enumerate(lines) if 'iframe "결제 위젯"' in line), None)
            self.assertIsNotNone(frame, result["snapshot"])
            self.assertIn("[frame=cross_origin", lines[frame])
            indent = len(lines[frame]) - len(lines[frame].lstrip())
            inner = []
            for line in lines[frame + 1:]:
                if len(line) - len(line.lstrip()) <= indent:
                    break
                inner.append(line.strip())
            return inner

        # An action inside the frame: what it changed there, with its target.
        checked = self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": check_ref}))
        inner = in_widget(checked)
        self.assertTrue(any(line.startswith('- button "확인 취소"') for line in inner), checked["snapshot"])
        self.assertEqual(checked.get("target_ref"), check_ref)
        self.assertNotIn("no_visible_change", checked)
        for unchanged in ('heading "주문서"', 'textbox "우편번호"', 'button "쿠폰 적용"', 'textbox "카드 소유자"'):
            self.assertNotIn(unchanged, checked["snapshot"])

        # An action on the page that changes the frame; what the last delta showed is not repeated.
        applied = self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": coupon_ref}))
        inner = in_widget(applied)
        self.assertTrue(any(line.startswith('- button "쿠폰 해제"') for line in inner), applied["snapshot"])
        self.assertEqual(applied.get("target_ref"), coupon_ref)
        self.assertNotIn("확인 취소", applied["snapshot"])

        # The new control's ref acts in the frame; it changes nothing, and the delta says so.
        release = re.search(r'button "쿠폰 해제" \[ref=(e\d+)\]', applied["snapshot"]).group(1)
        released = self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": release}))
        self.assertEqual(released.get("target_ref"), release, released)
        self.assertTrue(released.get("no_visible_change"), released)


class FrameSite:
    """Pages of many, nested and busy cross-origin frames (#343); records the forms it receives."""

    def __init__(self):
        self.lock = threading.Lock()
        self.saved = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                port = self.server.server_address[1]
                other = f"http://localhost:{port}"
                same = f"http://127.0.0.1:{port}"
                query = parse_qs(urlparse(self.path).query)
                widgets = "".join(f'<iframe title="위젯 {i}" src="{other}/widget?i={i}" width="200" height="60"></iframe>'
                                  for i in range(8))
                pages = {
                    "/many": "<!doctype html><title>many</title><h1>위젯 목록</h1>" + widgets +
                             f'<iframe title="추적" src="{other}/widget?i=99" width="1" height="1"></iframe>',
                    "/widget": f'<!doctype html><button>위젯 버튼 {query.get("i", ["?"])[0]}</button>',
                    "/nested": f'<!doctype html><title>nested</title><iframe title="바깥" src="{other}/outer"'
                               ' width="400" height="200" style="margin-left: 30px"></iframe>',
                    "/outer": f'<!doctype html><p>바깥 프레임</p><iframe title="안쪽" src="{same}/inner"'
                              ' width="300" height="100" style="margin-left: 20px"></iframe>',
                    "/inner": '<!doctype html><form method="post" action="/inner-click">'
                              '<input type="hidden" name="from" value="inner"><button>안쪽 버튼</button></form>',
                    "/busy": f'<!doctype html><title>busy</title><h1>바쁜 프레임</h1><iframe title="바쁨" src="{other}/busyframe"'
                             ' width="300" height="100"></iframe>',
                    "/busyframe": "<!doctype html><button>멈춘 버튼</button><script>"
                                  "setTimeout(() => { const t = Date.now(); while (Date.now() - t < 8000); }, 300)"
                                  "</script>",
                }
                page = pages.get(urlparse(self.path).path)
                if page is None:
                    self.send_error(404)
                    return
                self.reply(page)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                with site.lock:
                    site.saved.append((urlparse(self.path).path, form))
                self.reply("<!doctype html><p>ok</p>")

            def reply(self, text):
                data = text.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = harness.FixtureServer(Handler)
        self.origin = self.server.origin

    wait_for_save = AddressSite.wait_for_save

    def stop(self):
        self.server.stop()


class RealBrowserCrossOriginFrameTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = FrameSite()
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    @classmethod
    def extension_scripts(cls):
        return {"test-screen-point.js": SCREEN_POINT_SCRIPT}

    def snapshot(self, tab):
        return self.call("/api/snapshot", {"tabId": tab})

    def js(self, tab, code, selector=None):
        body = {"tabId": tab, "code": code, **({"selector": selector} if selector else {})}
        return json.loads(self.call("/api/js", body)["result"])

    def test_a_snapshot_reads_six_frames_skips_tiny_ones_and_says_how_many_it_left(self):
        tab = self.open_tab(f"{self.site.origin}/many")
        result = self.snapshot(tab)
        snapshot = result["snapshot"]
        for i in range(6):
            self.assertIn(f'button "위젯 버튼 {i}"', snapshot)
        for i in (6, 7, 99):
            self.assertNotIn(f'위젯 버튼 {i}"', snapshot)
        self.assertEqual(snapshot.count("[frame=cross_origin"), 6, snapshot)
        self.assertRegex(snapshot, r'iframe "위젯 7" \[ref=e\d+\] \[content=cross_origin')
        self.assertIn("2 more cross-origin frames were not read", result.get("hint", ""), result)
        # A snapshot scoped to a frame left out reads it.
        frame_ref = re.search(r'iframe "위젯 7" \[ref=(e\d+)\]', snapshot).group(1)
        scoped = self.call("/api/snapshot", {"tabId": tab, "ref": frame_ref})["snapshot"]
        self.assertIn('button "위젯 버튼 7"', scoped)

    def test_nested_cross_origin_frames_are_read_clicked_and_located_on_screen(self):
        with self.site.lock:
            self.site.saved.clear()
        tab = self.open_tab(f"{self.site.origin}/nested")
        snapshot = self.snapshot(tab)["snapshot"]
        self.assertRegex(snapshot, r'(?m)^- iframe "바깥" \[ref=e\d+\] \[frame=cross_origin')
        self.assertRegex(snapshot, r'(?m)^  - iframe "안쪽" \[ref=e\d+\] \[frame=cross_origin')
        button = re.search(r'(?m)^ {6}- button "안쪽 버튼" \[ref=(e\d+)\]', snapshot)
        self.assertIsNotNone(button, snapshot)
        inner_frame = re.search(r'iframe "안쪽" \[ref=(e\d+)\]', snapshot).group(1)

        point = json.loads(self.call("/api/js", {"tabId": tab, "code":
            f"browser.runtime.sendMessage({{ action: 'testScreenPoint', selector: '{button.group(1)}' }})"
            ".then(JSON.stringify)"})["result"])
        self.assertNotIn("error", point)
        content_box = ("(f => { const r = f.getBoundingClientRect(); const s = getComputedStyle(f);"
                       " return JSON.stringify([r.left + f.clientLeft + parseFloat(s.paddingLeft),"
                       " r.top + f.clientTop + parseFloat(s.paddingTop)]); })")
        outer = self.js(tab, "(o => JSON.stringify([mozInnerScreenX + o[0], mozInnerScreenY + o[1]]))(JSON.parse("
                        + content_box + "(document.querySelector('iframe'))))")
        inner = self.js(tab, content_box + "(__kitsuneElement)", inner_frame)
        local = self.js(tab, "(r => JSON.stringify([r.left + r.width / 2, r.top + r.height / 2]))"
                        "(__kitsuneElement.getBoundingClientRect())", button.group(1))
        self.assertAlmostEqual(point["screenX"], outer[0] + inner[0] + local[0], delta=1)
        self.assertAlmostEqual(point["screenY"], outer[1] + inner[1] + local[1], delta=1)

        self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": button.group(1)}))
        self.assertEqual(self.site.wait_for_save(), [("/inner-click", {"from": "inner"})])

    def test_find_shares_the_frame_budget_and_places_nested_frames_on_the_page(self):
        # #354: six frames per read, as for snapshots; tiny frames are not read.
        tab = self.open_tab(f"{self.site.origin}/many")
        found = self.call("/api/find", {"tabId": tab, "query": "위젯 버튼"})
        self.assertEqual(sorted(r["text"] for r in found["results"] if r["tag"] == "button"),
                         [f"위젯 버튼 {i}" for i in range(6)], found)
        self.assertIn("2 more cross-origin frames were not read", found.get("hint", ""), found)

        tab = self.open_tab(f"{self.site.origin}/nested")
        inner_frame = re.search(r'iframe "안쪽" \[ref=(e\d+)\]', self.snapshot(tab)["snapshot"]).group(1)
        found = self.call("/api/find", {"tabId": tab, "query": "안쪽 버튼"})
        button = next((r for r in found["results"] if r["tag"] == "button"), None)
        self.assertIsNotNone(button, found)
        content_box = ("(f => { const r = f.getBoundingClientRect(); const s = getComputedStyle(f);"
                       " return JSON.stringify([r.left + f.clientLeft + parseFloat(s.paddingLeft),"
                       " r.top + f.clientTop + parseFloat(s.paddingTop)]); })")
        outer = self.js(tab, content_box + "(document.querySelector('iframe'))")
        inner = self.js(tab, content_box + "(__kitsuneElement)", inner_frame)
        local = self.js(tab, "(r => JSON.stringify([r.left, r.top]))(__kitsuneElement.getBoundingClientRect())",
                        button["ref"])
        self.assertAlmostEqual(button["bounds"][0], outer[0] + inner[0] + local[0], delta=1)
        self.assertAlmostEqual(button["bounds"][1], outer[1] + inner[1] + local[1], delta=1)

    def test_ask_before_act_holds_a_click_inside_a_cross_origin_frame(self):
        with self.site.lock:
            self.site.saved.clear()
        self.assert_ok(self.call("/api/set-policy", {"ask": True}))
        self.addCleanup(self.call, "/api/set-policy", {"ask": False})
        tab = self.open_tab(f"{self.site.origin}/nested")
        snapshot = self.snapshot(tab)["snapshot"]
        button = re.search(r'button "안쪽 버튼" \[ref=(e\d+)\]', snapshot).group(1)
        headers = {**self.bridge.auth(), "Content-Type": "application/json"}
        status, _, text = harness._request(self.bridge.http_port, "POST", "/api/click", headers, self.bridge.host,
                                           json.dumps({"tabId": tab, "ref": button}))
        held = json.loads(text)
        self.assertEqual((status, held.get("code")), (403, "confirmation_required"), held)
        self.assertEqual(held["state"]["reason"], "cross_origin_frame")
        self.assertRegex(held["error"], r"http://127\.0\.0\.1:\d+/inner")
        self.assertEqual(self.site.wait_for_save(timeout=1), [])
        # A click on the page itself is not held for the frame.
        heading = self.call("/api/click", {"tabId": tab, "ref": re.search(
            r'iframe "바깥" \[ref=(e\d+)\]', snapshot).group(1)})
        self.assertNotEqual(heading.get("code"), "confirmation_required", heading)
        self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": button,
                                                "confirmToken": held["state"]["confirmToken"]}))
        self.assertEqual(self.site.wait_for_save(), [("/inner-click", {"from": "inner"})])

    def test_a_frame_that_does_not_answer_leaves_the_snapshot_on_time(self):
        tab = self.open_tab(f"{self.site.origin}/busy")
        # The frame's script blocks its process for 8 s, starting 300 ms after it loads.
        time.sleep(1.5)
        started = time.monotonic()
        snapshot = self.snapshot(tab)["snapshot"]
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 4, snapshot)
        self.assertIn('heading "바쁜 프레임"', snapshot)
        self.assertRegex(snapshot, r'iframe "바쁨" \[ref=e\d+\] \[content=cross_origin')


if __name__ == "__main__":
    unittest.main()
