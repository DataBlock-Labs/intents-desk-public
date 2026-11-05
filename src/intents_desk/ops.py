"""Operations audit for DataBlock products: a run log, daily rollups and backups.

Product-neutral on purpose: any DataBlock product with a DuckDB file can call
`ensure(con)`, wrap its jobs in `audited(...)` and run `backup(...)`.

What is kept, and for how long:
- ops.run_log: one row per job run (start, end, status, rows, error, code version, host).
  Kept indefinitely. No API payloads.
- ops.hourly_swap_summary: aggregates per UTC hour, partner, route and status, rolled up
  from silver.swap once each hour is complete and settled, before the 24 h purge.
  ops.daily_swap_summary is a view that sums the hours. Raw 1Click/Explorer rows are never archived:
  the 1Click API terms (4.1(e)) forbid keeping API data over 24 hours.
- backup(): Parquet copies of the ops tables and of public chain data (bronze.receipt,
  silver.event, silver.token_diff) with a manifest of row counts and SHA-256 hashes.
"""

import hashlib
import json
import os
import socket
import subprocess
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

OPS_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS ops;
CREATE TABLE IF NOT EXISTS ops.run_log (
    run_id TEXT PRIMARY KEY,
    product TEXT NOT NULL,
    job TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL,          -- running | ok | error
    rows_out BIGINT,
    detail JSON,
    error TEXT,
    code_version TEXT,
    host TEXT
);
CREATE TABLE IF NOT EXISTS ops.hourly_swap_summary (
    hour TIMESTAMPTZ,
    partner TEXT,
    origin_chain TEXT,
    destination_chain TEXT,
    status TEXT,
    swaps BIGINT NOT NULL,
    usd_in DOUBLE,
    refunded_usd DOUBLE,
    top_refund_reason TEXT,
    rolled_up_at TIMESTAMPTZ DEFAULT now(),
    source TEXT,
    PRIMARY KEY (hour, partner, origin_chain, destination_chain, status)
);
CREATE VIEW IF NOT EXISTS ops.daily_swap_summary AS
SELECT CAST(hour AT TIME ZONE 'UTC' AS DATE) AS day, partner, origin_chain,
    destination_chain, status, sum(swaps) AS swaps, sum(usd_in) AS usd_in,
    sum(refunded_usd) AS refunded_usd, count(DISTINCT hour) AS hours
FROM ops.hourly_swap_summary GROUP BY ALL;
"""
# Public chain tables that may be backed up in full (not 1Click API data).
CHAIN_TABLES = ("bronze.receipt", "silver.event", "silver.token_diff")
CHAIN_MACRO = """
CREATE OR REPLACE TEMP MACRO ops_chain(a) AS (
    CASE
        WHEN a LIKE '1cs_v1:%' THEN split_part(a, ':', 2)
        WHEN regexp_replace(a, '^[^:]*:', '') LIKE '%.omft.near'
            THEN split_part(split_part(regexp_replace(a, '^[^:]*:', ''), '-', 1), '.', 1)
        WHEN a LIKE '%omni.hot.tg%' THEN 'hot-omni'
        WHEN a LIKE '%.near' THEN 'near'
        ELSE split_part(regexp_replace(a, '^[^:]*:', ''), '.', 1)
    END
);
"""


def ensure(con) -> None:
    con.execute(OPS_SCHEMA)


def code_version() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except OSError:
        sha = ""
    return sha or os.environ.get("CODE_VERSION", "unknown")


@contextmanager
def audited(con, product: str, job: str):
    """Record one job run in ops.run_log. Yields a dict: set "rows" and any details.

    The row is written as "running" first, so a crash still leaves a trace.
    Call ensure() once at connect time; jobs on other threads must not re-run DDL
    (DuckDB refuses concurrent catalog changes).
    """
    run_id = str(uuid.uuid4())
    con.execute(
        "INSERT INTO ops.run_log (run_id, product, job, started_at, status, code_version, host)"
        " VALUES (?, ?, ?, now(), 'running', ?, ?)",
        [run_id, product, job, code_version(), socket.gethostname()],
    )
    info: dict = {}
    try:
        yield info
    except BaseException as exc:
        con.execute(
            "UPDATE ops.run_log SET finished_at = now(), status = 'error', error = ?,"
            " rows_out = ?, detail = ? WHERE run_id = ?",
            [
                f"{type(exc).__name__}: {exc}"[:2000],
                info.get("rows"),
                json.dumps(info),
                run_id,
            ],
        )
        raise
    con.execute(
        "UPDATE ops.run_log SET finished_at = now(), status = 'ok', rows_out = ?, detail = ?"
        " WHERE run_id = ?",
        [info.get("rows"), json.dumps(info, default=str), run_id],
    )


SETTLE_HOURS = 2  # statuses (refunds at the quote deadline) settle within this time


def rollup_swaps(con, source=None, label: str = "") -> int:
    """Add hourly aggregates of silver.swap (in `source`, default `con`) to `con`.

    An hour is rolled up once, and only when it is final in the source:
    - complete: the source holds swaps from before the hour started (nothing purged);
    - settled: the source's newest swap is at least SETTLE_HOURS after the hour ended.
    Hours already rolled up are never replaced, so a later source that holds only part
    of an hour (after the 24 h purge) cannot shrink the numbers. Return rows written.
    """
    src = source or con
    src.execute(CHAIN_MACRO)
    done = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT epoch(hour)::BIGINT FROM ops.hourly_swap_summary"
        ).fetchall()
    }
    rows = src.execute(
        f"""
        WITH bounds AS (
            SELECT min(created_at) AS oldest, max(created_at) AS newest FROM silver.swap
        )
        SELECT epoch(date_trunc('hour', created_at))::BIGINT AS hour,
            coalesce(nullif(referral, ''), '(none)'),
            ops_chain(origin_asset), ops_chain(destination_asset), status,
            count(*), sum(amount_in_usd),
            sum(amount_in_usd) FILTER (WHERE status = 'REFUNDED'),
            mode(refund_reason) FILTER (WHERE status = 'REFUNDED')
        FROM silver.swap, bounds
        WHERE date_trunc('hour', created_at) > oldest  -- the hour is complete in the source
            AND date_trunc('hour', created_at) + INTERVAL {1 + SETTLE_HOURS} HOUR <= newest
        GROUP BY ALL
        """
    ).fetchall()
    rows = [r for r in rows if r[0] not in done]
    if not rows:
        return 0
    con.executemany(
        "INSERT OR IGNORE INTO ops.hourly_swap_summary (hour, partner, origin_chain,"
        " destination_chain, status, swaps, usd_in, refunded_usd, top_refund_reason, source)"
        " VALUES (to_timestamp(?), ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [[*r, label or "self"] for r in rows],
    )
    return len(rows)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(con, product: str, out_dir: str | Path) -> Path:
    """Write Parquet copies of the ops and public chain tables plus a manifest.

    Layout: <out_dir>/<product>/<UTC timestamp>/<table>.parquet and manifest.json.
    """
    ensure(con)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    folder = Path(out_dir) / product / stamp
    folder.mkdir(parents=True, exist_ok=False)
    tables = ["ops.run_log", "ops.hourly_swap_summary", *CHAIN_TABLES]
    existing = {
        f"{s}.{t}"
        for s, t in con.execute(
            "SELECT schema_name, table_name FROM duckdb_tables()"
        ).fetchall()
    }
    manifest = {
        "product": product,
        "created_at": stamp,
        "code_version": code_version(),
        "tables": {},
        "not_included": "raw 1Click/Explorer API data (terms 4.1(e))",
    }
    for table in tables:
        if table not in existing:
            continue
        path = folder / f"{table}.parquet"
        con.execute(
            f"COPY (SELECT * FROM {table}) TO '{path}' (FORMAT parquet, COMPRESSION zstd)"
        )
        rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        manifest["tables"][table] = {
            "rows": rows,
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
        }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return folder


def verify_backup(folder: str | Path) -> list[str]:
    """Check every file in a backup against its manifest. Return the problems (empty = ok)."""
    import duckdb

    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text())
    problems = []
    con = duckdb.connect()
    for table, meta in manifest["tables"].items():
        path = folder / f"{table}.parquet"
        if not path.exists():
            problems.append(f"{table}: file missing")
            continue
        if _sha256(path) != meta["sha256"]:
            problems.append(f"{table}: hash differs")
            continue  # a changed file may not even be readable Parquet
        rows = con.execute(f"SELECT count(*) FROM '{path}'").fetchone()[0]
        if rows != meta["rows"]:
            problems.append(f"{table}: {rows} rows, manifest says {meta['rows']}")
    return problems


def recent_runs(con, limit: int = 20) -> list[tuple]:
    return con.execute(
        """
        SELECT strftime(started_at AT TIME ZONE 'UTC', '%Y-%m-%d %H:%M:%S'), product, job, status,
            rows_out, round(epoch(finished_at - started_at), 1), left(coalesce(error, ''), 80)
        FROM ops.run_log ORDER BY started_at DESC LIMIT ?
        """,
        [limit],
    ).fetchall()


def push(folder: str | Path, target: str) -> list[str]:
    """Copy a backup folder to `target` ("host:/path") over SSH (tar stream), then check
    every file's SHA-256 on the remote side against the manifest. Return the problems.

    The remote layout matches the local one: <target>/<product>/<stamp>/.
    """
    folder = Path(folder)
    host, _, base = target.partition(":")
    if not host or not base.startswith("/"):
        raise ValueError(f"push target must be host:/absolute/path, got {target!r}")
    manifest = json.loads((folder / "manifest.json").read_text())
    remote = f"{base.rstrip('/')}/{manifest['product']}/{folder.name}"
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host]
    subprocess.run([*ssh, "mkdir", "-p", remote], check=True, timeout=60)
    # tar over SSH, not rsync: the NAS's rsync refuses paths under @-folders.
    tar = subprocess.run(
        ["tar", "-C", str(folder), "-cf", "-", "."],
        check=True,
        capture_output=True,
        timeout=600,
    ).stdout
    subprocess.run(
        [
            *ssh,
            f"tar -C '{remote}' -xf - && chmod 440 '{remote}'/* && chmod 550 '{remote}'",
        ],
        input=tar,
        check=True,
        timeout=3600,
    )
    out = subprocess.run(
        [*ssh, "cd", remote, "&&", "sha256sum", "*.parquet"],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    ).stdout
    remote_hashes = {
        line.split()[1]: line.split()[0] for line in out.splitlines() if line.strip()
    }
    return [
        f"{table}: remote hash differs or missing"
        for table, meta in manifest["tables"].items()
        if remote_hashes.get(f"{table}.parquet") != meta["sha256"]
    ]
