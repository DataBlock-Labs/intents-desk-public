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
CREATE SCHEMA IF NOT EXISTS gold;
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS bronze.feed_post (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    author TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    text TEXT NOT NULL,
    url TEXT NOT NULL,
    tags TEXT[],
    fetched_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS gold.fill_sample (
    deposit_address TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    fill_s DOUBLE NOT NULL,
    sampled_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.watch (
    account_id TEXT,
    deposit_address TEXT,
    added_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (account_id, deposit_address)
);
ALTER TABLE app.watch ADD COLUMN IF NOT EXISTS last_status TEXT;
ALTER TABLE app.watch ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS app.telegram (
    account_id TEXT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    linked_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.quote (
    deposit_address TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    origin_asset TEXT NOT NULL,
    destination_asset TEXT NOT NULL,
    amount_in TEXT NOT NULL,
    fee_bps INTEGER NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app.link_code (
    code TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
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
CREATE TABLE IF NOT EXISTS bronze.explorer_tx (
    deposit_address TEXT,
    deposit_memo TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    fetched_at TIMESTAMPTZ DEFAULT now(),
    raw JSON NOT NULL,
    PRIMARY KEY (deposit_address, deposit_memo)
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
CREATE OR REPLACE VIEW silver.swap AS
SELECT
    deposit_address,
    deposit_memo,
    created_at,
    status,
    raw->>'originAsset' AS origin_asset,
    raw->>'destinationAsset' AS destination_asset,
    TRY_CAST(raw->>'amountInUsd' AS DOUBLE) AS amount_in_usd,
    TRY_CAST(raw->>'amountOutUsd' AS DOUBLE) AS amount_out_usd,
    raw->>'refundReason' AS refund_reason,
    raw->>'depositType' AS deposit_type,
    raw->>'referral' AS referral,
    raw->>'recipient' AS recipient,
    raw->>'refundTo' AS refund_to,
    fetched_at
FROM bronze.explorer_tx;
CREATE OR REPLACE VIEW gold.partner_hour AS
SELECT
    date_trunc('hour', created_at) AS hour,
    coalesce(nullif(referral, ''), '(none)') AS partner,
    count(*) AS swaps,
    count(*) FILTER (WHERE status = 'REFUNDED') AS refunds,
    round(100 * avg((status = 'REFUNDED')::INT), 1) AS refund_pct,
    round(sum(amount_in_usd)) AS usd_in,
    round(sum(amount_in_usd) FILTER (WHERE status = 'REFUNDED')) AS refunded_usd,
    count(DISTINCT refund_to) AS senders,
    mode(refund_reason) FILTER (WHERE status = 'REFUNDED') AS top_reason
FROM silver.swap
GROUP BY ALL;
"""


def connect(path: str | Path = "data/desk.duckdb") -> duckdb.DuckDBPyConnection:
    """Open the database and make sure the tables exist."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute(SCHEMA)
    from .ops import ensure  # the shared operations audit tables

    ensure(con)
    return con


def _ts(ns):
    """Convert nanoseconds to microseconds (to_timestamp(us / 1e6) is UTC-safe)."""
    return int(ns) // 1000 if ns else None


def _bulk_insert(con, table: str, exprs: list[str], rows: list[list]) -> None:
    """INSERT OR IGNORE many rows in one statement (5x faster than executemany).

    `exprs` has one SQL expression per column, with {} where the value goes.
    """
    if not rows:
        return
    cols = list(map(list, zip(*rows, strict=True)))
    select = ", ".join(e.format(f"unnest($c{i})") for i, e in enumerate(exprs))
    con.execute(
        f"INSERT OR IGNORE INTO {table} SELECT {select}",
        {f"c{i}": col for i, col in enumerate(cols)},
    )


def _insert_events(con, events, block_height=None, block_ts_ns=None) -> None:
    events = list(events)
    ts = _ts(block_ts_ns)
    _bulk_insert(
        con,
        "silver.event",
        [
            "{}",
            "{}::INTEGER",
            "{}::INTEGER",
            "{}",
            "{}::BIGINT",
            "to_timestamp({}::DOUBLE / 1e6)",
            "{}",
            "{}",
            "{}::JSON",
        ],
        [
            [
                e["receipt_id"],
                e["log_index"],
                e["data_index"],
                e["tx_hash"],
                block_height,
                ts,
                e["standard"],
                e["event"],
                json.dumps(e["data"]),
            ]
            for e in events
        ],
    )
    _bulk_insert(
        con,
        "silver.token_diff",
        ["{}", "{}", "{}", "{}", "{}", "{}::HUGEINT"],
        [
            [
                d["intent_hash"],
                d["token_id"],
                d["tx_hash"],
                d["receipt_id"],
                d["account_id"],
                str(int(d["amount"])),
            ]
            for d in iter_token_diffs(events)
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
    receipts = list(iter_block_receipts(block))
    con.begin()  # one transaction per block: executemany commits per row otherwise
    try:
        _store_block(con, receipts, height)
        con.commit()
    except Exception:
        con.rollback()
        raise
    return event_count(con) - before


def _store_block(con, receipts: list[dict], height: int) -> None:
    if receipts:  # one write per table per block, not one per receipt
        _bulk_insert(
            con,
            "bronze.receipt (receipt_id, tx_hash, block_height, block_ts, predecessor_id, raw)",
            [
                "{}",
                "{}",
                "{}::BIGINT",
                "to_timestamp({}::DOUBLE / 1e6)",
                "{}",
                "{}::JSON",
            ],
            [
                [
                    r["receipt_id"],
                    r["tx_hash"],
                    r["block_height"],
                    _ts(r["block_ts_ns"]),
                    r["predecessor_id"],
                    json.dumps(r["raw"]),
                ]
                for r in receipts
            ],
        )
        events = [
            e
            for r in receipts
            for e in parse_logs(r["receipt_id"], r["tx_hash"], r["logs"])
        ]
        _insert_events(
            con, events, receipts[0]["block_height"], receipts[0]["block_ts_ns"]
        )
    con.execute(
        "INSERT OR IGNORE INTO bronze.block_seen (block_height) VALUES (?)", [height]
    )


def load_explorer_txs(con, txs: list[dict]) -> int:
    """Store Explorer swaps. A swap seen again replaces the old row with its new status.

    Return the number of rows written.
    """
    rows = [
        [
            t["depositAddress"],
            t.get("depositMemo") or "",
            t["createdAt"],
            t["status"],
            json.dumps(t),
        ]
        for t in txs
    ]
    con.executemany(
        "INSERT OR REPLACE INTO bronze.explorer_tx"
        " (deposit_address, deposit_memo, created_at, status, raw)"
        " VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def partner_report(con, hours: float, min_swaps: int, alert_pct: float):
    """Get one row per partner for the last `hours`, worst refund rate first.

    `alert` is true when a partner has at least `min_swaps` swaps and a refund rate
    of at least `alert_pct` percent.
    """
    return con.execute(
        """
        SELECT
            coalesce(nullif(referral, ''), '(none)') AS partner,
            count(*) AS swaps,
            round(100 * avg((status = 'REFUNDED')::INT), 1) AS refund_pct,
            round(sum(amount_in_usd) FILTER (WHERE status = 'REFUNDED'))
                AS refunded_usd,
            count(DISTINCT refund_to) AS senders,
            mode(refund_reason) FILTER (WHERE status = 'REFUNDED') AS top_reason,
            CAST(min(created_at) AS VARCHAR) AS first_seen,
            count(*) >= $min AND 100 * avg((status = 'REFUNDED')::INT) >= $pct
                AS alert
        FROM silver.swap
        WHERE created_at > (SELECT max(created_at) FROM silver.swap)
            - to_microseconds(CAST($hours * 3600e6 AS BIGINT))
        GROUP BY 1
        ORDER BY alert DESC, refunded_usd DESC NULLS LAST
        """,
        {"min": min_swaps, "pct": alert_pct, "hours": hours},
    )


API_RETENTION_HOURS = 24  # 1Click API terms 4.1(e): no API data kept longer


def purge_api_data(con, hours: int = API_RETENTION_HOURS) -> int:
    """Delete Explorer swaps fetched or created more than `hours` ago. Return the count."""
    (n,) = con.execute(
        "DELETE FROM bronze.explorer_tx"
        " WHERE least(created_at, fetched_at) < now() - to_hours(CAST(? AS BIGINT))",
        [hours],
    ).fetchone()
    return n


def newest_swap_time(con):
    """Get the time of the newest stored swap (UTC), or None for an empty table."""
    from datetime import UTC, datetime

    [(epoch,)] = con.execute(
        "SELECT epoch(max(created_at)) FROM bronze.explorer_tx"
    ).fetchall()
    return datetime.fromtimestamp(epoch, UTC) if epoch is not None else None


def newest_raw_swaps(con, n: int = 1000) -> list[dict]:
    """Get the raw Explorer rows of the newest `n` swaps, newest first."""
    rows = con.execute(
        "SELECT raw FROM bronze.explorer_tx ORDER BY created_at DESC LIMIT ?", [n]
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def hourly_refunds(con, hours: int = 24) -> list[list]:
    """Get [hour (UTC text), swaps, refund %] for the last `hours` hours of data."""
    return con.execute(
        """
        SELECT strftime(hour AT TIME ZONE 'UTC', '%Y-%m-%dT%H:00Z'),
            sum(swaps)::INT, round(100.0 * sum(refunds) / sum(swaps), 1)
        FROM gold.partner_hour
        WHERE hour > (SELECT max(hour) FROM gold.partner_hour) - to_hours(?)
        GROUP BY hour ORDER BY hour
        """,
        [hours],
    ).fetchall()


def account_swaps(con, account: str, limit: int = 50) -> list[list]:
    """Get the stored swaps where `account` is the recipient or the refund address."""
    return con.execute(
        """
        SELECT deposit_address, CAST(created_at AS VARCHAR), status, origin_asset,
            destination_asset, amount_in_usd, refund_reason
        FROM silver.swap WHERE recipient = $a OR refund_to = $a
        ORDER BY created_at DESC LIMIT $n
        """,
        {"a": account, "n": limit},
    ).fetchall()


def save_feed(con, posts: list[dict]) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO bronze.feed_post"
        " (id, source, author, created_at, text, url, tags) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            [
                p["id"],
                p["source"],
                p["author"],
                p["created_at"],
                p["text"],
                p["url"],
                p["tags"],
            ]
            for p in posts
        ],
    )


def read_feed(con, tag: str | None = None, limit: int = 30) -> list[dict]:
    rows = con.execute(
        """
        SELECT id, source, author, CAST(created_at AS VARCHAR), text, url, tags
        FROM bronze.feed_post
        WHERE len(tags) > 0 AND ($tag IS NULL OR list_contains(tags, $tag))
        ORDER BY created_at DESC LIMIT $n
        """,
        {"tag": tag, "n": limit},
    ).fetchall()
    keys = ("id", "source", "author", "created_at", "text", "url", "tags")
    return [dict(zip(keys, r, strict=True)) for r in rows]


def watch_list(con, account: str) -> list[str]:
    rows = con.execute(
        "SELECT deposit_address FROM app.watch WHERE account_id = ? ORDER BY added_at",
        [account],
    ).fetchall()
    return [r[0] for r in rows]


def log_quote(con, account: str, address: str, request: dict, fee_bps: int) -> None:
    """Keep our own live quote (the deposit address we gave this user): their history."""
    con.execute(
        "INSERT OR IGNORE INTO app.quote (deposit_address, account_id, origin_asset,"
        " destination_asset, amount_in, fee_bps) VALUES (?, ?, ?, ?, ?, ?)",
        [
            address,
            account,
            request["originAsset"],
            request["destinationAsset"],
            request["amount"],
            fee_bps,
        ],
    )


def quote_list(con, account: str, limit: int = 5) -> list[str]:
    rows = con.execute(
        "SELECT deposit_address FROM app.quote WHERE account_id = ?"
        " ORDER BY created_at DESC LIMIT ?",
        [account, limit],
    ).fetchall()
    return [r[0] for r in rows]


MAX_WATCHED = 20


def set_watch(con, account: str, address: str, on: bool) -> None:
    if on:
        n = con.execute(
            "SELECT count(*) FROM app.watch WHERE account_id = ?", [account]
        ).fetchone()[0]
        if n >= MAX_WATCHED:
            raise ValueError(f"You can watch at most {MAX_WATCHED} swaps.")
        con.execute(
            "INSERT OR IGNORE INTO app.watch (account_id, deposit_address)"
            " VALUES (?, ?)",
            [account, address],
        )
    else:
        con.execute(
            "DELETE FROM app.watch WHERE account_id = ? AND deposit_address = ?",
            [account, address],
        )


def fill_candidates(con, n: int) -> list[tuple[str, str]]:
    """Get up to `n` settled SUCCESS swaps not sampled yet, in a fixed random order."""
    return con.execute(
        """
        SELECT deposit_address, CAST(created_at AS VARCHAR) FROM silver.swap
        WHERE status = 'SUCCESS'
            AND created_at < (SELECT max(created_at) FROM silver.swap) - INTERVAL 1 HOUR
            AND deposit_address NOT IN (SELECT deposit_address FROM gold.fill_sample)
        ORDER BY hash(deposit_address) LIMIT ?
        """,
        [n],
    ).fetchall()


def save_fill(con, address: str, created_at: str, updated_at: str) -> None:
    con.execute(
        "INSERT OR REPLACE INTO gold.fill_sample"
        " (deposit_address, created_at, updated_at, fill_s)"
        " VALUES ($a, $c::TIMESTAMPTZ, $u::TIMESTAMPTZ,"
        " epoch($u::TIMESTAMPTZ - $c::TIMESTAMPTZ))",
        {"a": address, "c": created_at, "u": updated_at},
    )


SWAP_STATUSES = (
    "SUCCESS",
    "REFUNDED",
    "PROCESSING",
    "PENDING_DEPOSIT",
    "INCOMPLETE_DEPOSIT",
    "FAILED",
)


def _route_macro(con) -> None:
    from .ops import CHAIN_MACRO

    con.execute(CHAIN_MACRO)


def partner_detail(con, partner: str) -> dict:
    """Everything the partner view shows: last-24 h figures (raw window) and daily history
    (aggregates in ops.daily_swap_summary). Referral '(none)' means swaps with no referral."""
    _route_macro(con)
    where = "coalesce(nullif(referral, ''), '(none)') = $p"
    q = lambda sql: con.execute(sql, {"p": partner}).fetchall()
    [(swaps, refund_pct, refunded, first, last)] = q(
        f"""SELECT count(*), round(100 * avg((status = 'REFUNDED')::INT), 1),
            round(sum(amount_in_usd) FILTER (WHERE status = 'REFUNDED')),
            CAST(min(created_at) AS VARCHAR), CAST(max(created_at) AS VARCHAR)
        FROM silver.swap WHERE {where}"""
    )
    return {
        "partner": partner,
        "swaps_24h": swaps,
        "refund_pct_24h": refund_pct,
        "refunded_usd_24h": refunded,
        "first": first,
        "last": last,
        "hourly": q(
            f"""SELECT strftime(date_trunc('hour', created_at) AT TIME ZONE 'UTC',
                '%Y-%m-%dT%H:00Z'), count(*)::INT,
                round(100 * avg((status = 'REFUNDED')::INT), 1)
            FROM silver.swap WHERE {where} GROUP BY 1 ORDER BY 1"""
        ),
        "daily": q(
            """SELECT CAST(day AS VARCHAR), sum(swaps)::INT,
                round(100.0 * sum(swaps) FILTER (WHERE status = 'REFUNDED') / sum(swaps), 1),
                round(sum(refunded_usd))
            FROM ops.daily_swap_summary WHERE partner = $p
            GROUP BY day ORDER BY day DESC LIMIT 30"""
        ),
        "routes": q(
            f"""SELECT ops_chain(origin_asset) || ' → ' || ops_chain(destination_asset),
                count(*)::INT, round(100 * avg((status = 'REFUNDED')::INT), 1)
            FROM silver.swap WHERE {where} GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""
        ),
        "reasons": q(
            f"""SELECT coalesce(refund_reason, 'not given'), count(*)::INT
            FROM silver.swap WHERE {where} AND status = 'REFUNDED'
            GROUP BY 1 ORDER BY 2 DESC"""
        ),
        "statuses": q(
            f"""SELECT status, count(*)::INT FROM silver.swap WHERE {where}
            GROUP BY 1 ORDER BY 2 DESC"""
        ),
    }


def _ref_secret() -> str:
    from .auth import _session_key

    return _session_key().hex()


def recent_swaps(
    con, status=None, partner=None, route=None, limit: int = 100
) -> list[dict]:
    """The newest swaps, optionally filtered. Values are bound as parameters.

    The public list never contains a full deposit address: only a short form and an
    opaque `ref` (keyed hash) that `address_for_ref` can turn back into the address.
    """
    _route_macro(con)
    rows = con.execute(
        """
        SELECT left(deposit_address, 6) || '…' || right(deposit_address, 4),
            left(md5($k || deposit_address), 20), strftime(timezone('UTC', created_at), '%Y-%m-%dT%H:%M:%SZ'), status,
            ops_chain(origin_asset) || ' → ' || ops_chain(destination_asset) AS route,
            amount_in_usd, coalesce(nullif(referral, ''), '(none)') AS partner, refund_reason
        FROM silver.swap
        WHERE ($s IS NULL OR status = $s)
            AND ($p IS NULL OR coalesce(nullif(referral, ''), '(none)') = $p)
            AND ($r IS NULL OR ops_chain(origin_asset) || ' → '
                || ops_chain(destination_asset) = $r)
        ORDER BY created_at DESC LIMIT $n
        """,
        {"s": status, "p": partner, "r": route, "n": limit, "k": _ref_secret()},
    ).fetchall()
    keys = (
        "short",
        "ref",
        "created_at",
        "status",
        "route",
        "usd_in",
        "partner",
        "reason",
    )
    return [dict(zip(keys, r, strict=True)) for r in rows]


def address_for_ref(con, ref: str) -> str | None:
    """Find the deposit address behind a public `ref` (only swaps still stored, <= 24 h)."""
    row = con.execute(
        "SELECT deposit_address FROM bronze.explorer_tx"
        " WHERE left(md5($k || deposit_address), 20) = $r LIMIT 1",
        {"k": _ref_secret(), "r": ref},
    ).fetchone()
    return row[0] if row else None
