"""Write video/data/incident.json: the masked totals the video and blog page use.

Usage: MASK_PARTNERS=1 uv run python video/export_data.py <db copy> 2026-11-04
It reads only aggregates (no addresses, no tx hashes, partner names masked).
"""

import json
import os
import sys
from collections import Counter
from pathlib import Path

import duckdb

from intents_desk import incidents


def export(db: str, day: str) -> dict:
    assert os.environ.get("MASK_PARTNERS") == "1", "run with MASK_PARTNERS=1"
    con = duckdb.connect(db, read_only=True)
    found = [i for i in incidents.listing(con, 50) if i["started_at"].startswith(day)]
    parts = [incidents.public(i) for i in found if i["scope"] == "partner"]
    [net] = [i for i in found if i["scope"] == "network"]
    hours: dict[str, list[float]] = {}
    for p in parts:
        for hour, swaps, bad_pct in p["hours"]:
            h = hours.setdefault(hour[:13] + ":00Z", [0, 0.0])
            h[0] += swaps
            h[1] += swaps * bad_pct / 100
    all_hours = {h[:13] + ":00Z": [s, b] for h, s, b in net["hours"]}
    routes: Counter = Counter()
    for p in parts:
        for route, n, _pct in p["routes"]:
            routes[route] += n
    swaps = sum(p["swaps"] for p in parts)
    return {
        "partners": [p["referral"] for p in parts],
        "started_at": min(p["started_at"] for p in parts),
        "ended_at": max(p["ended_at"] for p in parts),
        "swaps": swaps,
        "refunded": sum(p["refunded"] for p in parts),
        "failed": sum(p["failed"] for p in parts),
        "usd_in": round(sum(p["usd_in"] or 0 for p in parts)),
        "top_reason": parts[0]["top_reason"],
        "routes": routes.most_common(5),
        # hour, partner swaps, partner bad %, all swaps, all bad %
        "hours": [
            [h, s, round(100 * b / s, 1), *all_hours.get(h, [None, None])]
            for h, (s, b) in sorted(hours.items())
        ],
    }


if __name__ == "__main__":
    out = Path(__file__).parent / "data" / "incident.json"
    out.write_text(json.dumps(export(sys.argv[1], sys.argv[2]), indent=1) + "\n")
    print(out)
