"""Measure Intents Desk: time, space and response size (tokens), plus an output checksum.

    uv run python scripts/bench.py measure            # writes bench/<utc time>.json
    uv run python scripts/bench.py gate A.json B.json --target summary_cold_s --why "..."

The gate keeps a change only when the target metric gets better, the checksum is the
same (no lost rows) and no test is lost. It appends one row to bench/attempts.jsonl.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
DB = ROOT / "data" / "desk.duckdb"
SWAP = "TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq"  # a known refunded swap
WINDOW = ("2026-11-04 13:00:00+00", "2026-11-04 14:00:00+00")  # frozen for the checksum
LOWER_IS_BETTER = True  # every metric below is a cost


def timed(fn):
    start = time.perf_counter()
    out = fn()
    return out, round(time.perf_counter() - start, 4)


def checksum(con) -> str:
    """Hash the swap counts per status and per partner in the frozen window."""
    rows = con.execute(
        "SELECT status, coalesce(referral, ''), count(*) FROM silver.swap"
        " WHERE created_at >= ?::TIMESTAMPTZ AND created_at < ?::TIMESTAMPTZ"
        " GROUP BY ALL ORDER BY ALL",
        list(WINDOW),
    ).fetchall()
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


def count_tests() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    ).stdout
    last = out.strip().splitlines()[-1] if out.strip() else ""
    return int(last.split()[0]) if " passed" in last and "failed" not in last else 0


def measure() -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from intents_desk import store, web

    m: dict = {"at": datetime.now(UTC).isoformat(timespec="seconds")}
    con = duckdb.connect(str(DB), read_only=True)
    swaps = con.execute("SELECT count(*) FROM bronze.explorer_tx").fetchone()[0]
    m["db_bytes_per_1k_swaps"] = round(DB.stat().st_size / max(swaps, 1) * 1000)
    m["checksum"] = checksum(con)
    _, m["partner_report_s"] = timed(
        lambda: store.partner_report(con, 1, 50, 50).fetchall()
    )
    con.close()

    web._cache.update(at=0.0, data=None)
    summary, m["summary_cold_s"] = timed(web.get_summary)
    _, m["summary_warm_s"] = timed(web.get_summary)
    m["summary_bytes"] = len(json.dumps(summary))
    swap, m["swap_s"] = timed(lambda: web.swap_view(SWAP, "json"))
    m["swap_bytes"] = len(json.dumps(swap))
    agent, _ = timed(lambda: web.swap_view(SWAP, "agent"))
    m["swap_agent_bytes"] = len(agent if isinstance(agent, str) else json.dumps(agent))
    m["tests_passed"] = count_tests()
    return m


def gate(base: dict, cand: dict, target: str) -> tuple[str, str]:
    if cand["checksum"] != base["checksum"]:
        return "REJECT", "checksum changed (rows differ)"
    if cand["tests_passed"] < base["tests_passed"]:
        return "REJECT", "fewer tests pass"
    better = (
        cand[target] < base[target] if LOWER_IS_BETTER else cand[target] > base[target]
    )
    return ("KEEP", "target improved") if better else ("REJECT", "target not improved")


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("measure")
    g = sub.add_parser("gate")
    g.add_argument("base")
    g.add_argument("cand")
    g.add_argument("--target", required=True)
    g.add_argument("--why", default="")
    args = p.parse_args()
    BENCH.mkdir(exist_ok=True)
    if args.cmd == "measure":
        m = measure()
        path = BENCH / f"{m['at'].replace(':', '')}.json"
        path.write_text(json.dumps(m, indent=2))
        print(json.dumps(m, indent=2))
        print(path)
        return
    base, cand = (json.loads(Path(f).read_text()) for f in (args.base, args.cand))
    verdict, reason = gate(base, cand, args.target)
    row = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": "si",
        "target": args.target,
        "why": args.why,
        "measured": {args.target: [base[args.target], cand[args.target]]},
        "gate": verdict,
        "reason": reason,
        "branch": os.popen("git -C " + str(ROOT) + " branch --show-current")
        .read()
        .strip(),
    }
    with open(BENCH / "attempts.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    print(verdict, reason, row["measured"])


if __name__ == "__main__":
    main()
