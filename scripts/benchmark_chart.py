"""Draw the README benchmark chart, assets/benchmark.png, from the numbers below.

usage: benchmark_chart.py [OUT.png]

Run with Pillow on macOS (SF font), e.g. `uv run --no-project --with pillow python scripts/benchmark_chart.py`.
The numbers come from a kitsune-benchmark report; update RESULTS from the next report, not by hand-tuning.
"""
import sys

from PIL import Image, ImageDraw, ImageFont

# kitsune-benchmark docs/results/2026-10-26-opus-subscription-v0.7.0.md (taskset-v3).
RESULTS = {
    "title": "Benchmark · Kitsune v0.7.0 vs Playwright MCP 0.0.83",
    "subtitle": "Same model, same tasks, same 23-tool allowlist, Firefox. 2026-10-26.",
    "kept_front": {"tasks": 28, "kitsune": 28, "playwright": 2},
    # Claude Code list-price estimate, Kitsune / Playwright MCP, on tasks both solved.
    "cost": {"Personal": 0.837, "Crowded": 0.675, "Isolated": 0.908},
    "success": {"kitsune": {"Personal": 84, "Crowded": 62, "Isolated": 63},
                "playwright": {"Personal": 83, "Crowded": 63, "Isolated": 63},
                "of": {"Personal": 84, "Crowded": 63, "Isolated": 63}},
    "footer": ("Claude Opus 5.5 via Claude Code, one attempt per task. Playwright MCP was not built to share",
               "a browser with a person. Full reports: github.com/junyeong-nero/kitsune-benchmark"),
}

W, H, SCALE = 1200, 795, 2
BG, TEXT, MUTED, DIM = (28, 28, 30), (242, 242, 245), (170, 170, 178), (140, 140, 148)
ORANGE, GREY = (255, 140, 56), (122, 122, 134)
SF = "/System/Library/Fonts/SFNS.ttf"
LEFT, BAR_X, BAR_MAX, BAR_H = 40, 200, 820, 28


WEIGHTS = {"Regular": 400, "Semibold": 650}


def font(size, weight="Regular"):
    f = ImageFont.truetype(SF, size * SCALE)
    # SF's axes: width, optical size (the point size, not the 2x pixels), grade, weight.
    f.set_variation_by_axes([100, min(max(size, 17), 96), 400, WEIGHTS[weight]])
    return f


def main(out="assets/benchmark.png"):
    r = RESULTS
    image = Image.new("RGB", (W * SCALE, H * SCALE), BG)
    draw = ImageDraw.Draw(image)

    def text(x, y, value, size, colour=TEXT, weight="Regular"):
        draw.text((x * SCALE, y * SCALE), value, font=font(size, weight), fill=colour)

    def bar(y, fraction, colour, label, label_colour):
        width = max(BAR_MAX * fraction, BAR_H * 2.4)
        draw.rounded_rectangle((BAR_X * SCALE, y * SCALE, (BAR_X + width) * SCALE, (y + BAR_H) * SCALE),
                               radius=4 * SCALE, fill=colour)
        text(BAR_X + width + 10, y + 4, label, 16, label_colour, "Semibold")

    text(LEFT, 38, r["title"], 26, weight="Semibold")
    text(LEFT, 79, r["subtitle"], 16, MUTED)

    kept = r["kept_front"]
    text(LEFT, 132, "Kept the user's tab in front", 20, weight="Semibold")
    text(LEFT, 161, f"{kept['tasks']} personal tasks while a user is in the browser (higher is better)", 14, MUTED)
    for y, name, colour, label_colour in ((194, "kitsune", ORANGE, ORANGE), (234, "playwright", GREY, DIM)):
        text(LEFT, y + 5, "Kitsune" if name == "kitsune" else "Playwright MCP", 16, MUTED)
        bar(y, kept[name] / kept["tasks"], colour, f"{kept[name]} / {kept['tasks']}", label_colour)

    text(LEFT, 302, "Cost on tasks both solved", 20, weight="Semibold")
    text(LEFT, 331, "Playwright MCP = 1.00 (lower is better)", 14, MUTED)
    rows = [(name, value, ORANGE, ORANGE) for name, value in r["cost"].items()] + [("Playwright", 1.0, GREY, DIM)]
    for i, (name, value, colour, label_colour) in enumerate(rows):
        y = 364 + 40 * i
        text(LEFT, y + 5, name, 16, MUTED)
        bar(y, value, colour, f"{value:.2f}×", label_colour)

    success, of = r["success"], r["success"]["of"]
    total = sum(of.values())
    text(LEFT, 552, "Task success", 20, weight="Semibold")
    text(LEFT, 585, f"Kitsune {sum(success['kitsune'].values())} / {total}   ·   "
                    f"Playwright MCP {sum(success['playwright'].values())} / {total}", 18)
    detail = " · ".join(f"{track} {success['kitsune'][track]} vs {success['playwright'][track]}" for track in of)
    text(LEFT, 616, f"{detail} (out of {' / '.join(str(n) for n in of.values())})", 14, MUTED)

    for i, line in enumerate(r["footer"]):
        text(LEFT, 735 + 22 * i, line, 14, DIM)
    image.resize((W, H), Image.LANCZOS).save(out, optimize=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
