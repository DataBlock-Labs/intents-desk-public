"""Offline benchmark of block parse + store: ms per block and a row checksum.

    uv run python scripts/bench_parse.py        # prints JSON; same input every run

The checksum covers every stored key, so a faster version must store the same rows.
"""

import hashlib
import json
import statistics
import time
from pathlib import Path

from intents_desk.store import connect, load_block

ROOT = Path(__file__).resolve().parent.parent
# A busy real block (public chain data); falls back to the small test fixture.
FIXTURE = ROOT / "data" / "bench_block.json"
if not FIXTURE.exists():
    FIXTURE = ROOT / "tests" / "fixtures" / "block_intents.json"
RUNS = 30


def main() -> None:
    import sys

    max_ms = (
        float(sys.argv[sys.argv.index("--max-ms") + 1])
        if "--max-ms" in sys.argv
        else None
    )
    block = json.loads(FIXTURE.read_text())
    times = []
    for _ in range(RUNS):
        con = connect(":memory:")
        start = time.perf_counter()
        load_block(con, block)
        times.append((time.perf_counter() - start) * 1000)
    rows = [
        con.execute(q).fetchall()
        for q in (
            "SELECT receipt_id, tx_hash, block_height, epoch(block_ts) FROM bronze.receipt ORDER BY ALL",
            "SELECT receipt_id, log_index, data_index, standard, event FROM silver.event ORDER BY ALL",
            "SELECT intent_hash, token_id, account_id, amount::VARCHAR FROM silver.token_diff ORDER BY ALL",
        )
    ]
    print(
        json.dumps(
            {
                "parse_ms_per_block": round(statistics.median(times), 2),
                "rows": [len(r) for r in rows],
                "checksum": hashlib.sha256(
                    json.dumps(rows, default=str).encode()
                ).hexdigest()[:16],
            }
        )
    )

    if max_ms is not None and statistics.median(times) > max_ms:
        raise SystemExit(
            f"bench_parse: {statistics.median(times):.1f} ms/block > {max_ms} ms"
        )


if __name__ == "__main__":
    main()
