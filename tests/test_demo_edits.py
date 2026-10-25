"""The README demo's scene and edit files (scripts/demo/scenes) stay consistent."""
import json
import unittest
from pathlib import Path

SCENES = Path(__file__).resolve().parents[1] / "scripts" / "demo" / "scenes"


class DemoEditTests(unittest.TestCase):
    def test_edits_are_consistent(self):
        edits = sorted(SCENES.glob("*.edit.json"))
        self.assertTrue(edits)
        for path in edits:
            data = json.loads(path.read_text())
            for name, cut in data["cuts"].items():
                with self.subTest(edit=path.name, cut=name):
                    out, src = zip(*cut["time"])
                    # The time map plays forward: output strictly increases, source never goes back.
                    self.assertEqual(out[0], 0.0)
                    self.assertTrue(all(a < b for a, b in zip(out, out[1:])))
                    self.assertTrue(all(a <= b for a, b in zip(src, src[1:])))
                    duration = out[-1]
                    reach = [c[0] for c in cut["camera"]]
                    self.assertEqual(reach[0], 0.0)
                    self.assertTrue(all(a < b for a, b in zip(reach, reach[1:])))
                    for t, view, transition in cut["camera"]:
                        self.assertIn(view, data["views"])
                        self.assertLessEqual(t, duration)
                        self.assertGreaterEqual(t - transition, 0.0)
                    for start, end, text in cut["captions"]:
                        self.assertTrue(0 <= start < end <= duration, text)

    def test_scenes_name_a_prompt_and_a_stage(self):
        for path in sorted(SCENES.glob("*.json")):
            if path.name.endswith(".edit.json"):
                continue
            with self.subTest(scene=path.name):
                scene = json.loads(path.read_text())
                self.assertTrue(scene["prompt"].strip())
                self.assertEqual(len(scene["stage"]), 4)
                self.assertIn("avfoundation_device", scene["display"])


if __name__ == "__main__":
    unittest.main()
