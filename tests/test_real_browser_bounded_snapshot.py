"""Issue #262: bounded core snapshots of a long article and a product list.

Opt-in through KITSUNE_E2E_BROWSER; uses only the isolated real_browser_harness profile.
"""
import asyncio
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from kitsune import playwright_tools, server, tools

import real_browser_harness as harness

CARDS = 80
SECTIONS = 24


def _card(i):
    # Heterogeneous cards: a discount on odd ones, a badge on every third, colour swatches on
    # every fourth; consecutive cards differ, so sibling collapsing lists them all.
    discount = f'<span class="discount">Discount {i}: -{10 + i % 30}%</span>' if i % 2 else ""
    badge = f'<p class="badge">New {i}</p>' if i % 3 == 0 else ""
    swatches = (f'<div class="swatches"><button aria-label="Oak {i}"></button>'
                f'<button aria-label="White {i}"></button></div>' if i % 4 == 0 else "")
    return (f'<li><h3><a href="/p/desk-{i}">DESK-{i} desk</a></h3>'
            f'<p>Variant {i}: oak veneer, 120x60 cm</p>'
            f'<p><span class="price">Price {i}: ₩{129 + i},000</span> {discount}</p>{badge}{swatches}'
            f'<button onclick="document.title = this.textContent">Add DESK-{i} to cart</button></li>')


PRODUCTS = ("<!doctype html><meta charset=utf-8><title>Desks</title>"
            "<nav><a href=/>Home</a> <a href=/desks>Desks</a> <a href=/cart>Cart</a></nav>"
            "<main><h1>Search results for desk</h1><ul>"
            + "".join(_card(i) for i in range(CARDS)) + "</ul></main>")


def _section(s):
    paragraphs = "".join(
        f'<p>Section {s} paragraph {p}: the city <a href="/wiki/topic-{s}-{p}">topic {s}.{p}</a> '
        + "grew around its harbour and markets. " * 6 + "</p>" for p in range(5))
    # Flat like a Wikipedia article: a heading, then its paragraphs as siblings.
    return f'<div class="mw-heading"><h2>History part {s}</h2></div>{paragraphs}'


ARTICLE = ("<!doctype html><meta charset=utf-8><title>Fukuoka</title><main><article><h1>Fukuoka</h1>"
           + "".join(_section(s) for s in range(SECTIONS)) + "</article></main>")


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        data = (ARTICLE if self.path == "/article" else PRODUCTS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _cards(snapshot):
    """{card number: its lines}; fails when a card's line follows another card's name."""
    cards, current = {}, None
    for line in snapshot.split("\n"):
        number = re.search(r"(?:DESK-|desk-|Variant |Price |Discount |New |Oak |White )(\d+)", line)
        # A shortened URL (#310) belongs to the card it follows.
        if not number and line.strip() != '- /url: "…"':
            continue
        i = int(number[1]) if number else current
        if f'heading "DESK-{i} desk"' in line:
            current = i
        if i != current:
            raise AssertionError(f"a line of card {i} under card {current}:\n{snapshot}")
        cards.setdefault(i, []).append(line.strip())
    return cards


class RealBrowserBoundedSnapshotTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Site)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def setUp(self):
        playwright_tools._current_tab = None

    def core(self, name, **arguments):
        """A core tool's JSON result through FastMCP, against this class's bridge."""
        with patch.object(tools, "_post", lambda endpoint, data, timeout=15: self.call(endpoint, data)):
            result = asyncio.run(server.mcp.call_tool(name, arguments))
        self.assertFalse(result.isError, result.content)
        return json.loads(result.content[0].text)

    def assert_whole_card(self, lines, i, url=None, omit_url=False):
        # Same-origin link URLs are paths (#310); a cut snapshot without a target has none (#321).
        expected = [f'heading "DESK-{i} desk"', f'link "DESK-{i} desk"',
                    *([] if omit_url else [url or f'/url: "/p/desk-{i}"']),
                    f'text "Variant {i}:', f'text "Price {i}: ₩{129 + i},000' + (f' Discount {i}:' if i % 2 else '"'),
                    *([f'text "New {i}"'] if i % 3 == 0 else []),
                    *([f'button "Oak {i}"', f'button "White {i}"'] if i % 4 == 0 else []),
                    f'button "Add DESK-{i} to cart"']
        self.assertEqual(len(lines), len(expected), lines)
        for line, part in zip(lines, expected):
            self.assertIn(part, line)

    def test_product_list_is_cut_between_cards_and_later_cards_stay_reachable(self):
        tab = self.open_tab(self.site.origin + "/products")
        playwright_tools._current_tab = tab
        whole = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        self.assertGreater(len(whole), playwright_tools.SNAPSHOT_LIMIT * 3 // 2)
        self.assertEqual(len(_cards(whole)), CARDS)

        bounded = self.core("browser_snapshot")
        snapshot = bounded["snapshot"]
        self.assertLessEqual(len(snapshot), playwright_tools.SNAPSHOT_LIMIT)
        self.assertIs(bounded["truncated"], True)
        self.assertEqual(snapshot.split("\n")[-1], "- … truncated; browser_find searches the rest")
        self.assertIn("browser_find", bounded["hint"])
        self.assertNotIn("selector", bounded["hint"])
        self.assertEqual((bounded["url"], bounded["title"]), (self.site.origin + "/products", "Desks"))
        cards = _cards(snapshot)
        self.assertEqual(list(cards), list(range(len(cards))))
        self.assertGreater(len(cards), 10)
        # A cut snapshot without a target lists no link URLs and says so (#321).
        self.assertIs(bounded.get("urls_omitted"), True)
        self.assertIn("lists no link URLs", bounded["hint"])
        self.assertNotIn("/url:", snapshot)
        for i, lines in cards.items():
            self.assert_whole_card(lines, i, omit_url=True)

        # A card past the cut: find it, read its card by the heading's container, act on it.
        found = self.core("browser_find", text="DESK-55 desk")
        heading = next(m["match"] for m in found["matches"] if m["match"].startswith("- heading"))
        ref = re.search(r"\[ref=(e\d+)\]", heading)[1]
        card = self.core("browser_snapshot", target=f"{ref} >> ..")
        self.assertNotIn("truncated", card)
        self.assert_whole_card(_cards(card["snapshot"])[55], 55)
        self.assertEqual(set(_cards(card["snapshot"])), {55})
        button = re.search(r'button "Add DESK-55 to cart" \[ref=(e\d+)\]', card["snapshot"])[1]
        self.assertEqual(self.core("browser_click", target=button)["ok"], True)
        self.assertEqual(self.call("/api/js", {"tabId": tab, "code": "document.title"})["result"],
                         "Add DESK-55 to cart")

        # A saved snapshot is whole.
        with tempfile.TemporaryDirectory() as directory:
            saved = self.core("browser_snapshot", filename=str(Path(directory, "products.yml")))
            text = Path(saved["saved_to"]).read_text(encoding="utf-8")
        self.assertNotIn("truncated", saved)
        self.assertEqual(len(_cards(text)), CARDS)

    def test_long_article_is_cut_between_paragraphs_and_later_sections_stay_reachable(self):
        tab = self.open_tab(self.site.origin + "/article")
        playwright_tools._current_tab = tab
        whole = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        self.assertGreater(len(whole), playwright_tools.SNAPSHOT_LIMIT * 3 // 2)

        bounded = self.core("browser_snapshot")
        snapshot = bounded["snapshot"]
        self.assertLessEqual(len(snapshot), playwright_tools.SNAPSHOT_LIMIT)
        self.assertIs(bounded["truncated"], True)
        # The cut falls after a whole paragraph, its text and link; the cut snapshot lists no URLs (#321).
        self.assertNotIn("/url:", snapshot)
        self.assertIs(bounded.get("urls_omitted"), True)
        self.assertRegex(snapshot.split("\n")[-2], r'- link "topic \d+\.\d+" \[ref=e\d+\]$')
        shown = [(int(s), int(p)) for s, p in re.findall(r'text "Section (\d+) paragraph (\d+):', snapshot)]
        self.assertEqual(shown, [(s, p) for s in range(SECTIONS) for p in range(5)][:len(shown)])
        self.assertGreater(len(shown), 10)
        for s, p in shown[:-1]:
            self.assertIn(f'link "topic {s}.{p}"', snapshot)
        self.assertIn('link "topic {}.{}"'.format(*shown[-1]), snapshot)

        # A paragraph past the cut: browser_find returns it with its link and URL.
        found = self.core("browser_find", text="Section 20 paragraph")
        self.assertEqual(found["count"], 5)
        for p, match in enumerate(found["matches"]):
            self.assertTrue(match["match"].startswith(f'- text "Section 20 paragraph {p}: the city topic 20.{p} grew'))
            context = "\n".join(match["lines"])
            self.assertIn(f'link "topic 20.{p}"', context)
            self.assertIn(f'- /url: "/wiki/topic-20-{p}"', context)
        heading = self.core("browser_find", text="History part 20")["matches"][0]["match"]
        self.assertRegex(heading, r'^- heading "History part 20" \[ref=e\d+\] \[level=2\]$')


if __name__ == "__main__":
    unittest.main()
