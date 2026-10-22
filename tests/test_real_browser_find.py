"""Issue #152 in a real browser: browser_find matches query terms, not every interactive element.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it.
tests/find_query_scoring.cjs checks the scoring on a mocked DOM; here one page with a
header nav above a filter bar checks that descriptive queries find their control first,
that the header never fills the results, and that a nonsense query returns nothing.
Issue #160: on a crowded page browser_dom is capped with truncated=true, and a scoped
read returns only its subtree.
"""
from http.server import BaseHTTPRequestHandler
import json
import unittest

import real_browser_harness as harness

SECRET = "KITSUNE_FAKE_PASSWORD_000"
NAV = ["Sign in", "Store", "Home", "Discovery Queue", "Wishlist", "Points Shop", "News", "Stats"]

PAGE = """<!doctype html><title>Reviews</title>
<header><nav>%s</nav></header>
<main>
<h2>Customer reviews</h2>
<div class="filters">
<button id="review-type" aria-haspopup="listbox">Review Type</button>
<button id="playtime">Playtime</button>
<button id="filter-ko">리뷰 유형</button>
<label for="sort">Sort by</label><select id="sort"><option>Most helpful</option><option>Recent</option></select>
</div>
<p id="summary">Showing 1,234 reviews that match the filters above</p>
<label for="pw">Password</label><input id="pw" type="password">
</main>
<script>document.getElementById('pw').value = %s;</script>""" % (
    "".join(f'<a href="#">{text}</a>' for text in NAV), json.dumps(SECRET))

# A sticky-header page like the kitsune-benchmark gallery: hundreds of links before the content.
CROWDED_PAGE = """<!doctype html><title>Crowded</title>
<header><nav>%s</nav></header>
<main><section id="target"><h2>Target section</h2><button>Target action</button></section></main>""" % (
    "".join(f'<a href="#l{i}">Header link {i} with a longer label</a>' for i in range(800)))


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (CROWDED_PAGE if self.path == "/crowded" else PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserFindTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(_Page)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def find(self, tab_id, query):
        return self.call("/api/find", {"query": query, "tabId": tab_id})

    def test_descriptive_queries_find_their_control_and_nonsense_finds_nothing(self):
        tab_id = self.open_tab(self.site.origin + "/reviews")
        for query, selector in (
            ("'Review Type' dropdown button in Customer reviews filters", "#review-type"),
            ("리뷰 유형 드롭다운", "#filter-ko"),
            ("sort dropdown", "#sort"),
            ("text Showing N reviews that match the filters", "#summary"),
        ):
            with self.subTest(query=query):
                results = self.find(tab_id, query)["results"]
                self.assertEqual(results[0]["selector"], selector, results)
                self.assertFalse([r for r in results if r["text"] in NAV], results)

        for query in ("xyzzy frobnicate", "xyzzy button"):
            with self.subTest(query=query):
                found = self.find(tab_id, query)
                self.assertEqual(found["results"], [], found)
                self.assertIn("No element matches", found["hint"])

        # The password's value is never matched: its words find the field only by its
        # label, exactly as a decoy with the same label does.
        secret = self.find(tab_id, SECRET)
        decoy = self.find(tab_id, SECRET.replace("FAKE", "DECOY"))
        self.assertNotIn(SECRET, json.dumps(secret))
        summary = lambda found: [(r["selector"], r["text"], r["score"]) for r in found["results"]]
        self.assertEqual(summary(secret), summary(decoy))

    def test_dom_on_a_crowded_page_is_capped_and_scoped_reads_return_the_subtree(self):
        tab_id = self.open_tab(self.site.origin + "/crowded")
        full = self.call("/api/dom", {"tabId": tab_id})
        self.assertTrue(full.get("truncated"), {key: full[key] for key in full if key != "tree"})
        self.assertIn("selector, ref or max_depth", full["hint"])
        self.assertLessEqual(len(json.dumps(full["tree"], separators=(",", ":"))), 20000)
        self.assertIn("Header link 0", json.dumps(full["tree"]))
        self.assertNotIn("Target action", json.dumps(full["tree"]))

        scoped = self.call("/api/dom", {"tabId": tab_id, "selector": "#target"})
        self.assertNotIn("truncated", scoped)
        self.assertEqual(scoped["tree"]["tag"], "section")
        self.assertEqual([node["text"] for node in scoped["tree"]["children"]],
                         ["Target section", "Target action"])
        self.assertNotIn("Header link", json.dumps(scoped))

        by_ref = self.call("/api/dom", {"tabId": tab_id, "ref": scoped["tree"]["ref"], "maxDepth": 0})
        self.assertEqual(by_ref["tree"]["tag"], "section")
        self.assertNotIn("children", by_ref["tree"])


if __name__ == "__main__":
    unittest.main()
