# Recording the README demo

`assets/demo.gif` is a real screen recording: Claude Code in a terminal and a Zen window on a
fresh profile, using the live websites. The scripts in `scripts/demo/` stage that scene, record
it, and edit it into the GIF and an MP4 the way screen-recording editors such as Screen Studio
do: idle stretches are sped up and an eased camera zooms in on what matters.

The edit changes only time and framing. Every frame comes from the recording; nothing is drawn
over the page or the terminal except the caption band and the speed badge below the picture.

## How it works

| Step | Script | What happens |
|---|---|---|
| Stage | `stage.py SCENE up` | Starts a bridge on ports 47761/47762 and a visible Zen on a temporary profile with a developer-build copy of the extension that only reaches that bridge. Your own bridge and browser profiles are untouched |
| | `stage.py SCENE terminal` | Opens Claude Code in a new Ghostty window with only the demo Kitsune server, then fits Zen beside it |
| Record | `take.py SCENE NAME` | Captures the stage with ffmpeg, types the prompt, waits until the agent has opened the scene's tabs and gone quiet, then opens each `review` tab as the user would |
| Sync | `sync.py NAME [TEXT ...]` | Prints the take's timeline in video seconds: typing, every bridge action, and when given text appeared in Claude Code's output |
| Edit | `scenes/SCENE.edit.json` | Camera views, the output-to-source time map and captions, written from that timeline |
| Render | `render.py EDIT TAKE OUT.mp4 --cut full\|short` | Renders the MP4; `--preview DIR` saves one still per camera view first |

The capture and the logs share one clock. AVFoundation stamps frames with `CLOCK_UPTIME_RAW`,
and the take records the offset to wall time, so bridge audit entries and terminal output land
on the right frame without a sync flash.

## Requirements

- macOS with Zen at `/Applications/Zen.app`, Ghostty at `/Applications/Ghostty.app`, Claude Code
  (`claude`), `uv`, `ffmpeg` and `gifski` (`brew install ffmpeg gifski`)
- A checkout with `uv sync` done; the demo MCP server runs `uv run --directory <checkout> kitsune`,
  so the recording shows the checked-out code
- Screen Recording permission for Ghostty (System Settings, Privacy & Security, Screen & System
  Audio Recording). macOS applies it only to Ghostty instances started after you grant it, so the
  scripts capture through a new instance (`runner.py`) instead of the terminal you work in
- A display you will not touch for about three minutes. Turn on Do Not Disturb

## Recording

Set the scene's `display` and `stage` first. `ffmpeg -f avfoundation -list_devices true -i ""`
lists the screens (`Capture screen 0` is device 5 when five cameras come first, and so on);
`origin` is that display's top-left corner in global points and `stage` is the region to record,
below its menu bar. Then:

```bash
cd scripts/demo
export KITSUNE_DEMO_DIR=/tmp/kitsune-demo
../../.venv/bin/python stage.py scenes/tokyo.json runner
../../.venv/bin/python stage.py scenes/tokyo.json up
../../.venv/bin/python stage.py scenes/tokyo.json terminal
../../.venv/bin/python take.py scenes/tokyo.json take1
../../.venv/bin/python sync.py take1 "Cheapest outbound" "Day route"
```

For another take, run `stage.py SCENE reset` and `terminal` again. `stage.py SCENE down` stops
everything, including the runner.

Review a few frames of `$KITSUNE_DEMO_DIR/takes/NAME.mkv` before editing: the recorded region
must show only the demo windows, and nothing private.

## Editing and exporting

```bash
R="uv run --no-project --with pillow --with numpy python render.py scenes/tokyo.edit.json $KITSUNE_DEMO_DIR/takes/take1.mkv"
$R --preview /tmp/preview --cut short
$R /tmp/demo-short.mp4 --cut short
$R /tmp/demo.mp4 --cut full

# README GIF: 800 px, 10 fps, under 5 MB
ffmpeg -i /tmp/demo-short.mp4 -vf "fps=10,scale=800:-1:flags=lanczos" -f yuv4mpegpipe - \
  | gifski --fps 10 --quality 85 --lossy-quality 60 --motion-quality 70 -o ../../assets/demo.gif -

# Full video for the pull request and social posts
ffmpeg -i /tmp/demo.mp4 -c:v libx264 -preset slow -crf 23 -pix_fmt yuv420p -movflags +faststart -an /tmp/kitsune-demo.mp4
```

Keep the GIF under 5 MB. The README links it with an absolute URL so PyPI can show it, and
GitHub's image proxy (Camo) refuses files larger than 5,242,880 bytes. Attach the full video to
the pull request with `gh pr create --attach /tmp/kitsune-demo.mp4` and link the uploaded URL from
the caption. Squash-merge GIF changes so replaced versions do not stay in the history.

## Notes

- The run uses your Claude Code login and plan; each take is one or two minutes of agent work.
- Live sites change. Prices, results and layouts differ between takes, so check the answer on
  screen against the pages before using a take.
- A fresh Zen profile shows an "Update Complete!" card at the bottom of the sidebar for a few
  seconds. Wait until it is gone before `take.py`.
- `setup_clicks` runs on the first tab after it loads. The Tokyo scene clicks Google's English
  link, because Google picks the page language from the visitor's location.
- The agent reads pages with `browser_evaluate` when the developer build allows it; with the
  signed build it uses snapshots. Say which build was used when you publish a recording.
