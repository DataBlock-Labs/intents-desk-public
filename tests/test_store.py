import json
from pathlib import Path

from intents_desk.events import iter_block_receipts, iter_events, iter_token_diffs
from intents_desk.store import connect, load_block, load_tx

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text())


def test_token_diff_is_a_balanced_swap():
    rows = list(iter_token_diffs(iter_events(load("tx_token_diff.json"))))
    intents = {r["intent_hash"] for r in rows}
    assert len(intents) == 2
    # Each side of the swap gives one token and gets the other.
    for intent in intents:
        signs = sorted(int(r["amount"]) > 0 for r in rows if r["intent_hash"] == intent)
        assert signs == [False, True]


def test_tx_reload_is_idempotent(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    tx = load("tx_token_diff.json")
    assert load_tx(con, tx) > 0
    assert load_tx(con, tx) == 0
    assert con.execute("SELECT count(*) FROM silver.token_diff").fetchone()[0] == 4


def test_transfer_has_no_token_diff(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    load_tx(con, load("tx_transfer.json"))
    assert con.execute("SELECT count(*) FROM silver.token_diff").fetchone()[0] == 0
    assert ("transfer",) in con.execute("SELECT event FROM silver.event").fetchall()


def test_block_keeps_only_intents_receipts(tmp_path):
    block = load("block_intents.json")
    receipts = list(iter_block_receipts(block))
    assert receipts
    assert all(r["tx_hash"] for r in receipts)
    con = connect(tmp_path / "t.duckdb")
    first = load_block(con, block)
    assert load_block(con, block) == 0
    assert first > 0
    n = con.execute("SELECT count(*) FROM bronze.receipt").fetchone()[0]
    assert n == len(receipts)
    assert con.execute("SELECT count(*) FROM bronze.block_seen").fetchone()[0] == 1
    # Events from blocks carry the block time.
    nulls = "SELECT count(*) FROM silver.event WHERE block_ts IS NULL"
    assert con.execute(nulls).fetchone()[0] == 0


def explorer_tx(status):
    return {
        "depositAddress": "TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq",
        "depositMemo": None,
        "createdAt": "2026-11-04T14:00:00Z",
        "status": status,
        "originAsset": "nep141:tron-d28a265909efecdcee7c5028585214ea0b96f015.omft.near",
        "amountInUsd": "15000.5",
    }


def test_explorer_swap_keeps_the_newest_status(tmp_path):
    from intents_desk.store import load_explorer_txs

    con = connect(tmp_path / "t.duckdb")
    load_explorer_txs(con, [explorer_tx("PROCESSING")])
    load_explorer_txs(con, [explorer_tx("REFUNDED")])
    rows = con.execute("SELECT status, amount_in_usd FROM silver.swap").fetchall()
    assert rows == [("REFUNDED", 15000.5)]


def test_partner_report_flags_a_failing_partner(tmp_path):
    from intents_desk.store import load_explorer_txs, partner_report

    def swap(i, status, referral):
        return {
            "depositAddress": f"addr{i:04d}",
            "createdAt": "2026-11-04T14:00:00Z",
            "status": status,
            "referral": referral,
            "refundTo": "same-sender",
            "refundReason": "INTENT_SUBMIT_FAILED" if status == "REFUNDED" else None,
            "amountInUsd": "10",
        }

    con = connect(tmp_path / "t.duckdb")
    bad = [swap(i, "REFUNDED", "widget:pda") for i in range(60)]
    good = [swap(100 + i, "SUCCESS", "partner-b") for i in range(60)]
    load_explorer_txs(con, bad + good)
    rows = {r[0]: r for r in partner_report(con, 1, 50, 50).fetchall()}
    assert rows["widget:pda"][2] == 100.0 and rows["widget:pda"][-1] is True
    assert rows["partner-b"][-1] is False
    assert rows["widget:pda"][5] == "INTENT_SUBMIT_FAILED"
