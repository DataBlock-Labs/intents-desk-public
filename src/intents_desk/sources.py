"""Read-only HTTP clients for the public NEAR data sources."""

import json
import time
import urllib.error
import urllib.request

NEARDATA = "https://mainnet.neardata.xyz/v0"
RPC = "https://rpc.mainnet.near.org"
ONECLICK = "https://1click.chaindefuser.com/v0"
HEADERS = {"User-Agent": "intents-desk/0.1", "Content-Type": "application/json"}


def get_json(url: str, body: dict | None = None, retries: int = 6) -> dict:
    """Get JSON from a URL. Retry with backoff on rate limits and server errors."""
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return json.load(exc)
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
        time.sleep(2**attempt)
    raise RuntimeError(f"no response from {url}")


def get_final_height() -> int:
    """Get the height of the latest final block."""
    return get_json(f"{NEARDATA}/last_block/final")["block"]["header"]["height"]


def get_block(height: int) -> dict | None:
    """Get one block with all shards and receipt outcomes. None if the height is empty."""
    block = get_json(f"{NEARDATA}/block/{height}")
    return block if block and "block" in block else None


def get_tx_status(tx_hash: str, signer: str) -> dict:
    """Get a final transaction with all receipt outcomes and logs."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "EXPERIMENTAL_tx_status",
        "params": {"tx_hash": tx_hash, "sender_account_id": signer, "wait_until": "FINAL"},
    }
    return get_json(RPC, body)


def get_swap_status(deposit_address: str) -> dict:
    """Get the 1Click status trail for one swap deposit address."""
    return get_json(f"{ONECLICK}/status?depositAddress={deposit_address}")
