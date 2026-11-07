# Explainer video

The 2-minute Intents Desk explainer is code. Output: `docs/video/` (see [docs/explainer.md](../docs/explainer.md)).

- **`storyboard.md`:** the scenes and captions. Captions also live in `remotion/src/timeline.ts`; keep the two the same.
- **`export_data.py`:** writes `data/incident.json`, the masked totals of the Nov 4–5 incident.
- **`manim/`:** the 3 concept scenes (`theme.py` has the DataBlock tokens and the Manim traps).
- **`shots.py`:** app screenshots, from demo data in `tests/fakes.py`.
- **`remotion/`:** puts it all together, with captions.

## Prerequisites

- The Manim conda env at `~/project/databolt-spark-videos/.mmenv`, or set `MANIM_ENV`. Manim needs conda here: `manimpango` has no Linux wheels.
- Node 22 and ffmpeg on PATH. Run `uv sync` in the repo for Playwright.

## Build

```bash
cd /home/edwar/project/DataBlock/intents-desk
# 1. Data (only when the incident changes). Use a copy of the live database.
cp data/desk-live.duckdb /tmp/desk-copy.duckdb
MASK_PARTNERS=1 uv run python video/export_data.py /tmp/desk-copy.duckdb 2026-11-04
# 2. Manim clips
/home/edwar/project/DataBlock/intents-desk/video/render.sh check   # fast test render
/home/edwar/project/DataBlock/intents-desk/video/render.sh final
# 3. Screenshots
PYTHONPATH=. uv run python video/shots.py
# 4. Preview, then render
cd /home/edwar/project/DataBlock/intents-desk/video/remotion
npm install
npx remotion studio                       # preview in the browser
npm run render                            # -> out/explainer.mp4
# 5. Publish to docs/video
ffmpeg -y -i out/explainer.mp4 -an -c:v libx264 -crf 28 -preset slow -pix_fmt yuv420p \
  -movflags +faststart ../../docs/video/explainer.mp4
node --experimental-strip-types srt.mts > ../../docs/video/explainer.srt
```

## Rules

- **No partner name, no user address and no key** in any frame or file. The data export runs with `MASK_PARTNERS=1`; the screenshots use demo data only.
- **Look at the frames.** A render that exits cleanly can still have overlapping text. Check with `npx remotion still src/index.ts Explainer /tmp/f.png --frame=<n>`.
