"""Command line: fetch intents.near activity, summarize it, check one swap."""

import argparse
import json
import time

from . import sources
from .store import connect, load_block


def cmd_fetch(args) -> None:
    """Fetch the last N final blocks and keep their intents.near receipts.

    Blocks already fetched are skipped, so an interrupted run can be resumed.
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
    new_events = fetched = 0
    for height in range(head, head - args.blocks, -1):
        if height in seen:
            continue
        block = sources.get_block(height)
        fetched += 1
        if block is not None:
            new_events += load_block(con, block)
        time.sleep(args.sleep)
    print(
        f"blocks {head - args.blocks + 1}..{head}: fetched {fetched}, new events {new_events}"
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

    from .store import load_explorer_txs

    con = connect(args.db)
    start = datetime.now(UTC) - timedelta(hours=args.hours)
    total = 0
    for page in sources.iter_explorer_since(start):
        total += load_explorer_txs(con, page)
        print(f"swaps {total}, oldest {page[-1]['createdAt']}", flush=True)
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

    print(write_thesis(connect(args.db), args.out))


def cmd_partner_report(args) -> None:
    """Write a report for the partners whose referral matches a LIKE pattern."""
    from .report import write_partner_report

    print(write_partner_report(connect(args.db), args.referral, args.out))


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
    p.add_argument("--sleep", type=float, default=0.35)  # neardata: ~180 req/min per IP
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
    p = sub.add_parser("serve", help="run the local dashboard")
    p.add_argument("--port", type=int, default=8420)
    p.set_defaults(func=cmd_serve)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
