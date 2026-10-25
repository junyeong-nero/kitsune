"""Render a take with an edit: time remap, eased camera, captions in a band below the picture.

usage: render.py EDIT.json TAKE.mkv OUT.mp4 [--cut full|short]
       render.py EDIT.json TAKE.mkv --preview DIR [--cut full|short]   one still per camera view

Run with Pillow and NumPy, e.g. `uv run --no-project --with pillow --with numpy python render.py ...`.
The edit only maps output time to source time and moves a camera; frames are never altered.
"""
import json
import math
import os
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SRC_FPS = 30
PAD = 96              # background around the stage, source px
EXTRA = 80            # margin so a view may overshoot the background a little
OUT_W = 1600
BAND = 76             # caption band below the picture
OUT_FPS = 30
BG = (16, 31, 34)
RADIUS = 24           # macOS window corner radius at 2x
SF = "/System/Library/Fonts/SFNS.ttf"


def font(size, weight="Semibold"):
    f = ImageFont.truetype(SF, size)
    try:
        f.set_variation_by_name(weight)
    except (OSError, ValueError):
        pass
    return f


def ease(x):
    x = min(max(x, 0.0), 1.0)
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


class Edit:
    def __init__(self, path, cut, src_w, src_h):
        data = json.load(open(path))
        self.src_w, self.src_h = src_w, src_h
        self.world_w, self.world_h = src_w + 2 * PAD, src_h + 2 * PAD
        self.content_h = round(OUT_W * self.world_h / self.world_w / 2) * 2
        self.out_h = self.content_h + BAND
        self.aspect = OUT_W / self.content_h
        self.views = {name: ([-PAD, -PAD, self.world_w] if v == "full" else v) for name, v in data["views"].items()}
        c = data["cuts"][cut]
        self.time = [tuple(p) for p in c["time"]]
        self.camera = [tuple(p) for p in c["camera"]]
        self.captions = [tuple(p) for p in c["captions"]]
        self.caption_font = font(c.get("caption_size", 30))
        self.badge_font = font(24)
        self.duration = self.time[-1][0]

    def view_rect(self, name):
        x0, y0, w = self.views[name]
        return x0 + PAD + EXTRA, y0 + PAD + EXTRA, w, w / self.aspect

    def blend(self, a, b, k):
        # Centre moves linearly, width in log space, so zooms feel even.
        (ax, ay, aw, ah), (bx, by, bw, bh) = a, b
        acx, acy, bcx, bcy = ax + aw / 2, ay + ah / 2, bx + bw / 2, by + bh / 2
        w = math.exp(math.log(aw) + (math.log(bw) - math.log(aw)) * k)
        cx, cy = acx + (bcx - acx) * k, acy + (bcy - acy) * k
        return cx - w / 2, cy - w / self.aspect / 2, w, w / self.aspect

    def camera_at(self, t):
        current = self.view_rect(self.camera[0][1])
        for reach, name, dur in self.camera[1:]:
            target = self.view_rect(name)
            if t >= reach:
                current = target
                continue
            if dur and t > reach - dur:
                return self.blend(current, target, ease((t - (reach - dur)) / dur))
            return current
        return current

    def source_time(self, t):
        out, src = zip(*self.time)
        return float(np.interp(t, out, src))

    def speed(self, t, dt=0.25):
        lo = max(t - dt, 0)
        return (self.source_time(t + dt) - self.source_time(lo)) / (t + dt - lo)


def pill(draw, right, top, text, f, fill, fg):
    pad_x, pad_y = 22, 12
    box = draw.textbbox((0, 0), text, font=f)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = right - tw - 2 * pad_x
    draw.rounded_rectangle([x, top, right, top + th + 2 * pad_y], radius=(th + 2 * pad_y) // 2,
                           fill=fill + (205,), outline=(255, 255, 255, 40), width=1)
    draw.text((x + pad_x - box[0], top + pad_y - box[1]), text, font=f, fill=fg + (255,))


class Renderer:
    def __init__(self, edit):
        self.e = edit
        self.mask = Image.new("L", (edit.src_w, edit.src_h), 0)
        ImageDraw.Draw(self.mask).rounded_rectangle([0, 0, edit.src_w - 1, edit.src_h - 1], RADIUS, fill=255)
        self.world = Image.new("RGB", (edit.world_w + 2 * EXTRA, edit.world_h + 2 * EXTRA), BG)
        shadow = Image.new("L", self.world.size, 0)
        o = PAD + EXTRA
        ImageDraw.Draw(shadow).rounded_rectangle([o, o + 18, o + edit.src_w, o + edit.src_h + 18], RADIUS, fill=150)
        self.world.paste((4, 10, 11), (0, 0), shadow.filter(ImageFilter.GaussianBlur(28)))

    def frame(self, stage, t):
        e = self.e
        world = self.world.copy()
        world.paste(stage, (PAD + EXTRA, PAD + EXTRA), self.mask)
        x, y, w, h = e.camera_at(t)
        canvas = Image.new("RGB", (OUT_W, e.out_h), BG)
        canvas.paste(world.resize((OUT_W, e.content_h), Image.LANCZOS, box=(x, y, x + w, y + h)), (0, 0))
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for start, end, text in e.captions:
            if start <= t <= end:
                alpha = max(0.0, min(1.0, (t - start) / 0.3, (end - t) / 0.3))
                box = draw.textbbox((0, 0), text, font=e.caption_font)
                draw.text(((OUT_W - (box[2] - box[0])) / 2 - box[0], e.content_h + (BAND - (box[3] - box[1])) / 2 - box[1]),
                          text, font=e.caption_font, fill=(236, 240, 238, int(255 * alpha)))
        if e.speed(t) > 1.6:
            pill(draw, OUT_W - 28, e.content_h + (BAND - 46) // 2, f"{round(e.speed(t))}\u00d7", e.badge_font,
                 (34, 52, 50), (176, 214, 196))
        return Image.alpha_composite(canvas.convert("RGBA"), layer).convert("RGB")


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", path], capture_output=True, text=True, check=True).stdout
    return tuple(int(v) for v in out.strip().split(","))


def source_frames(path, w, h):
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={SRC_FPS}", "-f", "rawvideo",
                             "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE, bufsize=w * h * 3)
    try:
        while len(data := proc.stdout.read(w * h * 3)) == w * h * 3:
            yield Image.frombuffer("RGB", (w, h), data, "raw", "RGB", 0, 1)
    finally:
        proc.kill()


def render(edit, take, out_path):
    r = Renderer(edit)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                            "-s", f"{OUT_W}x{edit.out_h}", "-r", str(OUT_FPS), "-i", "-", "-c:v", "libx264",
                            "-preset", "slow", "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                            out_path], stdin=subprocess.PIPE)
    frames = source_frames(take, edit.src_w, edit.src_h)
    index, current = -1, None
    for n in range(int(edit.duration * OUT_FPS)):
        t = n / OUT_FPS
        want = int(round(edit.source_time(t) * SRC_FPS))
        while index < want and (nxt := next(frames, None)) is not None:
            current, index = nxt, index + 1
        enc.stdin.write(r.frame(current, t).tobytes())
        if n % 150 == 0:
            print(f"{t:5.1f}s / {edit.duration:.1f}s", flush=True)
    enc.stdin.close()
    enc.wait()


def preview(edit, take, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    r = Renderer(edit)
    picks = {}
    for i, (reach, name, _) in enumerate(edit.camera):
        t = min(reach + 0.4, edit.duration - 0.05)
        picks.setdefault(int(round(edit.source_time(t) * SRC_FPS)), []).append((f"{i:02d}-{name}", t))
    for i, img in enumerate(source_frames(take, edit.src_w, edit.src_h)):
        for label, t in picks.pop(i, []):
            r.frame(img, t).save(f"{out_dir}/{label}.png")
        if not picks:
            break


if __name__ == "__main__":
    args = sys.argv[1:]
    cut = "full"
    if "--cut" in args:
        cut = args.pop(args.index("--cut") + 1)
        args.remove("--cut")
    edit_path, take = args[0], args[1]
    edit = Edit(edit_path, cut, *probe(take))
    if args[2] == "--preview":
        preview(edit, take, args[3])
    else:
        render(edit, take, args[2])
