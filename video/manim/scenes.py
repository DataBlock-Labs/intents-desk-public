"""The three Manim scenes of the explainer (see ../storyboard.md).

Keep the bottom 1.6 units free: Remotion puts the captions there.
"""

import itertools

from theme import *

TOP = 1.6  # y of the content area under the header


class SwapFlow(DeskScene):
    """Scene 2: the steps of a 1Click swap, then the refund branch."""

    def construct(self):
        self.header("How it works", "Where the funds go in a 1Click swap")
        steps = [
            ("Quote", MUTED),
            ("Deposit\norigin chain", BLUE),
            ("Intent\non NEAR", BLUE),
            ("Solver\nfills it", BLUE),
            ("Delivered", GREEN),
        ]
        nodes = VGroup(*[node(s, c, width=2.4, height=1.2) for s, c in steps])
        nodes.arrange(RIGHT, buff=0.6).move_to(UP * 0.9)
        fit_in_frame(nodes)
        links = VGroup(
            *[arrow(a.get_right(), b.get_left()) for a, b in itertools.pairwise(nodes)]
        )
        for n, link in zip(nodes, [*links, None]):
            self.play(FadeIn(n, shift=UP * 0.2), run_time=0.5)
            if link:
                self.play(GrowArrow(link), run_time=0.3)
        ok = status("SUCCESS", GREEN).next_to(nodes[-1], UP, buff=0.25)
        coin = Dot(nodes[0].get_center(), radius=0.12, color=GREEN)
        self.play(FadeIn(coin))
        for n in nodes[1:]:
            self.play(coin.animate.move_to(n.get_center()), run_time=0.6)
        self.play(FadeIn(ok, shift=DOWN * 0.1), FadeOut(coin))
        self.wait(2.5)

        # No solver fills it: the funds go back.
        nofill = text("no solver fills it", 18, RED)
        refund = node("Refund to\nthe sender", YELLOW, width=2.8, height=1.2)
        refund.next_to(nodes[2], DOWN, buff=1.4)
        down = arrow(nodes[2].get_bottom(), refund.get_top(), RED)
        nofill.next_to(down, RIGHT, buff=0.2)
        cross = Cross(VGroup(nodes[3], links[3], nodes[4]), stroke_width=4)
        cross.set_color(RED)
        self.play(FadeOut(ok), Create(cross), run_time=0.6)
        self.play(GrowArrow(down), FadeIn(nofill), run_time=0.6)
        self.play(FadeIn(refund, shift=DOWN * 0.2), run_time=0.5)
        st = status("REFUNDED", YELLOW).next_to(refund, RIGHT, buff=0.3)
        why = text("reason: " + DATA["top_reason"], 18, MUTED).next_to(
            st, DOWN, buff=0.2
        )
        why.align_to(st, LEFT)
        coin = Dot(nodes[1].get_center(), radius=0.12, color=YELLOW)
        self.play(FadeIn(coin))
        self.play(coin.animate.move_to(nodes[2].get_center()), run_time=0.6)
        self.play(coin.animate.move_to(refund.get_center()), run_time=0.8)
        self.play(FadeIn(st), FadeIn(why), FadeOut(coin))
        self.wait(6)


class IncidentHours(DeskScene):
    """Scene 3: per-hour swaps and bad %, as two charts with one axis each."""

    def construct(self):
        self.header("The incident", "Nov 4–5: one partner, 24 hours")
        hours = DATA["hours"]
        n = len(hours)
        width, left = 9.6, -6.6
        step = width / n
        x = [left + step * (i + 0.5) for i in range(n)]

        # Chart 1: swaps per hour (all swaps as an outline, the partner's filled).
        base1, h1 = 0.0, 1.4
        peak = max(h[3] for h in hours)
        all_bars, part_bars = VGroup(), VGroup()
        for xi, (_, s, _p, a, _ap) in zip(x, hours):
            ah, ph = h1 * a / peak, h1 * s / peak
            all_bars.add(
                Rectangle(
                    width=step * 0.7, height=ah, stroke_color=MUTED, stroke_width=1.5
                ).move_to([xi, base1 + ah / 2, 0])
            )
            part_bars.add(
                Rectangle(width=step * 0.7, height=ph, stroke_width=0)
                .set_fill(RED, 0.85)
                .move_to([xi, base1 + ph / 2, 0])
            )
        axis1 = Line([left, base1, 0], [left + width, base1, 0], color=BORDER)
        t1 = text("swaps per hour", 18, MUTED).next_to(axis1, UP, buff=h1 + 0.1)
        t1.align_to(axis1, LEFT)
        p1 = text(f"peak {peak:,}", 16, MUTED).next_to(
            all_bars[hours.index(max(hours, key=lambda h: h[3]))], UP, buff=0.08
        )
        lab_all = (
            text("all swaps", 16, MUTED).next_to(axis1, RIGHT, buff=0.2).shift(UP * 1.1)
        )
        lab_part = text("one partner", 16, RED).next_to(
            lab_all, DOWN, buff=0.15, aligned_edge=LEFT
        )

        # Chart 2: bad % per hour (the partner vs everyone else), the 50% rule.
        base2, h2 = -2.3, 1.6
        others = []
        for _, s, p, a, ap in hours:
            bad_all, bad_part = a * ap / 100, s * p / 100
            others.append(100 * (bad_all - bad_part) / max(a - s, 1))

        def line(vals, color):
            pts = [[xi, base2 + h2 * v / 100, 0] for xi, v in zip(x, vals)]
            return VMobject(stroke_color=color, stroke_width=3).set_points_as_corners(
                pts
            )

        axis2 = Line([left, base2, 0], [left + width, base2, 0], color=BORDER)
        rule = DashedLine(
            [left, base2 + h2 / 2, 0],
            [left + width, base2 + h2 / 2, 0],
            color=YELLOW,
            stroke_width=2,
        )
        rule_t = text("rule: 50% bad", 16, YELLOW).next_to(rule, RIGHT, buff=0.2)
        t2 = text("bad % per hour (refunded + failed)", 18, MUTED).next_to(
            axis2, UP, buff=h2 + 0.1
        )
        t2.align_to(axis2, LEFT)
        part_line, other_line = line([h[2] for h in hours], RED), line(others, GREEN)
        part_t = (
            text(f"one partner {hours[0][2]:.0f}%", 16, RED)
            .next_to(part_line, RIGHT, buff=0.2)
            .shift(UP * 0.25)
        )
        other_t = text(f"everyone else {sum(others) / n:.0f}%", 16, GREEN).next_to(
            other_line, RIGHT, buff=0.2
        )

        x0 = text(hours[0][0][5:10] + " " + hours[0][0][11:16] + " UTC", 14, MUTED)
        x0.next_to(axis2, DOWN, buff=0.12).align_to(axis2, LEFT)
        x1 = text(hours[-1][0][5:10] + " " + hours[-1][0][11:16] + " UTC", 14, MUTED)
        x1.next_to(axis2, DOWN, buff=0.12).align_to(axis2, RIGHT)

        self.play(
            Create(axis1), FadeIn(t1), Create(axis2), FadeIn(t2), FadeIn(x0), FadeIn(x1)
        )
        self.play(
            LaggedStart(*[GrowFromEdge(b, DOWN) for b in all_bars], lag_ratio=0.04),
            FadeIn(lab_all),
            run_time=1.5,
        )
        self.play(
            LaggedStart(*[GrowFromEdge(b, DOWN) for b in part_bars], lag_ratio=0.04),
            FadeIn(lab_part),
            FadeIn(p1),
            run_time=1.5,
        )
        self.wait(1.5)
        self.play(Create(rule), FadeIn(rule_t))
        self.play(Create(other_line), FadeIn(other_t), run_time=1.2)
        self.play(Create(part_line), FadeIn(part_t), run_time=1.2)
        self.wait(1.5)

        # The incident opens in the first bad hour and closes after 2 clean hours.
        span = Rectangle(
            width=x[-2] - x[0] + step, height=3.85, stroke_width=0
        ).set_fill(YELLOW, 0.07)
        span.move_to([(x[0] + x[-2]) / 2, -0.425, 0])
        opened = text("incident open 24 h, closed after 2 clean hours", 14, YELLOW)
        opened.next_to(axis2, DOWN, buff=0.12).set_x(span.get_x())
        self.play(FadeIn(span), FadeIn(opened))
        total = (
            text(
                f"{DATA['swaps']:,} swaps · {100 * DATA['refunded'] / DATA['swaps']:.1f}% refunded"
                f" · {100 * DATA['failed'] / DATA['swaps']:.1f}% failed",
                20,
                TEXT,
                bold=True,
            )
            .to_edge(LEFT, buff=0.5)
            .set_y(2.15)
        )
        self.play(Write(total), run_time=1.2)
        self.wait(9)


class EvidenceChain(DeskScene):
    """Scene 5: every claim links to a public record on chain (demo values)."""

    def construct(self):
        self.header("References", "Every claim has a reference")
        chain = [
            ("Case\nref 7f3a…", MUTED),
            ("Intent hash\n9sQb…Kd", BLUE),
            ("NEAR block\n131,224,001", GREEN),
            ("nearblocks\nexplorer ↗", BLUE),
        ]
        nodes = VGroup(*[node(s, c, width=2.8, height=1.3) for s, c in chain])
        nodes.arrange(RIGHT, buff=0.8).move_to(UP * 0.4)
        fit_in_frame(nodes)
        demo = (
            text("demo swap", 16, MUTED)
            .next_to(nodes, UP, buff=0.4)
            .align_to(nodes, LEFT)
        )
        self.play(FadeIn(demo))
        for i, n in enumerate(nodes):
            self.play(FadeIn(n, shift=RIGHT * 0.2), run_time=0.5)
            if i < len(nodes) - 1:
                self.play(
                    GrowArrow(arrow(n.get_right(), nodes[i + 1].get_left())),
                    run_time=0.3,
                )
        check = text("✓ anyone can check it on chain", 22, GREEN, bold=True)
        check.next_to(nodes, DOWN, buff=0.8)
        self.play(Indicate(nodes[2][0], color=GREEN), run_time=1.0)
        self.play(FadeIn(check, shift=UP * 0.2))
        self.wait(7)
