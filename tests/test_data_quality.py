"""Data assertions: run the same load under two time zones and check the stored data."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intents_desk.store import connect, load_block, load_explorer_txs, purge_api_data
from tests.fakes import explorer_swaps

BLOCK = json.loads(
    (Path(__file__).parent / "fixtures" / "block_intents.json").read_text()
)
KEYS = {
    "bronze.receipt": "receipt_id",
    "bronze.explorer_tx": "deposit_address, deposit_memo",
    "silver.event": "receipt_id, log_index, data_index",
    "silver.token_diff": "intent_hash, token_id",
}


def loaded(tmp_path, tz):
    con = connect(tmp_path / f"{tz.replace('/', '_')}.duckdb")
    con.execute(f"SET TimeZone = '{tz}'")
    load_block(con, BLOCK)
    load_explorer_txs(con, explorer_swaps(datetime.now(UTC)))
    purge_api_data(con)
    return con


def checksum(con) -> str:
    rows = [
        con.execute(f"SELECT to_json(x)::VARCHAR AS j FROM {t} x ORDER BY j").fetchall()
        for t in KEYS
    ]
    return hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_keys_are_unique_and_not_null(tmp_path, tz):
    con = loaded(tmp_path, tz)
    for table, key in KEYS.items():
        dupes = con.execute(
            f"SELECT count(*) FROM (SELECT {key} FROM {table} GROUP BY ALL HAVING count(*) > 1)"
        ).fetchone()[0]
        nulls = con.execute(
            f"SELECT count(*) FROM {table} WHERE {key.split(',')[0]} IS NULL"
        ).fetchone()[0]
        assert (dupes, nulls) == (0, 0), table


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_block_time_matches_the_chain_in_every_zone(tmp_path, tz):
    con = loaded(tmp_path, tz)
    chain_s = int(BLOCK["block"]["header"]["timestamp"]) / 1e9
    for table in ("bronze.receipt", "silver.event"):
        lo, hi = con.execute(
            f"SELECT min(epoch(block_ts)), max(epoch(block_ts)) FROM {table}"
        ).fetchone()
        assert abs(lo - chain_s) < 1 and abs(hi - chain_s) < 1, table


def test_api_data_is_never_older_than_24_hours(tmp_path):
    con = loaded(tmp_path, "UTC")
    hours = con.execute(
        "SELECT max(epoch(now() - least(created_at, fetched_at))) / 3600 FROM bronze.explorer_tx"
    ).fetchone()[0]
    assert hours < 24


def test_reloading_changes_nothing_and_zone_does_not_change_the_data(tmp_path):
    a = loaded(tmp_path, "UTC")
    before = checksum(a)
    load_block(a, BLOCK)  # the same block again
    assert checksum(a) == before
    b = loaded(tmp_path, "America/New_York")
    b.execute("SET TimeZone = 'UTC'")  # compare in one zone
    a_rows = a.execute(
        "SELECT receipt_id, epoch(block_ts) FROM bronze.receipt ORDER BY 1"
    ).fetchall()
    b_rows = b.execute(
        "SELECT receipt_id, epoch(block_ts) FROM bronze.receipt ORDER BY 1"
    ).fetchall()
    assert a_rows == b_rows
