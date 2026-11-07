"""Manim theme for the Intents Desk explainer, in DataBlock tokens.

Taken from ~/project/databolt-spark-videos/manim/databolt_theme.py.
Rules from that series (they cost time to learn):
  * No LaTeX: no Tex, MathTex, Axes number labels or DecimalNumber defaults.
    There is no TeX install. Use Text only.
  * Pass each composed group through fit_in_frame(): Manim renders off-camera content silently.
  * Animate a node's box n[0] and label n[1] separately. set_opacity on the group hides the label.
"""

import json
from pathlib import Path

from manim import *

# src/intents_desk/static/tokens.css
BG = "#0a0a0a"
SURFACE = "#1a1a1a"
BORDER = "#404040"
TEXT = "#e0e0e0"
MUTED = "#a0a0a0"
GREEN = "#00ff88"
BLUE = "#00aaff"
YELLOW = "#ffcc00"
RED = "#ff4444"
FONT = "DejaVu Sans Mono"  # installed here; JetBrains Mono is not

config.background_color = BG
DATA = json.loads((Path(__file__).parent.parent / "data" / "incident.json").read_text())


def text(s: str, size: int = 22, color: str = TEXT, bold: bool = False) -> Text:
    return Text(
        s, font=FONT, font_size=size, color=color, weight=BOLD if bold else NORMAL
    )


def node(
    s: str, color: str = BLUE, width: float = 2.6, height: float = 1.0, size: int = 20
):
    """A square-cornered box with a centred label (radius 0, as the app)."""
    box = Rectangle(width=width, height=height, color=color, stroke_width=2.5)
    box.set_fill(color, opacity=0.12)
    label = text(s, size).move_to(box)
    if label.width > width - 0.3:
        label.scale_to_fit_width(width - 0.3)
    return VGroup(box, label)


def status(word: str, color: str, size: int = 18) -> VGroup:
    """A status word on a coloured rule: colour is never the only signal."""
    t = text(word, size, color, bold=True)
    box = Rectangle(
        width=t.width + 0.3, height=t.height + 0.2, color=color, stroke_width=2
    )
    return VGroup(box.move_to(t), t)


def arrow(a, b, color: str = MUTED) -> Arrow:
    return Arrow(
        a, b, color=color, buff=0.1, stroke_width=3, max_tip_length_to_length_ratio=0.15
    )


def grid() -> VGroup:
    """The faint 48 px grid of the app background (48 px = 0.4 units at 1080p)."""
    step = 0.4
    w, h = config.frame_width / 2, config.frame_height / 2
    lines = [Line([x, -h, 0], [x, h, 0]) for x in np.arange(-w, w, step)]
    lines += [Line([-w, y, 0], [w, y, 0]) for y in np.arange(-h, h, step)]
    return VGroup(*lines).set_stroke("#ffffff", width=1, opacity=0.03)


def fit_in_frame(m: Mobject, margin: float = 0.4) -> Mobject:
    max_w, max_h = config.frame_width - 2 * margin, config.frame_height - 2 * margin
    if m.width > max_w:
        m.scale_to_fit_width(max_w)
    if m.height > max_h:
        m.scale_to_fit_height(max_h)
    return m


class DeskScene(Scene):
    """The grid background and a kicker + heading at the top."""

    def setup(self):
        self.add(grid())

    def header(self, kicker: str, heading: str) -> VGroup:
        k = text("// " + kicker.upper(), 18, GREEN, bold=True).to_corner(UL, buff=0.5)
        h = text(heading, 36, TEXT, bold=True).next_to(
            k, DOWN, buff=0.2, aligned_edge=LEFT
        )
        self.play(FadeIn(k, shift=RIGHT * 0.2), Write(h), run_time=1.0)
        return VGroup(k, h)
