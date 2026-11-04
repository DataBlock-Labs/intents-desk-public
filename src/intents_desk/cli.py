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
    print(f"blocks {head - args.blocks + 1}..{head}: fetched {fetched}, new events {new_events}")


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
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
