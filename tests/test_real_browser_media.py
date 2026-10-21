"""Video and audio in a real browser (issue #158): snapshot state, click-to-play and the media command.

Opt-in and built on tests/real_browser_harness.py, which documents how to run it. The
fixture is a muted 4 s, 16x16 VP8 WebM (a red screen, generated with
`ffmpeg -f lavfi -i color=c=red:s=16x16:r=4:d=4 -c:v libvpx -b:v 20k -g 4 -an`),
served next to a page that shows it with native controls. tests/media_controls.cjs
covers the same rules on a mocked DOM.

Agent tabs open in the background, and Firefox delays playback in a tab until it has been
shown (media.block-autoplay-until-in-foreground), so the test selects the tab explicitly.
"""
import base64
from http.server import BaseHTTPRequestHandler
import io
import re
import time
import unittest
import wave

import real_browser_harness as harness

CLIP = base64.b64decode(
    "GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAQYEU2bdLpNu4tTq4QVSalmU6yBoU27i1Or"
    "hBZUrmtTrIHWTbuMU6uEElTDZ1OsggEyTbuMU6uEHFO7a1OsggPK7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsCrX"
    "sYMPQkBNgIxMYXZmNjMuMS4xMDJXQYxMYXZmNjMuMS4xMDJEiYhAr0AAAAAAABZUrmvXrgEAAAAAAABO14EBc8WITD7XeVHM"
    "IoKcgQAitZyDdW5kiIEAhoVWX1ZQOIOBASPjg4QO5rKA4JCwgRC6gRCagQJVsIRVuYEBVe6BAOwBAAAAAAAAAgAAElTDZ/pz"
    "c59jwIBnyJlFo4dFTkNPREVSRIeMTGF2ZjYzLjEuMTAyc3PVY8CLY8WITD7XeVHMIoJnyKBFo4dFTkNPREVSRIeTTGF2YzYz"
    "LjEuMTAyIGxpYnZweGfIoUWjiERVUkFUSU9ORIeTMDA6MDA6MDQuMDAwMDAwMDAwAB9DtnVCE+eBAKO9gQAAgPACAJ0BKhAA"
    "EAAARwiFhYiFhIgCAgJ1qgP4A/oCDU0YAP79bvP/45k3MMT/jm3/8WE8DijI//FRAKOVgQD6ALEBAAEQEAAYABhYL/QACAAA"
    "o5WBAfQAsQEAARAQABgAGFgv9AAIAACjlYEC7gCxAQABEBAAGAAYWC/0AAgAAKO9gQPogPACAJ0BKhAAEAAARwiFhYiFhIgC"
    "AgJ1qgP4A/oCDU0YAP79bvP/45k3MMT/jm3/8WE8DijI//FRAKOVgQTiALEBAAEQEAAYABhYL/QACAAAo5WBBdwAsQEAARAQ"
    "ABgAGFgv9AAIAACjlYEG1gCxAQABEBAAGAAYWC/0AAgAAKO9gQfQgPACAJ0BKhAAEAAARwiFhYiFhIgCAgJ1qgP4A/oCDU0Y"
    "AP79bvP/45k3MMT/jm3/8WE8DijI//FRAKOVgQjKALEBAAEQEAAYABhYL/QACAAAo5WBCcQAsQEAARAQABgAGFgv9AAIAACj"
    "lYEKvgCxAQABEBAAGAAYWC/0AAgAAKO9gQu4gPACAJ0BKhAAEAAARwiFhYiFhIgCAgJ1qgP4A/oCDU0YAP79bvP/45k3MMT/"
    "jm3/8WE8DijI//FRAKOVgQyyALEBAAEQEAAYABhYL/QACAAAo5WBDawAsQEAARAQABgAGFgv9AAIAACjlYEOpgCxAQABEBAA"
    "GAAYWC/0AAgAABxTu2vJu4+zgQC3iveBAfGCAbHwgQO7kLOCA+i3iveBAfGCAbHwgYe7kbOCB9C3i/eBAfGCAbHwggELu5Gz"
    "ggu4t4v3gQHxggGx8IIBjw==")
PAGE = """<!doctype html><title>Media</title><h1>Clip</h1>
<video id="clip" src="/clip.webm" aria-label="Red screen" controls muted preload="auto" width="160" height="160">
<p>Your browser cannot play this video</p></video>"""
AUDIO_PAGE = """<!doctype html><title>Audio autoplay</title>
<audio src="/audio.wav" aria-label="Audio clip" controls loop preload="auto"></audio>"""
with io.BytesIO() as audio:
    with wave.open(audio, "wb") as wav:
        wav.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x00\x00" * 8000 * 4)
    AUDIO = audio.getvalue()
VIDEO_LINE = re.compile(r'- video "Red screen" \[ref=(\S+)\] \[src="[^"]*/clip\.webm" duration=([\d.]+) '
                        r'currentTime=([\d.]+) paused=(true|false) muted=true controls=true\]')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body, kind = {"/media": (PAGE.encode(), "text/html; charset=utf-8"),
                      "/clip.webm": (CLIP, "video/webm"),
                      "/audio": (AUDIO_PAGE.encode(), "text/html; charset=utf-8"),
                      "/audio.wav": (AUDIO, "audio/wav")}.get(self.path, (None, None))
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RealBrowserMediaTests(harness.RealBrowserTestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = harness.FixtureServer(Handler)
        cls.addClassCleanup(cls.site.stop)
        super().setUpClass()

    def video(self, tab):
        """(ref, duration, currentTime, paused) of the video's snapshot line."""
        snapshot = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
        match = VIDEO_LINE.search(snapshot)
        self.assertIsNotNone(match, snapshot)
        return match[1], float(match[2]), float(match[3]), match[4] == "true"

    def media(self, tab, ref, op, **params):
        result = self.call("/api/batch", {"commands": [{"action": "media", "tabId": tab, "ref": ref, "op": op,
                                                         **params}]})["results"][0]
        return self.assert_ok(result)["media"]

    def test_unmuted_audio_click_is_autoplay_blocked_then_mute_and_play(self):
        tab = self.open_tab(f"{self.site.origin}/audio")
        self.assert_ok(self.call("/api/switch-tab", {"tabId": tab}))
        deadline = time.monotonic() + 5
        while True:
            snapshot = self.call("/api/snapshot", {"tabId": tab})["snapshot"]
            match = re.search(r'- audio "Audio clip" \[ref=(\S+)\] .*duration=4 ', snapshot)
            if match or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        self.assertIsNotNone(match, snapshot)
        ref = match[1]
        clicked = self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": ref}))
        self.assertEqual(clicked["media"]["reason"], "autoplay_blocked", clicked)
        self.assertIs(clicked["media"]["paused"], True, clicked)
        self.assertIs(clicked["media"]["muted"], False, clicked)
        self.assertIn("media command mute then play", clicked["media"]["error"])
        self.assertIs(self.media(tab, ref, "mute")["muted"], True)
        state = self.media(tab, ref, "play")
        self.assertIs(state["paused"], False, state)
        self.assertIs(state["muted"], True, state)
        self.media(tab, ref, "pause")

    def test_muted_video_is_listed_and_plays_pauses_and_seeks(self):
        tab = self.open_tab(f"{self.site.origin}/media")
        deadline = time.monotonic() + 10
        while (state := self.video(tab))[1] != 4 and time.monotonic() < deadline:
            time.sleep(0.1)  # duration is known once metadata loaded
        ref, duration, current, paused = state
        self.assertEqual((duration, current, paused), (4, 0, True))

        clicked = self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": ref}))
        self.assertEqual((clicked["media"]["reason"], clicked["media"]["paused"]), ("background_tab", True), clicked)
        self.assert_ok(self.call("/api/switch-tab", {"tabId": tab}))

        # A click on the video plays it through the media API; headless, without user activation.
        ref = self.video(tab)[0]
        clicked = self.assert_ok(self.call("/api/click", {"tabId": tab, "ref": ref}))
        self.assertIs(clicked["media"]["paused"], False, clicked)
        self.assertNotIn("error", clicked["media"])
        time.sleep(1)
        ref, _, current, paused = self.video(tab)
        self.assertIs(paused, False)
        self.assertGreater(current, 0.3, "currentTime advances while playing")

        state = self.media(tab, ref, "pause")
        self.assertIs(state["paused"], True)
        time.sleep(0.5)
        self.assertEqual(self.video(tab)[2:], (state["currentTime"], True), "paused playback stays put")

        state = self.media(tab, self.video(tab)[0], "seek", time=3)
        self.assertEqual((state["currentTime"], state["paused"]), (3, True))


if __name__ == "__main__":
    unittest.main()
