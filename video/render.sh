#!/usr/bin/env bash
# Render the explainer's Manim scenes (from databolt-spark-videos/render.sh).
#   video/render.sh check    480p15 smoke render, frames go to /tmp/intents-desk-video
#   video/render.sh final    1080p30 clips -> video/remotion/public/clips, and src/clips.json
#   video/render.sh final --no-render   only rewrite src/clips.json
# Manim runs from the conda env in ~/project/databolt-spark-videos/.mmenv
# (manimpango has no Linux wheels, and this box has no root for cairo/pango headers).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV="${MANIM_ENV:-$HOME/project/databolt-spark-videos/.mmenv}"
MEDIA="${MEDIA_DIR:-/tmp/intents-desk-video}"
CLIPS="$ROOT/remotion/public/clips"
[[ -x "$ENV/bin/manim" ]] || { echo "manim not found in $ENV (see video/README.md)"; exit 1; }
export PATH="$ENV/bin:$PATH"
cd "$ROOT/manim"
case "${1:-check}" in
  check) manim render -ql --disable_caching --media_dir "$MEDIA" scenes.py -a ;;
  final)
    [[ "${2:-}" == "--no-render" ]] || manim render -qh --fps 30 --disable_caching --media_dir "$MEDIA" scenes.py -a
    mkdir -p "$CLIPS"
    cp -f "$MEDIA"/videos/scenes/1080p30/*.mp4 "$CLIPS"/
    # clip lengths in frames, for the Remotion timeline
    for f in "$CLIPS"/*.mp4; do
      printf '"%s": %s\n' "$(basename "$f" .mp4)" \
        "$(ffprobe -v error -count_packets -select_streams v:0 -show_entries stream=nb_read_packets -of csv=p=0 "$f")"
    done | paste -sd, | sed 's/^/{/; s/$/}/' > "$ROOT/remotion/src/clips.json"
    cat "$ROOT/remotion/src/clips.json" ;;
  *) echo "usage: $0 {check|final}"; exit 2 ;;
esac
