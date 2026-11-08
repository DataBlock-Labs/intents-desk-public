"""Take the app screenshots for the explainer (demo data from tests/fakes.py).

Usage: uv run python video/shots.py
Writes PNGs to video/remotion/public/shots/. No live API and no real swap is used.
"""

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from intents_desk.store import load_explorer_txs
from tests import fakes
from tests.test_e2e import run_app, seed_incidents
from tests.test_incidents import raw_swaps

OUT = Path(__file__).parent / "remotion" / "public" / "shots"
DEMO = "demo-widget:pda"


class Tmp:
    def mktemp(self, name: str) -> Path:
        return Path(tempfile.mkdtemp(prefix=name))


def seed(con) -> None:
    """The e2e incidents, plus a demo incident with evidence and NEAR blocks."""
    os.environ["NEAR_BLOCK_LOOKUP"] = "0"  # blocks come from app.tx_block below
    for i, c in enumerate("abcdefgh"):
        con.execute(
            "INSERT INTO app.tx_block VALUES (?, ?, now())",
            [fakes.NEAR_TX[:-1] + c, fakes.NEAR_BLOCK + i],
        )
    con.execute(  # the demo swap's NEAR step, minutes after its quote
        "INSERT INTO app.tx_block VALUES (?, ?, '2026-11-04 13:54:40+00')",
        [fakes.NEAR_TX, fakes.NEAR_BLOCK],
    )
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    for h in range(1, 9):  # 8 bad hours before the raw one, so the charts have a shape
        con.execute(
            "INSERT INTO ops.hourly_swap_summary (hour, partner, origin_chain,"
            " destination_chain, status, swaps, usd_in, refunded_usd,"
            " top_refund_reason) VALUES (?, ?, 'arb', 'base', 'REFUNDED', ?, 0,"
            " NULL, 'X_FAILED')",
            [hour - timedelta(hours=h), DEMO, 60 + 25 * (8 - h)],
        )
    load_explorer_txs(con, raw_swaps(hour, 60, DEMO))  # the newest hour: read raw
    seed_incidents(con)
    del os.environ["NEAR_BLOCK_LOOKUP"]


def shoot(page, name: str, selector: str) -> None:
    page.locator(selector).screenshot(path=OUT / f"{name}.png")
    print(name)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    app = run_app(Tmp(), seed=seed)
    url, _ = next(app)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": 1600, "height": 900},
                device_scale_factor=2,  # sharp when Remotion zooms in
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = ctx.new_page()
            page.goto(url)
            page.wait_for_load_state("networkidle")
            page.add_style_tag(content="header { position: static !important; }")
            lookup = "section:has(#lookup)"
            page.fill("#addr", fakes.TRON_TX)
            shoot(page, "1-lookup", lookup)
            page.click("#lookup button[type=submit]")
            expect(page.locator("#swap")).to_contain_text("Your funds came back")
            expect(page.locator("#swap")).to_contain_text(f"{fakes.NEAR_BLOCK:,}")
            shoot(page, "2-case", lookup)
            page.get_by_role("button", name="Copy support message").click()
            expect(page.locator("#swap")).to_contain_text("Copied")
            shoot(page, "3-copied", lookup)
            page.reload()
            page.wait_for_load_state("networkidle")
            page.add_style_tag(content="header { position: static !important; }")
            shoot(page, "4-asks", "section:has(#asks)")
            page.locator("#incidents a", has_text=DEMO).click()
            expect(page.locator("#incident-view")).to_be_visible()
            expect(page.locator("#incident-repro-text")).to_contain_text("curl")
            shoot(page, "5-incident", "section:has(#incident-view)")
            browser.close()
    finally:
        next(app, None)  # stops the server and the fake APIs


if __name__ == "__main__":
    main()
