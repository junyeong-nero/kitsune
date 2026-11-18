"""A repeated script snapshot of one scope shows only its changes; the str stays the whole tree."""
import unittest

from kitsune import script_runner
from kitsune.browser import Snapshot

TREE = "\n".join(f'- button "Item {n}" [ref=e{n}]' for n in range(40))


class Client:
    def __init__(self, trees):
        self.trees = list(trees)

    def request(self, method, route, **kwargs):
        return {"snapshot": self.trees.pop(0)} if route == "/api/snapshot" else {"ok": True}


class SnapshotDiffTests(unittest.IsolatedAsyncioTestCase):
    async def run_code(self, code, client):
        result = await script_runner.run_python(code, client, tab_id=7)
        self.assertTrue(result["ok"], result)
        return result

    async def test_first_full_then_unchanged_then_changed_lines(self):
        changed = TREE.replace('"Item 5"', '"Item 5 (saved)"')
        client = Client([TREE, TREE, changed])
        first = await self.run_code("result = tab.snapshot()", client)
        self.assertEqual(first["result"], TREE)
        same = await self.run_code("print(tab.snapshot())", client)
        self.assertIn("unchanged", same["output"])
        diff = await self.run_code("s = tab.snapshot()\nresult = {'shown': s, 'whole': s.full == s, 'n': len(s.splitlines())}",
                                   client)
        self.assertIn('+- button "Item 5 (saved)" [ref=e5]', diff["result"]["shown"])
        self.assertIn('-- button "Item 5" [ref=e5]', diff["result"]["shown"])
        self.assertLess(len(diff["result"]["shown"]), len(TREE) // 2)
        self.assertEqual((diff["result"]["whole"], diff["result"]["n"]), (True, 40))

    async def test_scopes_and_clients_are_separate_and_large_changes_show_the_tree(self):
        client = Client([TREE, TREE, "- heading \"Other page\" [ref=e1]"])
        await self.run_code("result = tab.snapshot()", client)
        scoped = await self.run_code("result = tab.snapshot('e2')", client)
        self.assertEqual(scoped["result"], TREE)  # another scope: no previous one
        replaced = await self.run_code("result = tab.snapshot()", client)
        self.assertEqual(replaced["result"], '- heading "Other page" [ref=e1]')
        other = await self.run_code("result = tab.snapshot()", Client([TREE]))
        self.assertEqual(other["result"], TREE)

    def test_snapshot_without_previous_is_a_plain_tree(self):
        snapshot = Snapshot(TREE)
        self.assertIsNone(snapshot.diff)
        self.assertEqual(str(snapshot), TREE)


if __name__ == "__main__":
    unittest.main()
