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
