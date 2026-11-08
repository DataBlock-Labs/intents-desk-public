from pathlib import Path

from intents_desk.report import run, write_partner_report, write_thesis
from intents_desk.store import connect, load_explorer_txs, save_fill

USDT_TRON = "nep141:tron-d28a265909efecdcee7c5028585214ea0b96f015.omft.near"
BTC = "1cs_v1:btc:native:coin"


def swap(i, status, referral, hour=14, usd="100"):
    return {
        "depositAddress": f"addr{i:04d}",
        "createdAt": f"2026-11-04T{hour:02d}:{i % 60:02d}:00Z",
        "status": status,
        "referral": referral,
        "refundTo": "sender-a" if referral.endswith(":pda") else f"user{i}",
        "refundReason": "INTENT_SUBMIT_FAILED" if status == "REFUNDED" else None,
        "originAsset": BTC if i % 2 else USDT_TRON,
        "destinationAsset": USDT_TRON,
        "amountInUsd": usd,
    }


def fixture_db(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    txs = [swap(i, "REFUNDED", "widget-y:pda", hour=15) for i in range(20)]
    txs += [swap(100 + i, "SUCCESS", "partner-good", hour=14) for i in range(30)]
    load_explorer_txs(con, txs)
    save_fill(con, "addr0100", "2026-11-04T14:00:00Z", "2026-11-04T14:02:04Z")
    save_fill(con, "addr0101", "2026-11-04T14:01:00Z", "2026-11-04T14:20:00Z")
    return con


def test_fill_time_is_updated_minus_created(tmp_path):
    con = fixture_db(tmp_path)
    fills = sorted(
        r[0] for r in con.execute("SELECT fill_s FROM gold.fill_sample").fetchall()
    )
    assert fills == [124.0, 1140.0]


def test_every_thesis_query_runs(tmp_path):
    con = fixture_db(tmp_path)
    for name in (
        "01_window",
        "02_by_chain",
        "03_partners",
        "04_concentration",
        "05_hourly",
        "06_reasons",
        "07_incident",
        "08_fill",
        "09_last24h",
    ):
        cols, rows = run(con, name)
        assert cols and rows, name
    _, [incident] = run(con, "07_incident")
    assert incident[0] == "widget-y:pda" and incident[3] == 20 and incident[4] == 100.0
    assert incident[6] == 1  # one sender address
    _, chains = run(con, "02_by_chain")
    assert {r[0] for r in chains} == {"btc", "tron"}


def test_thesis_and_partner_report_write_files(tmp_path):
    con = fixture_db(tmp_path)
    path = write_thesis(con, str(tmp_path / "reports"))
    text = Path(path).read_text()
    assert "20 swaps; 100.0% were refunded" in text
    charts = list((tmp_path / "reports" / "charts").glob("*.svg"))
    assert len(charts) == 3
    assert all("<title>" in c.read_text() for c in charts)  # hover text on marks
    partner = Path(
        write_partner_report(con, "%:pda", str(tmp_path / "reports"))
    ).read_text()
    assert (
        "widget-y:pda" in partner
        and "All other swaps in the same window: 0.0%" in partner
    )
