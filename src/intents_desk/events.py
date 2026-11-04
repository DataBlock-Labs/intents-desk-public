"""Parse NEAR Intents event logs (EVENT_JSON) from transactions and blocks."""

import json
from collections.abc import Iterable, Iterator

PREFIX = "EVENT_JSON:"
CONTRACT = "intents.near"


def parse_logs(receipt_id: str, tx_hash: str, logs: Iterable[str]) -> Iterator[dict]:
    """Yield one dict per data entry of each EVENT_JSON log line."""
    for log_index, log in enumerate(logs):
        if not log.startswith(PREFIX):
            continue
        event = json.loads(log[len(PREFIX) :])
        for i, data in enumerate(event.get("data", [])):
            yield {
                "tx_hash": tx_hash,
                "receipt_id": receipt_id,
                "log_index": log_index,
                "data_index": i,
                "standard": event["standard"],
                "event": event["event"],
                "data": data,
            }


def iter_events(tx: dict) -> Iterator[dict]:
    """Yield the events of one RPC tx_status result."""
    result = tx.get("result", tx)
    tx_hash = result["transaction"]["hash"]
    for outcome in result["receipts_outcome"]:
        yield from parse_logs(outcome["id"], tx_hash, outcome["outcome"]["logs"])


def iter_block_receipts(block: dict) -> Iterator[dict]:
    """Yield the intents.near receipt outcomes of one neardata block."""
    header = block["block"]["header"]
    for shard in block.get("shards", []):
        for item in shard.get("receipt_execution_outcomes", []):
            if item["receipt"]["receiver_id"] != CONTRACT:
                continue
            outcome = item["execution_outcome"]
            yield {
                "receipt_id": outcome["id"],
                "tx_hash": item.get("tx_hash"),
                "block_height": header["height"],
                "block_ts_ns": int(header["timestamp"]),
                "predecessor_id": item["receipt"]["predecessor_id"],
                "logs": outcome["outcome"]["logs"],
                "raw": item,
            }


def iter_token_diffs(events: Iterable[dict]) -> Iterator[dict]:
    """Yield one row per (intent, token) change from dip4 token_diff events."""
    for ev in events:
        if ev["standard"] != "dip4" or ev["event"] != "token_diff":
            continue
        data = ev["data"]
        for token, amount in data["diff"].items():
            yield {
                "tx_hash": ev["tx_hash"],
                "receipt_id": ev["receipt_id"],
                "intent_hash": data["intent_hash"],
                "account_id": data["account_id"],
                "token_id": token,
                "amount": amount,
            }
