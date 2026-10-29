"""The core tools are Playwright MCP's: published verbatim, mapped onto the Kitsune bridge."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from kitsune import bridge, playwright_tools, server, tools

ROOT = Path(__file__).resolve().parent.parent
VENDORED = json.loads((ROOT / "src" / "kitsune" / "playwright_tools.json").read_text(encoding="utf-8"))

LIST_SCRIPT = """import asyncio, json
from kitsune.server import mcp
print(json.dumps([t.model_dump(mode="json", exclude_none=True) for t in asyncio.run(mcp.list_tools())]))
"""


class PublishedSurfaceTests(unittest.TestCase):
    def test_default_tools_list_is_playwright_mcp_verbatim(self):
        env = {key: value for key, value in os.environ.items() if not key.startswith("KITSUNE_")}
        listed = json.loads(subprocess.run([sys.executable, "-c", LIST_SCRIPT], env=env, text=True,
                                           capture_output=True, check=True).stdout)
        self.assertEqual(listed, VENDORED["tools"])

    def test_every_vendored_tool_has_an_adapter(self):
        self.assertEqual([function.__name__ for function in playwright_tools._TOOLS],
                         [tool["name"] for tool in VENDORED["tools"]])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        playwright_tools._current_tab = None
        playwright_tools._clicked.clear()
        playwright_tools._requests.clear()
        post = patch.object(tools, "_post", return_value={"ok": True})
        get = patch.object(tools, "_get", return_value={"tabs": []})
        self.post, self.get = post.start(), get.start()
        self.addCleanup(post.stop)
        self.addCleanup(get.stop)

    def posted(self):
        return [(call.args[0], call.args[1]) for call in self.post.call_args_list]

    def answer_snapshot(self, *lines):
        self.post.return_value = {"snapshot": "\n".join(lines)}

    def test_targets_are_refs_or_selectors(self):
        playwright_tools.browser_click(target="e12", element="Pay")
        playwright_tools.browser_click(target="#pay", button="right", modifiers=["Shift"])
        playwright_tools.browser_click(target="e3", doubleClick=True)
        self.assertEqual(self.posted(), [
            ("/api/click", {"selector": "", "ref": "e12", "element": "Pay"}),
            ("/api/click", {"selector": "#pay", "modifiers": ["Shift"], "button": "right"}),
            ("/api/double-click", {"selector": "", "ref": "e3"}),
        ])
        self.assertIn("error", playwright_tools.browser_click(target="e3", doubleClick=True, button="right"))

    def test_type_fills_or_types_slowly_then_submits(self):
        playwright_tools.browser_type(target="e1", text="hi")
        playwright_tools.browser_type(target="#q", text="hi", slowly=True, submit=True)
        self.assertEqual(self.posted(), [
            ("/api/fill", {"selector": "", "value": "hi", "ref": "e1"}),
            ("/api/type", {"selector": "#q", "text": "hi", "clear": False}),
            ("/api/keypress", {"key": "Enter", "selector": "#q"}),
        ])

    def test_press_key_splits_modifier_combinations(self):
        for key, expected in (("ArrowLeft", {"key": "ArrowLeft"}),
                              ("Control+Shift+A", {"key": "A", "modifiers": {"ctrl": True, "shift": True}}),
                              ("+", {"key": "+"}), ("Shift++", {"key": "Shift++"})):
            with self.subTest(key=key):
                self.post.reset_mock()
                playwright_tools.browser_press_key(key=key)
                self.assertEqual(self.posted(), [("/api/keypress", expected)])

    def test_tabs_track_the_current_tab(self):
        tabs = {"tabs": [{"id": 3, "title": "A", "url": "https://a/", "active": True},
                         {"id": 8, "title": "B", "url": "https://b/", "owned": True}]}
        self.get.return_value = tabs
        self.assertEqual(playwright_tools.browser_tabs("list"), {"tabs": [
            {"index": 0, "title": "A", "url": "https://a/", "active": True},
            {"index": 1, "title": "B", "url": "https://b/", "owned": True}]})
        self.assertEqual(playwright_tools.browser_tabs("select", index=1)["index"], 1)
        playwright_tools.browser_navigate(url="https://c/")
        self.assertEqual(self.posted()[-1], ("/api/navigate", {"url": "https://c/", "tabId": 8}))
        self.assertTrue(playwright_tools.browser_tabs("list")["tabs"][1]["current"])
        self.assertEqual(playwright_tools.browser_tabs("select", index=5)["error"], "Tab 5 not found")

        self.post.return_value = {"ok": True, "tabId": 11}
        self.get.return_value = {"tabs": [*tabs["tabs"], {"id": 11, "title": "D", "url": "https://d/", "owned": True}]}
        playwright_tools.browser_tabs("new", url="https://d/")
        self.assertEqual(self.posted()[-1][1]["url"], "https://d/")
        self.assertIs(self.posted()[-1][1]["wait"], True)
        self.assertEqual(playwright_tools._current_tab, 11)

        self.post.return_value = {"ok": True}
        playwright_tools.browser_close()
        self.assertEqual(self.posted()[-1], ("/api/close-tab", {"tabId": 11}))
        self.assertIsNone(playwright_tools._current_tab)
        # Without a current tab, close takes the active tab.
        playwright_tools.browser_tabs("close")
        self.assertEqual(self.posted()[-1], ("/api/close-tab", {"tabId": 3}))

    def test_a_current_tab_that_closed_is_forgotten(self):
        playwright_tools._current_tab = 99
        self.get.return_value = {"tabs": [{"id": 3, "title": "A", "url": "https://a/", "active": True}]}
        playwright_tools.browser_tabs("list")
        self.assertIsNone(playwright_tools._current_tab)

    def test_snapshot_scope_depth_boxes_and_file(self):
        playwright_tools.browser_snapshot(target="e4", depth=2, boxes=True)
        self.assertEqual(self.posted(), [("/api/snapshot", {"maxChars": 12000, "ref": "e4", "maxDepth": 2,
                                                            "boxes": True})])
        self.assertIn("error", playwright_tools.browser_snapshot(depth=1.5))
        self.post.return_value = {"snapshot": '- button "OK" [ref=e1]', "url": "https://a/"}
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "cwd", return_value=Path(directory)):
            result = playwright_tools.browser_snapshot(filename="out/snap.yml")
            self.assertEqual((Path(directory) / "out" / "snap.yml").read_text(), '- button "OK" [ref=e1]')
        self.assertEqual(result, {"ok": True, "saved_to": str(Path(directory) / "out" / "snap.yml"),
                                  "url": "https://a/"})
        # A saved snapshot is whole (#262).
        self.assertEqual(self.posted()[-1], ("/api/snapshot", {}))

    def test_hidden_tab_fields_reach_core_and_kitsune_results(self):
        # The extension adds visibilityState and the not_rendered hint flags (#328); the tools keep them.
        snapshot = {"snapshot": "", "url": "https://a/", "readyState": "complete", "empty": True,
                    "hint": "The page shows no content yet.", "visibilityState": "hidden"}
        self.post.return_value = snapshot
        self.assertEqual(playwright_tools.browser_snapshot(), snapshot)
        self.assertEqual(tools.browser_snapshot(tab_id=3, text=True), snapshot)
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "cwd", return_value=Path(directory)):
            saved = playwright_tools.browser_snapshot(filename="snap.yml")
        self.assertEqual((saved["visibilityState"], saved["empty"]), ("hidden", True))
        clicked = {"ok": True, "tag": "div", "visibilityState": "hidden", "snapshot": "",
                   "warning": {"code": "hidden_or_covered", "hint": "The tab is in the background, ...",
                               "state": {"reason": "not_rendered", "hiddenTab": True, "inPopup": True}}}
        self.post.return_value = clicked
        self.assertEqual(playwright_tools.browser_click(target="e3"), clicked)
        self.assertEqual(tools.browser_click(ref="e3", tab_id=3), clicked)

    def test_inline_snapshot_truncation_names_core_tools_only(self):
        marker = "- … truncated; scope with selector, ref or max_depth"
        self.post.return_value = {"snapshot": '- link "Desk 1" [ref=e1]\n  - /url: "https://a/1"\n' + marker,
                                  "truncated": True, "url": "https://a/", "title": "Desks",
                                  "hint": "Snapshot truncated; scope with selector, ref or max_depth."}
        result = playwright_tools.browser_snapshot()
        self.assertEqual(self.posted(), [("/api/snapshot", {"maxChars": playwright_tools.SNAPSHOT_LIMIT})])
        lines = result["snapshot"].split("\n")
        self.assertEqual(lines[:2], ['- link "Desk 1" [ref=e1]', '  - /url: "https://a/1"'])
        self.assertEqual(lines[-1], "- … truncated; browser_find searches the rest")
        # The bridge reserved the extension marker's length; the core marker fits in it.
        self.assertLessEqual(len(lines[-1]), len(marker))
        self.assertEqual((result["truncated"], result["url"], result["title"]), (True, "https://a/", "Desks"))
        self.assertIn("12,000 characters", result["hint"])
        guidance = lines[-1] + " " + result["hint"]
        for name in ("selector", "max_depth", "tab_id", "text=", "browser_dom", "browser_page_text"):
            self.assertNotIn(name, guidance)
        self.assertIn("browser_find", playwright_tools.SCHEMAS)
        snapshot_parameters = playwright_tools.SCHEMAS["browser_snapshot"]["inputSchema"]["properties"]
        self.assertIn("target", snapshot_parameters)
        self.assertIn("filename", snapshot_parameters)
        for word in ("browser_find", "target", "filename"):
            self.assertIn(word, result["hint"])
        # A whole snapshot is returned as the bridge sent it.
        self.post.return_value = {"snapshot": '- button "OK" [ref=e1]', "url": "https://a/"}
        self.assertEqual(playwright_tools.browser_snapshot(), self.post.return_value)

    def test_find_searches_the_snapshot_with_paths(self):
        self.post.return_value = {"snapshot": "\n".join([
            "- main [ref=e1]", "  - list [ref=e2]", '    - link "Error log" [ref=e3]',
            '    - link "Home" [ref=e4]', '- button "Retry" [ref=e5]'])}
        found = playwright_tools.browser_find(text="error")
        # Items a snapshot collapses into "… N more similar items" are searched too (#237).
        self.assertEqual(self.posted(), [("/api/snapshot", {"collapse": False})])
        self.assertEqual(found["count"], 1)
        self.assertEqual(found["blocks"][0]["path"], ["- main [ref=e1]", "- list [ref=e2]"])
        self.assertIn('>     - link "Error log" [ref=e3]', found["blocks"][0]["lines"])
        self.assertEqual(playwright_tools.browser_find(regex="Error")["count"], 1)
        self.assertEqual(playwright_tools.browser_find(regex="/error|retry/i")["count"], 2)
        self.assertIn("error", playwright_tools.browser_find(text="a", regex="b"))
        self.assertIn("error", playwright_tools.browser_find(regex="(unclosed"))

    def test_find_merges_blocks_marks_matches_and_skips_urls(self):
        # #323: one block per path, matched lines marked, /url lines searched only for URL queries.
        self.answer_snapshot(
            "- main [ref=e1]", '  - link "Release notes" [ref=e2]', '    - /url: "/notes/releases"',
            '  - link "Docs" [ref=e3]', '    - /url: "/docs/releases"', '  - text "Release 2"',
            '  - text "other"', '  - text "other 2"', '  - text "other 3"', '  - text "Release 3"')
        found = playwright_tools.browser_find(regex="/release/i")
        self.assertEqual(found["count"], 3)
        self.assertEqual(len(found["blocks"]), 1)
        rows = found["blocks"][0]["lines"]
        self.assertEqual([r for r in rows if r.startswith(">")],
                         ['>   - link "Release notes" [ref=e2]', '>   - text "Release 2"', '>   - text "Release 3"'])
        self.assertIn('      - /url: "/notes/releases"', rows, "a matched link keeps its own URL")
        self.assertNotIn('      - /url: "/docs/releases"', rows, "other links' URLs are left out")
        self.assertIn("  …", rows, "a hidden /url line inside a window")
        self.assertNotIn("truncated", found)
        self.assertEqual(playwright_tools.browser_find(text="/docs/")["count"], 1, "a URL query searches /url lines")
        self.assertEqual(playwright_tools._url_query(None, "/Initial release|Release/"), False)
        self.assertEqual(playwright_tools._url_query("Release 2.2", None), False)
        for query in ("https://a.example", "github.com", "en.wikipedia.org/wiki"):
            self.assertEqual(playwright_tools._url_query(query, None), True, query)
        for pattern in ("https?://", "/^https?:/i", r"www\.example", r"\/wiki\/", r"/\/wiki\//", "(?i)/wiki",
                        r"en\.wikipedia\.org"):
            self.assertEqual(playwright_tools._url_query(None, pattern), True, pattern)

    def test_find_shows_a_gap_between_windows_that_do_not_touch(self):
        self.answer_snapshot("- main [ref=e1]", '  - text "Release 1"', *(f'  - text "other {n}"' for n in range(6)),
                             '  - text "Release 2"')
        rows = playwright_tools.browser_find(text="release")["blocks"][0]["lines"]
        self.assertEqual(rows, ['>   - text "Release 1"', '    - text "other 0"', '    - text "other 1"', "  …",
                                '    - text "other 4"', '    - text "other 5"', '>   - text "Release 2"'])

    def test_find_windows_stay_in_their_container_and_are_not_repeated(self):
        # Neighbor lists each get their own lines; a matched list's block already shows its matched items.
        self.answer_snapshot(
            "- main [ref=e1]", "  - list [ref=e2]", '    - text "hit 1"', "  - list [ref=e3]", '    - text "hit 2"',
            '  - list "hit list" [ref=e4]', '    - text "hit 3"', '    - text "hit 4"')
        found = playwright_tools.browser_find(text="hit")
        self.assertEqual(found["count"], 5)
        self.assertEqual([(b["path"][-1], b["lines"]) for b in found["blocks"]], [
            ("- list [ref=e2]", ['>     - text "hit 1"']),
            ("- list [ref=e3]", ['>     - text "hit 2"']),
            ("- main [ref=e1]", ['    - list [ref=e3]', "  …", '>   - list "hit list" [ref=e4]',
                                 '>     - text "hit 3"', '>     - text "hit 4"'])])

    def test_find_with_filename_saves_every_match(self):
        # /url lines are searched for any query in a saved result, as before #323.
        self.answer_snapshot(
            "- main [ref=e1]", '  - link "Release notes" [ref=e2]', '    - /url: "/wiki/Release_notes"')
        with tempfile.TemporaryDirectory() as directory:
            saved = playwright_tools.browser_find(text="release", filename=str(Path(directory, "found.txt")))
            text = Path(saved["saved_to"]).read_text(encoding="utf-8")
        self.assertEqual(saved["count"], 2)
        self.assertEqual(text, '- main [ref=e1]\n- main [ref=e1]\n  - link "Release notes" [ref=e2]\n'
                               '    - /url: "/wiki/Release_notes"\n\n'
                               '- main [ref=e1] > - link "Release notes" [ref=e2]\n- main [ref=e1]\n'
                               '  - link "Release notes" [ref=e2]\n    - /url: "/wiki/Release_notes"')

    def test_find_is_cut_line_by_line_and_says_so(self):
        self.answer_snapshot("- main [ref=e1]", *(
            line for n in range(300) for line in (f'  - text "item {n} {"x" * 62}"', f'  - text "after {n}"',
                                                  f'  - text "after {n} again"', f'  - text "before {n + 1}"')))
        found = playwright_tools.browser_find(text="item")
        self.assertEqual(found["count"], 300)
        self.assertIs(found["truncated"], True)
        self.assertEqual(len(found["blocks"]), 1, "the one block is cut partway")
        rows = found["blocks"][0]["lines"]
        self.assertEqual(found["shown"], sum(row.startswith(">") for row in rows))
        self.assertLess(found["shown"], 300)
        # The cut keeps the last listed match's own context, not the context of the next match.
        self.assertNotIn("before", rows[-1])
        self.assertLessEqual(len(json.dumps(found["blocks"], ensure_ascii=False)), playwright_tools.FIND_LIMIT)
        self.assertIn("filename", found["hint"])

    def test_find_lists_at_least_one_match(self):
        self.answer_snapshot("- main [ref=e1]", '  - text "context"', f'  - text "item {"x" * 20000}"',
                             '  - text "item 2"')
        found = playwright_tools.browser_find(text="item")
        self.assertEqual((found["count"], found["shown"]), (2, 1))
        self.assertEqual(found["blocks"][0]["lines"][-1], f'>   - text "item {"x" * 20000}"')

    def test_evaluate_calls_the_function_and_parses_its_json(self):
        self.post.return_value = {"result": '{"title":"Example"}'}
        self.assertEqual(playwright_tools.browser_evaluate(function="() => ({title: document.title})"),
                         {"result": {"title": "Example"}})
        self.assertEqual(self.posted()[-1], ("/api/js", {"code": "Promise.resolve((() => ({title: document.title}))"
                                                                 "()).then(value => JSON.stringify(value) ?? "
                                                                 "'undefined')"}))
        self.post.return_value = {"result": "undefined"}
        self.assertEqual(playwright_tools.browser_evaluate(function="(el) => el.click()", target="e2"),
                         {"result": "undefined"})
        code, body = self.posted()[-1]
        self.assertIn("((el) => el.click())(__kitsuneElement)", body["code"])
        self.assertEqual(body["selector"], "e2")

    def test_network_requests_are_numbered_and_filtered(self):
        self.post.return_value = {"entries": [
            {"url": "https://a/", "initiator": "navigation", "responseStatus": 200},
            {"url": "https://a/app.js", "initiator": "script", "responseStatus": 200},
            {"url": "https://a/missing.js", "initiator": "script", "responseStatus": 404},
            {"url": "https://a/api/user", "initiator": "fetch", "responseStatus": 200}]}
        listed = playwright_tools.browser_network_requests()
        self.assertEqual(self.posted()[-1], ("/api/capture-network", {"op": "all"}))
        self.assertEqual([(r["index"], r["url"]) for r in listed["requests"]],
                         [(1, "https://a/"), (2, "https://a/missing.js"), (3, "https://a/api/user")])
        self.assertEqual(playwright_tools.browser_network_requests(static=True)["count"], 4)
        filtered = playwright_tools.browser_network_requests(filter="/api/.*user")
        self.assertEqual([r["url"] for r in filtered["requests"]], ["https://a/api/user"])
        self.assertEqual(playwright_tools.browser_network_request(index=1)["url"], "https://a/api/user")
        self.assertIn("error", playwright_tools.browser_network_request(index=2))
        self.assertEqual(playwright_tools.browser_network_request(index=1, part="response-body")["code"],
                         "not_supported")

    def test_wait_for_runs_time_then_text_gone_then_text(self):
        with patch.object(tools, "_sleep") as sleep:
            self.post.return_value = {"ok": True}
            playwright_tools.browser_wait_for(time=60, text="Done", textGone="Loading")
        sleep.assert_called_once_with(30)
        self.assertEqual([body.get("text_gone") or body.get("text") for _, body in self.posted()],
                         ["Loading", "Done"])
        self.assertIn("error", playwright_tools.browser_wait_for())

    def test_fill_form_dispatches_by_field_type(self):
        fields = [{"target": "e1", "name": "Email", "type": "textbox", "value": "a@b"},
                  {"target": "e2", "name": "Agree", "type": "checkbox", "value": "true"},
                  {"target": "#plan", "name": "Plan", "type": "combobox", "value": "Pro"}]
        self.assertEqual(playwright_tools.browser_fill_form(fields=fields)["filled"], ["Email", "Agree", "Plan"])
        self.assertEqual([endpoint for endpoint, _ in self.posted()], ["/api/fill", "/api/check", "/api/select-option"])
        self.assertIs(self.posted()[1][1]["checked"], True)
        self.assertEqual(self.posted()[2][1]["values"], ["Pro"])
        self.post.return_value = {"error": "Element not found", "code": "not_found"}
        failed = playwright_tools.browser_fill_form(fields=fields)
        self.assertEqual((failed["field"], failed["filled"]), ("Email", []))

    def test_fill_form_returns_one_delta_for_all_fields(self):
        # #348: the first field records the delta's baseline, the middle ones take no snapshot,
        # and only the last one waits for the page and returns the delta of every field.
        fields = [{"target": "e1", "name": "Email", "type": "textbox", "value": "a@b"},
                  {"target": "e2", "name": "Agree", "type": "checkbox", "value": "true"},
                  {"target": "#plan", "name": "Plan", "type": "combobox", "value": "Pro"}]
        playwright_tools.browser_fill_form(fields=fields)
        flags = [{key: body[key] for key in ("includeSnapshot", "deltaBaseline", "keepBaseline") if key in body}
                 for _, body in self.posted()]
        self.assertEqual(flags, [{"includeSnapshot": False, "deltaBaseline": True}, {"includeSnapshot": False},
                                 {"keepBaseline": True}])

    def test_fill_form_checks_every_field_before_filling_any(self):
        schema = playwright_tools.SCHEMAS["browser_fill_form"]["inputSchema"]["properties"]["fields"]["items"]
        self.assertEqual(playwright_tools._FIELD_TYPES, set(schema["properties"]["type"]["enum"]))
        valid = {"target": "e1", "name": "Email", "type": "textbox", "value": "a@b"}
        for field, error in (({"target": "e2", "name": "Size", "type": "spinbutton", "value": "3"},
                              "Unknown field type: spinbutton"),
                             ({"name": "Agree", "type": "checkbox", "value": "true"}, "Each field needs a target")):
            with self.subTest(error=error):
                failed = playwright_tools.browser_fill_form(fields=[valid, field])
                self.assertEqual((failed["error"], failed["field"], failed["filled"]), (error, field["name"], []))
                self.assertEqual(self.posted(), [])

    def test_select_option_sends_every_value(self):
        playwright_tools.browser_select_option(target="e7", values=["a", "b"])
        self.assertEqual(self.posted(), [("/api/select-option", {"selector": "", "values": ["a", "b"], "ref": "e7"})])

    def test_console_messages_include_more_severe_levels(self):
        logs = [{"level": level, "message": level} for level in ("debug", "log", "info", "warn", "error")]
        self.post.side_effect = lambda endpoint, body, **_: {"logs": logs} if endpoint == "/api/console-logs" else {}
        self.assertEqual([log["level"] for log in playwright_tools.browser_console_messages(level="warning")["logs"]],
                         ["warn", "error"])
        self.assertEqual(playwright_tools.browser_console_messages()["count"], 4)
        self.assertEqual(self.posted()[0], ("/api/watch-console", {"enabled": True}))

    def test_file_upload_fills_the_input_last_clicked(self):
        playwright_tools.browser_click(target="e9")
        playwright_tools.browser_file_upload(paths=["/tmp/a.png"])
        self.assertEqual(self.posted()[-1], ("/api/upload-file", {"paths": ["/tmp/a.png"], "mode": "input",
                                                                  "selector": "e9"}))
        self.assertEqual(playwright_tools.browser_file_upload(), {"ok": True, "cancelled": True})

    def test_drop_sends_files_and_data_as_a_drop(self):
        playwright_tools.browser_drop(target="e5", data={"text/plain": "hello"})
        self.assertEqual(self.posted(), [("/api/upload-file", {"paths": [], "mode": "drop", "selector": "e5",
                                                               "data": {"text/plain": "hello"}})])
        self.assertIn("error", playwright_tools.browser_drop(target="e5"))

    def test_screenshot_formats(self):
        import base64

        jpeg = b"\xff\xd8\xfffake jpeg\xff\xd9"
        self.post.return_value = {"dataUrl": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}
        with tempfile.TemporaryDirectory() as directory:
            for suffix in ("jpg", "jpeg", "JPG"):
                destination = Path(directory, f"shot.{suffix}")
                result = playwright_tools.browser_take_screenshot(filename=str(destination), fullPage=True)
                self.assertEqual(result["saved_to"], str(destination))
                self.assertEqual(destination.read_bytes(), jpeg)
                self.assertEqual(self.posted()[-1], ("/api/full-page-screenshot", {
                    "imagePreview": False, "format": "jpeg"}))
            destination = Path(directory, "explicit.jpg")
            playwright_tools.browser_take_screenshot(type="jpeg", filename=str(destination), target="e1")
            self.assertEqual(self.posted()[-1], ("/api/element-screenshot", {
                "selector": "", "ref": "e1", "imagePreview": False, "format": "jpeg"}))
            self.assertEqual(destination.read_bytes(), jpeg)
        image, _ = playwright_tools.browser_take_screenshot(type="jpeg")
        self.assertEqual(image, tools.Image(jpeg, "jpeg"))
        # The MCP server sends it as image content of its format.
        self.assertEqual(server._mcp_errors(lambda: [image, {}])()[0].to_image_content().mimeType, "image/jpeg")
        self.assertEqual(self.posted()[-1], ("/api/screenshot", {"imagePreview": True, "format": "jpeg"}))
        self.post.reset_mock()
        self.assertIn("error", playwright_tools.browser_take_screenshot(target="e1", fullPage=True))
        for options in ({"type": "webp"}, {"filename": "shot.webp"}, {"type": "gif"}):
            self.assertEqual(playwright_tools.browser_take_screenshot(**options)["code"], "not_supported")
        self.post.assert_not_called()

    def test_screenshot_png_default_and_explicit_type_precedence(self):
        with patch.object(tools, "browser_screenshot", return_value={"ok": True}) as screenshot:
            for options, image_type in (({}, "png"), ({"type": "jpeg"}, "jpeg"),
                                        ({"type": "png", "filename": "shot.jpg"}, "png")):
                playwright_tools.browser_take_screenshot(**options)
                self.assertEqual(screenshot.call_args.kwargs["format"], image_type)

    def test_resize_targets_the_current_tab_window(self):
        playwright_tools._current_tab = 4
        playwright_tools.browser_resize(width=800, height=600)
        self.assertEqual(self.posted(), [("/api/resize-window", {"width": 800, "height": 600, "tabId": 4})])

    def test_tools_kitsune_cannot_back_answer_not_supported(self):
        for result in (playwright_tools.browser_emulate_media(colorScheme="dark"),
                       playwright_tools.browser_run_code_unsafe(code="async (page) => 1")):
            self.assertEqual((result["code"], result["retryable"]), ("not_supported", False))
        self.post.assert_not_called()

    def test_mcp_calls_take_playwright_arguments(self):
        playwright_tools._current_tab = 4
        result = asyncio.run(server.mcp.call_tool("browser_click", {"target": "e1", "button": "middle"}))
        self.assertFalse(result.isError)
        self.assertEqual(self.posted(), [("/api/click", {"selector": "", "button": "middle", "ref": "e1",
                                                         "tabId": 4})])

    def test_navigate_and_back_take_an_unpublished_tab_id(self):
        # Like confirm_token, tab_id is accepted but not in Playwright's published schema (#303).
        playwright_tools._current_tab = 4
        playwright_tools.browser_navigate(url="https://a.test/")
        playwright_tools.browser_navigate(url="https://a.test/", tab_id=9)
        playwright_tools.browser_navigate_back()
        playwright_tools.browser_navigate_back(tab_id=9)
        self.assertEqual([(endpoint, body.get("tabId")) for endpoint, body in self.posted()],
                         [("/api/navigate", 4), ("/api/navigate", 9), ("/api/back", 4), ("/api/back", 9)])
        for name in ("browser_navigate", "browser_navigate_back"):
            self.assertNotIn("tab_id", playwright_tools.SCHEMAS[name]["inputSchema"]["properties"])

    def test_press_key_takes_an_unpublished_repeat(self):
        # Like tab_id on navigate, repeat is accepted but not in Playwright's published schema (#311);
        # the extension presses the key repeat times within one request.
        playwright_tools._current_tab = 4
        playwright_tools.browser_press_key(key="ArrowLeft", repeat=7)
        playwright_tools.browser_press_key(key="Shift+ArrowRight", repeat=100)
        playwright_tools.browser_press_key(key="ArrowLeft", repeat=1)
        self.assertEqual(self.posted(), [
            ("/api/keypress", {"key": "ArrowLeft", "repeat": 7, "tabId": 4}),
            ("/api/keypress", {"key": "ArrowRight", "modifiers": {"shift": True}, "repeat": 100, "tabId": 4}),
            ("/api/keypress", {"key": "ArrowLeft", "tabId": 4}),
        ])
        self.post.reset_mock()
        for repeat in (0, 101, -1, True, 2.5):
            with self.subTest(repeat=repeat):
                self.assertEqual(playwright_tools.browser_press_key(key="ArrowLeft", repeat=repeat),
                                 {"error": "repeat must be an integer from 1 to 100"})
        self.post.assert_not_called()
        self.assertNotIn("repeat", playwright_tools.SCHEMAS["browser_press_key"]["inputSchema"]["properties"])
        self.post.return_value = {"ok": True, "key": "ArrowDown", "repeat": 3}
        result = asyncio.run(server.mcp.call_tool("browser_press_key", {"key": "ArrowDown", "repeat": 3}))
        self.assertFalse(result.isError)
        self.assertEqual(self.posted(), [("/api/keypress", {"key": "ArrowDown", "repeat": 3, "tabId": 4})])

    def test_press_key_repeat_on_an_older_extension_is_an_error(self):
        # An extension older than repeat presses once and answers without repeat.
        playwright_tools._current_tab = 4
        self.post.return_value = {"ok": True, "key": "ArrowLeft", "effect": None}
        result = playwright_tools.browser_press_key(key="ArrowLeft", repeat=7)
        self.assertEqual((result["code"], result["retryable"], result["state"]),
                         ("extension_version_mismatch", False, {"reason": "repeat_unsupported", "pressed": 1}))
        self.assertEqual(playwright_tools.browser_press_key(key="ArrowLeft"), self.post.return_value)
        self.post.return_value = {"error": "No target", "code": "not_found"}
        self.assertEqual(playwright_tools.browser_press_key(key="ArrowLeft", repeat=7)["code"], "not_found")

    def test_fill_form_slider_and_type_fill_the_same_way(self):
        # Routing guard: both reach /api/fill, where the extension steps an ARIA slider with keys (#311).
        playwright_tools.browser_fill_form(fields=[{"target": "e4", "name": "Volume", "type": "slider",
                                                    "value": 15}])
        playwright_tools.browser_type(target="e4", text="15")
        self.assertEqual(self.posted(), [("/api/fill", {"selector": "", "value": "15", "ref": "e4",
                                                        "element": "Volume"}),
                                         ("/api/fill", {"selector": "", "value": "15", "ref": "e4"})])


class BridgeDropTests(unittest.TestCase):
    def upload(self, body):
        sent = []

        async def send(action, params, timeout=None):
            sent.append((action, params))
            return {"ok": True}

        with patch.object(bridge, "send_to_extension", send):
            return asyncio.run(bridge._upload_files_async(body)), sent

    def test_data_alone_is_dropped_without_chunks(self):
        result, sent = self.upload({"paths": [], "mode": "drop", "selector": "e5", "tabId": 3,
                                    "data": {"text/plain": "hello"}})
        self.assertTrue(result["ok"])
        self.assertEqual([action for action, _ in sent], ["uploadCommit"])
        self.assertEqual(sent[0][1]["data"], {"text/plain": "hello"})
        self.assertEqual(sent[0][1]["files"], [])

    def test_data_must_be_strings_and_needs_a_drop(self):
        self.assertIn("error", self.upload({"mode": "drop", "tabId": 3, "data": {"text/plain": 1}})[0])
        self.assertEqual(self.upload({"mode": "input", "tabId": 3, "data": {"text/plain": "x"}})[0],
                         {"error": "path or paths is required"})

    def test_dropped_data_is_redacted_in_the_audit_log(self):
        self.assertEqual(bridge._audit_params("uploadFile", {"data": {"text/plain": "secret"}})["data"],
                         bridge.AUDIT_REDACTED)


if __name__ == "__main__":
    unittest.main()
