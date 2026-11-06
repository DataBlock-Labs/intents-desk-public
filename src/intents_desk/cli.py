"""Command line: fetch intents.near activity, summarize it, check one swap."""

import argparse
import json
import os
import queue
import threading
import time

from . import ops, sources
from .store import connect, load_block

PRODUCT = "intents-desk"


def next_gap(gap: float, base: float, rate_limited: bool) -> float:
    """Seconds between request starts: back off 1.5x after a 429, else drift back to base."""
    return min(gap * 1.5, 5.0) if rate_limited else max(base, gap * 0.98)


def cmd_fetch(args) -> None:
    """Fetch the last N final blocks and keep their intents.near receipts.

    One thread fetches at most one block per `--sleep` seconds (neardata's free limit);
    the main thread stores the previous block meanwhile. Blocks already fetched are
    skipped, so an interrupted run can be resumed.
    """
    con = connect(args.db)
    head = args.end or sources.get_final_height()
    seen = {
        h
        for (h,) in con.execute(
            "SELECT block_height FROM bronze.block_seen WHERE block_height > ?",
            [head - args.blocks],
        ).fetchall()
    }
    todo = [h for h in range(head, head - args.blocks, -1) if h not in seen]
    blocks: queue.Queue = queue.Queue(maxsize=8)

    def produce() -> None:
        gap, hits, start = args.sleep, sources.RATE_LIMITED[0], time.monotonic()
        for height in todo:
            time.sleep(max(0.0, start - time.monotonic()))
            start = time.monotonic() + gap
            try:
                blocks.put((height, sources.get_block(height)))
            except (OSError, ValueError, RuntimeError) as exc:
                blocks.put((height, exc))
                return
            gap = next_gap(gap, args.sleep, sources.RATE_LIMITED[0] > hits)
            hits = sources.RATE_LIMITED[0]
        blocks.put(None)

    threading.Thread(target=produce, daemon=True).start()
    began = time.monotonic()
    with ops.audited(con, PRODUCT, "fetch") as run:
        _consume(con, blocks, todo, began, run)
    print(
        f"blocks {head - args.blocks + 1}..{head}: fetched {run['rows']},"
        f" new events {run['events']}"
    )


def _consume(con, blocks, todo, began, run) -> None:
    """Store blocks from the fetch thread; print progress every 100 blocks."""
    new_events = fetched = 0
    while (item := blocks.get()) is not None:
        height, block = item
        if isinstance(block, Exception):
            run.update(rows=fetched, events=new_events)
            raise block  # resume later: stored blocks are skipped
        fetched += 1
        if block is not None:
            new_events += load_block(con, block)
        if fetched % 100 == 0:
            per_min = fetched / (time.monotonic() - began) * 60
            print(
                f"{fetched}/{len(todo)} blocks, {per_min:.0f}/min, events {new_events},"
                f" 429s {sources.RATE_LIMITED[0]}, at {height}",
                flush=True,
            )
        run.update(
            rows=fetched, events=new_events, rate_limited=sources.RATE_LIMITED[0]
        )


def cmd_stats(args) -> None:
    """Print the time window, event counts and swap counts in the database."""
    con = connect(args.db)
    print(
        con.sql(
            "SELECT count(*) AS receipts, count(DISTINCT tx_hash) AS txns,"
            " min(block_ts) AS first_ts, max(block_ts) AS last_ts FROM bronze.receipt"
        )
    )
    print(
        con.sql(
            "SELECT standard, event, count(*) AS n FROM silver.event"
            " GROUP BY ALL ORDER BY n DESC"
        )
    )
    print(
        con.sql(
            "SELECT count(DISTINCT intent_hash) AS swap_intents,"
            " count(DISTINCT account_id) AS accounts FROM silver.token_diff"
        )
    )


def cmd_swap(args) -> None:
    """Show the 1Click status trail for one deposit address."""
    print(json.dumps(sources.get_swap_status(args.deposit_address), indent=2))


def cmd_sync(args) -> None:
    """Get every 1Click swap of the last N hours from the Explorer API."""
    from datetime import UTC, datetime, timedelta

    from .store import load_explorer_txs, purge_api_data

    con = connect(args.db)
    start = datetime.now(UTC) - timedelta(hours=args.hours)
    total = 0
    with ops.audited(con, PRODUCT, "sync-cli") as run:
        for page in sources.iter_explorer_since(start):
            total += load_explorer_txs(con, page)
            print(f"swaps {total}, oldest {page[-1]['createdAt']}", flush=True)
        run["rollup_rows"] = ops.rollup_swaps(con)  # daily numbers survive the purge
        run["purged"] = purge_api_data(con)
        run["rows"] = total
    print(f"purged {run['purged']} swaps older than 24 h (1Click API terms)")
    print(con.sql("SELECT status, count(*) AS n FROM silver.swap GROUP BY ALL"))


def cmd_partners(args) -> None:
    """Print the refund rate per partner (1Click referral) and flag the bad ones."""
    from .store import partner_report

    con = connect(args.db)
    rel = partner_report(con, args.hours, args.min_swaps, args.alert_pct)
    rows = rel.fetchall()
    print(
        f"partners in the last {args.hours:g} h (alert: >= {args.min_swaps} swaps,"
        f" >= {args.alert_pct:g}% refunded)"
    )
    print(
        f"{'partner':44} {'swaps':>6} {'refund%':>7} {'refunded$':>10} {'senders':>7}"
        "  top reason"
    )
    for p, n, pct, usd, senders, reason, _first, alert in rows[: args.limit]:
        flag = "ALERT " if alert else "      "
        print(
            f"{flag}{p[:38]:38} {n:6} {pct:7} {usd or 0:10,.0f} {senders:7}"
            f"  {reason or ''}"
        )


def cmd_fill_sample(args) -> None:
    """Read 1Click updatedAt for a random sample of settled swaps (fill time)."""
    from .store import fill_candidates, save_fill

    con = connect(args.db)
    rows = fill_candidates(con, args.n)
    for i, (address, created_at) in enumerate(rows, 1):
        status = sources.get_swap_status(address)
        if status.get("status") == "SUCCESS" and status.get("updatedAt"):
            save_fill(con, address, created_at, status["updatedAt"])
        if i % 50 == 0:
            print(f"sampled {i}/{len(rows)}", flush=True)
        time.sleep(args.sleep)
    print(con.sql("SELECT count(*) AS sampled FROM gold.fill_sample"))


def cmd_thesis(args) -> None:
    """Write the thesis report (markdown + SVG charts) from the database."""
    from .report import write_thesis

    con = connect(args.db)
    with ops.audited(con, PRODUCT, "thesis") as run:
        run["path"] = write_thesis(con, args.out)
    print(run["path"])


def cmd_partner_report(args) -> None:
    """Write a report for the partners whose referral matches a LIKE pattern."""
    from .report import write_partner_report

    print(write_partner_report(connect(args.db), args.referral, args.out))


def cmd_audit(args) -> None:
    """Show the most recent job runs from ops.run_log."""
    con = connect(args.db)
    print(
        f"{'started (UTC)':19}  {'product':12} {'job':10} {'status':7} {'rows':>8} {'secs':>7}  error"
    )
    for started, product, job, status, rows, secs, err in ops.recent_runs(
        con, args.limit
    ):
        print(
            f"{started:19}  {product:12} {job:10} {status:7} {rows or 0:8} {secs or 0:7}  {err}"
        )


def cmd_rollup(args) -> None:
    """Add hourly swap aggregates (final hours only) from this DB or another one."""
    import duckdb

    con = connect(args.db)
    source = duckdb.connect(args.from_db, read_only=True) if args.from_db else None
    with ops.audited(con, PRODUCT, "rollup") as run:
        run["rows"] = ops.rollup_swaps(con, source, label=args.from_db or "self")
        run["from"] = args.from_db or args.db
    print(
        con.sql(
            "SELECT day, sum(swaps) AS swaps, max(hours) AS hours, round(sum(usd_in)) AS usd_in,"
            " round(sum(refunded_usd)) AS refunded_usd FROM ops.daily_swap_summary"
            " GROUP BY day ORDER BY day"
        )
    )


def cmd_backup(args) -> None:
    """Back up the ops tables and public chain data to Parquet, then verify the copy."""
    con = connect(args.db)
    with ops.audited(con, PRODUCT, "backup") as run:
        folder = ops.backup(con, PRODUCT, args.out)
        problems = ops.verify_backup(folder)
        if args.push and not problems:
            problems = ops.push(folder, args.push)
            run["pushed_to"] = args.push
        run.update(path=str(folder), problems=problems)
        if problems:
            raise RuntimeError(f"backup check failed: {problems}")
    print(folder, f"-> {args.push} (hashes checked on the remote)" if args.push else "")


def cmd_verify_backup(args) -> None:
    problems = ops.verify_backup(args.folder)
    print("ok" if not problems else "\n".join(problems))
    raise SystemExit(1 if problems else 0)


def cmd_fees(args) -> None:
    """Print the fee account's balances: in its wallet and inside intents.near."""
    from .web import SWAP_ASSETS

    account = args.account or sources.fee_settings()[0]
    yocto = int((sources.get_account(account) or {}).get("amount", 0))
    print(f"{account} wallet: {yocto / 10**24:.6f} NEAR ({yocto} yocto)")
    held = sources.intents_balances(account, [t for t, _ in SWAP_ASSETS.values()])
    for symbol, (token, decimals) in SWAP_ASSETS.items():
        n = held.get(token, 0)
        print(f"{account} in intents.near: {n / 10**decimals:.6f} {symbol} ({n})")


def cmd_revenue(args) -> None:
    """Fees earned per day (ops.daily_revenue), and a check against the Explorer."""
    con = connect(args.db)
    print(con.sql("SELECT * FROM ops.daily_revenue ORDER BY day"))
    total = con.execute("SELECT coalesce(sum(fee_usd), 0) FROM ops.daily_revenue")
    print(f"total fees earned: ${total.fetchone()[0]:.4f} (the solver budget)")
    ours = {
        r[0]
        for r in con.execute(
            "SELECT deposit_address FROM app.quote WHERE status = 'SUCCESS'"
        ).fetchall()
    }
    if os.environ.get("INTENTS_EXPLORER_JWT"):
        referral = os.environ.get("REFERRAL_ID", "intents-desk")
        seen = sources.get_explorer_txs(numberOfTransactions=1000, referral=referral)
        found = {t["depositAddress"] for t in seen if t.get("status") == "SUCCESS"}
        print(
            f"Explorer referral={referral}: {len(found)} successful swaps;"
            f" ours not in the Explorer: {len(ours - found)}"
        )


def cmd_serve(args) -> None:
    """Run the local dashboard."""
    from .web import serve

    serve(args.port, db_path=args.db)


def main() -> None:
    parser = argparse.ArgumentParser(prog="intents-desk")
    parser.add_argument("--db", default="data/desk.duckdb")
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("fetch", help="fetch the last N blocks of intents.near activity")
    p.add_argument("--blocks", type=int, default=100)
    p.add_argument("--end", type=int, help="last block height (default: final head)")
    # neardata free tier: 30 blocks/minute per IP (HTTP 429 text, checked 2026-11-05)
    p.add_argument("--sleep", type=float, default=2.0)
    p.set_defaults(func=cmd_fetch)
    sub.add_parser("stats", help="summarize the database").set_defaults(func=cmd_stats)
    p = sub.add_parser("swap", help="where's my swap? (1Click status trail)")
    p.add_argument("deposit_address")
    p.set_defaults(func=cmd_swap)
    p = sub.add_parser("sync", help="get 1Click swaps from the Explorer API")
    p.add_argument("--hours", type=float, default=24)
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("partners", help="refund rate per partner, with alerts")
    p.add_argument("--hours", type=float, default=1)
    p.add_argument("--min-swaps", type=int, default=50)
    p.add_argument("--alert-pct", type=float, default=50)
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_partners)
    p = sub.add_parser("fill-sample", help="sample fill times from 1Click")
    p.add_argument("--n", type=int, default=300)
    p.add_argument("--sleep", type=float, default=0.5)
    p.set_defaults(func=cmd_fill_sample)
    p = sub.add_parser("thesis", help="write the thesis report")
    p.add_argument("--out", default="reports")
    p.set_defaults(func=cmd_thesis)
    p = sub.add_parser("partner-report", help="write a report for some partners")
    p.add_argument("--referral", required=True, help="SQL LIKE pattern, e.g. %%:pda")
    p.add_argument("--out", default="reports")
    p.set_defaults(func=cmd_partner_report)
    p = sub.add_parser("audit", help="show recent job runs (ops.run_log)")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_audit)
    p = sub.add_parser(
        "rollup", help="hourly swap aggregates into ops.hourly_swap_summary"
    )
    p.add_argument("--from-db", help="read swaps from another DuckDB file (read-only)")
    p.set_defaults(func=cmd_rollup)
    p = sub.add_parser("backup", help="Parquet backup of ops + public chain tables")
    p.add_argument("--out", default=os.environ.get("BACKUP_DIR", "data/backups"))
    p.add_argument(
        "--push",
        default=os.environ.get("BACKUP_PUSH", ""),
        help="copy to host:/path over SSH and check hashes there (e.g. the NAS)",
    )
    p.set_defaults(func=cmd_backup)
    p = sub.add_parser(
        "verify-backup", help="check a backup folder against its manifest"
    )
    p.add_argument("folder")
    p.set_defaults(func=cmd_verify_backup)
    p = sub.add_parser("fees", help="fee account balances (wallet + intents.near)")
    p.add_argument("--account", help="default: FEE_RECIPIENT (datablock.near)")
    p.set_defaults(func=cmd_fees)
    sub.add_parser("revenue", help="fees earned per day + Explorer check").set_defaults(
        func=cmd_revenue
    )
    p = sub.add_parser("serve", help="run the local dashboard")
    p.add_argument("--port", type=int, default=8420)
    p.set_defaults(func=cmd_serve)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
