"""DuckDB storage: bronze keeps raw responses, silver keeps parsed rows.

Every insert is keyed, so a re-run over the same window changes nothing.
"""

import json
from pathlib import Path

import duckdb

from .events import iter_block_receipts, iter_events, iter_token_diffs, parse_logs

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE TABLE IF NOT EXISTS bronze.receipt (
    receipt_id TEXT PRIMARY KEY,
    tx_hash TEXT,
    block_height BIGINT,
    block_ts TIMESTAMPTZ,
    predecessor_id TEXT,
    fetched_at TIMESTAMPTZ DEFAULT now(),
    raw JSON NOT NULL
);
CREATE TABLE IF NOT EXISTS bronze.block_seen (
    block_height BIGINT PRIMARY KEY,
    fetched_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS silver.event (
    receipt_id TEXT,
    log_index INTEGER,
    data_index INTEGER,
    tx_hash TEXT,
    block_height BIGINT,
    block_ts TIMESTAMPTZ,
    standard TEXT NOT NULL,
    event TEXT NOT NULL,
    data JSON NOT NULL,
    PRIMARY KEY (receipt_id, log_index, data_index)
);
CREATE TABLE IF NOT EXISTS silver.token_diff (
    intent_hash TEXT,
    token_id TEXT,
    tx_hash TEXT,
    receipt_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    amount HUGEINT NOT NULL,
    PRIMARY KEY (intent_hash, token_id)
);
"""


def connect(path: str | Path = "data/desk.duckdb") -> duckdb.DuckDBPyConnection:
    """Open the database and make sure the tables exist."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    return con


def _ts(ns):
    """Convert nanoseconds to the microseconds that make_timestamp() takes."""
    return int(ns) // 1000 if ns else None


def _insert_events(con, events, block_height=None, block_ts_ns=None) -> None:
    events = list(events)
    if not events:
        return
    con.executemany(
        "INSERT OR IGNORE INTO silver.event"
        " VALUES (?, ?, ?, ?, ?, make_timestamp(?), ?, ?, ?)",
        [
            [
                e["receipt_id"],
                e["log_index"],
                e["data_index"],
                e["tx_hash"],
                block_height,
                _ts(block_ts_ns),
                e["standard"],
                e["event"],
                json.dumps(e["data"]),
            ]
            for e in events
        ],
    )
    diffs = list(iter_token_diffs(events))
    if diffs:
        con.executemany(
            "INSERT OR IGNORE INTO silver.token_diff VALUES (?, ?, ?, ?, ?, ?)",
            [
                [
                    d["intent_hash"],
                    d["token_id"],
                    d["tx_hash"],
                    d["receipt_id"],
                    d["account_id"],
                    int(d["amount"]),
                ]
                for d in diffs
            ],
        )


def event_count(con) -> int:
    return con.execute("SELECT count(*) FROM silver.event").fetchone()[0]


def load_tx(con, tx: dict) -> int:
    """Store the events of one RPC tx_status result. Return the new event count."""
    before = event_count(con)
    _insert_events(con, iter_events(tx))
    return event_count(con) - before


def load_block(con, block: dict) -> int:
    """Store the intents.near receipts of one neardata block, raw and parsed.

    Return the number of new events.
    """
    before = event_count(con)
    height = block["block"]["header"]["height"]
    for r in iter_block_receipts(block):
        con.execute(
            "INSERT OR IGNORE INTO bronze.receipt"
            " (receipt_id, tx_hash, block_height, block_ts, predecessor_id, raw)"
            " VALUES (?, ?, ?, make_timestamp(?), ?, ?)",
            [
                r["receipt_id"],
                r["tx_hash"],
                r["block_height"],
                _ts(r["block_ts_ns"]),
                r["predecessor_id"],
                json.dumps(r["raw"]),
            ],
        )
        _insert_events(
            con,
            parse_logs(r["receipt_id"], r["tx_hash"], r["logs"]),
            r["block_height"],
            r["block_ts_ns"],
        )
    con.execute("INSERT OR IGNORE INTO bronze.block_seen (block_height) VALUES (?)", [height])
    return event_count(con) - before
