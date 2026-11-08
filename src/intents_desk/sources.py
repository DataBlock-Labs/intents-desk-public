"""HTTP clients for the NEAR data sources and the 1Click swap API."""

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

# Every base URL can be overridden by env (tests point them at fake servers).
NEARDATA = os.environ.get("NEARDATA_URL", "https://mainnet.neardata.xyz/v0")
RPC = os.environ.get("NEAR_RPC_URL", "https://rpc.mainnet.near.org")
# FastNEAR's public transactions API: the only free source that lists one account's txs.
TXAPI = os.environ.get("TXAPI_URL", "https://tx.main.fastnear.com/v0")
ONECLICK = os.environ.get("ONECLICK_URL", "https://1click.chaindefuser.com/v0")
EXPLORER = os.environ.get(  # needs INTENTS_EXPLORER_JWT
    "EXPLORER_URL", "https://explorer.near-intents.org/api/v0"
)
HEADERS = {"User-Agent": "intents-desk/0.1", "Content-Type": "application/json"}


RATE_LIMITED = [0]  # 429 replies seen by this process


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, so an API key in a header never follows a 30x to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)
_public = urllib.request.build_opener()


def get_json(
    url: str, body: dict | None = None, retries: int = 6, gated: bool = True
) -> dict:
    """Get JSON from a URL. Retry with backoff on rate limits and server errors.
    Explorer requests wait for the shared slot (1 request every EXPLORER_GAP s)."""
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(HEADERS)
    # 1Click works without a key. Send the partner key (ONECLICK_JWT) when it is set.
    if url.startswith(ONECLICK) and os.environ.get("ONECLICK_JWT"):
        headers["Authorization"] = f"Bearer {os.environ['ONECLICK_JWT']}"
    if url.startswith(EXPLORER):
        headers["Authorization"] = f"Bearer {os.environ['INTENTS_EXPLORER_JWT']}"
    for attempt in range(retries):
        if gated and url.startswith(EXPLORER):
            explorer_slot(wait=True)
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            # Requests with a key never follow a redirect; public ones may (neardata's
            # last_block/final answers with a 302 to the block).
            opener = _opener if "Authorization" in headers else _public
            with opener.open(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return json.load(exc)
            if exc.code == 429:
                RATE_LIMITED[0] += 1  # read by fetch loops to slow down
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
        "params": {
            "tx_hash": tx_hash,
            "sender_account_id": signer,
            "wait_until": "FINAL",
        },
    }
    return get_json(RPC, body)


def get_swap_status(deposit_address: str, retries: int = 6) -> dict:
    """Get the 1Click status trail for one swap deposit address.

    Web requests pass retries=1, so one visitor cannot hold a thread for minutes.
    """
    return get_json(
        f"{ONECLICK}/status?depositAddress={deposit_address}", retries=retries
    )


def get_explorer_txs(**params) -> list[dict]:
    """Get one page of 1Click swaps from the Explorer API, newest first.

    The API allows 1 request every 5 s, so callers must wait between pages.
    """
    query = urllib.parse.urlencode(params)
    return get_json(f"{EXPLORER}/transactions?{query}")


class ExplorerBusy(Exception):
    """The Explorer slot is taken: a visitor search does not wait for it."""


EXPLORER_GAP = float(os.environ.get("EXPLORER_GAP_S", "5"))  # API: 1 request / 5 s
_explorer = {"lock": threading.Lock(), "last": float("-inf")}


def explorer_slot(wait: bool) -> None:
    """Take the one Explorer slot that the sync and visitor searches share.
    wait=False raises ExplorerBusy instead of waiting."""
    with _explorer["lock"]:
        delay = EXPLORER_GAP - (time.monotonic() - _explorer["last"])
        if delay > 0:
            if not wait:
                raise ExplorerBusy
            time.sleep(delay)
        _explorer["last"] = time.monotonic()


def search_explorer(key: str) -> dict | None:
    """Find one swap by its deposit address, intent hash or a NEAR, origin or
    destination tx hash (Explorer `search`). Only an exact match counts."""
    explorer_slot(wait=False)
    query = urllib.parse.urlencode({"search": key, "numberOfTransactions": 5})
    # one try with the slot we hold: a retry would break the 1 request / 5 s limit
    for row in (
        get_json(f"{EXPLORER}/transactions?{query}", retries=1, gated=False) or []
    ):
        hashes = row.get("intentHashes") or []
        keys = {
            row.get("depositAddress"),
            *(hashes if isinstance(hashes, list) else [hashes]),
        }
        for field in (
            "nearTxHashes",
            "originChainTxHashes",
            "destinationChainTxHashes",
        ):
            keys.update(row.get(field) or [])
        if key in keys:
            return row
    return None


def get_tx_block(tx_hash: str) -> tuple[int, int] | None:
    """Get (block height, block time in ns) of a NEAR transaction, or None.

    The public RPC tracks all shards, so it finds the tx with any sender account.
    """
    tx = get_json(
        RPC,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tx",
            "params": {
                "tx_hash": tx_hash,
                "sender_account_id": "intents.near",
                "wait_until": "NONE",
            },
        },
        retries=2,
    )
    block_hash = tx.get("result", {}).get("transaction_outcome", {}).get("block_hash")
    if not block_hash:
        return None
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "block",
        "params": {"block_id": block_hash},
    }
    header = get_json(RPC, body, retries=2).get("result", {}).get("header", {})
    if "height" not in header:
        return None
    return header["height"], int(header["timestamp_nanosec"])


def iter_explorer_since(start: datetime, page_size: int = 1000, wait: float = 5.5):
    """Get all 1Click swaps created after `start`, newest first, one page at a time.

    Waits `wait` seconds between pages, because the API allows 1 request every 5 s.
    """
    params = {
        "numberOfTransactions": page_size,
        "startTimestamp": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    while True:
        page = get_explorer_txs(**params)
        if not page:
            return
        yield page
        if len(page) < page_size:
            return
        last = page[-1]
        params["lastDepositAddress"] = last["depositAddress"]
        if last.get("depositMemo"):
            params["lastDepositMemo"] = last["depositMemo"]
        else:
            params.pop("lastDepositMemo", None)
        time.sleep(wait)


def fee_settings() -> tuple[str, int]:
    """Get the DataBlock fee recipient and fee in bps. 1Click keeps half the fee."""
    return os.environ.get("FEE_RECIPIENT", "datablock.near"), int(
        os.environ.get("FEE_BPS", "40")
    )


def get_quote(request: dict, dry: bool = True, retries: int = 1) -> dict:
    """Get a 1Click quote with the DataBlock fee and referral.

    `request` holds the asset ids, amount and addresses: an earlier swap's quoteRequest
    ("Swap again") or a new swap from the signed-in user's own account.
    With dry=False, 1Click returns a deposit address. No funds move until the user
    sends to it.
    """
    recipient, bps = fee_settings()
    deadline = datetime.now(UTC) + timedelta(minutes=30)
    body = {
        "dry": dry,
        "swapType": "EXACT_INPUT",
        "slippageTolerance": 100,
        "depositType": "ORIGIN_CHAIN",
        # A recovery keeps the original recipient type; INTENTS means an intents.near balance.
        "recipientType": "INTENTS"
        if request.get("recipientType") == "INTENTS"
        else "DESTINATION_CHAIN",
        "refundType": "ORIGIN_CHAIN",
        "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
        **{
            k: request[k]
            for k in (
                "originAsset",
                "destinationAsset",
                "amount",
                "recipient",
                "refundTo",
            )
        },
    }
    body["referral"] = os.environ.get(
        "REFERRAL_ID", "intents-desk"
    )  # Explorer counts it
    if bps > 0:
        body["appFees"] = [{"recipient": recipient, "fee": bps}]
    return get_json(f"{ONECLICK}/quote", body, retries=retries)


def submit_deposit(address: str, tx_hash: str, sender: str) -> dict:
    """Tell 1Click the deposit is sent (optional; it makes the swap start sooner)."""
    body = {"depositAddress": address, "txHash": tx_hash, "nearSenderAccount": sender}
    return get_json(f"{ONECLICK}/deposit/submit", body, retries=1)


def rpc_query(params: dict) -> dict | None:
    """Run one NEAR RPC `query` call. None if the account or key does not exist."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "query", "params": params}
    result = get_json(RPC, body, retries=1).get("result")  # only web requests use this
    # An unknown key or account comes back as 200 with {"error": ...} inside "result".
    return None if not result or "error" in result else result


def get_access_key(account: str, public_key: str) -> dict | None:
    return rpc_query(
        {
            "request_type": "view_access_key",
            "finality": "final",
            "account_id": account,
            "public_key": public_key,
        }
    )


def get_account_txs(account: str, limit: int = 15) -> list[dict]:
    """Get the account's newest transactions (public chain data), newest first.

    Each item is FastNEAR's transaction record: `transaction` (signer, receiver, actions)
    and `execution_outcome` (block time), plus `is_success` from the account index.
    """
    index = get_json(
        f"{TXAPI}/account", {"account_id": account, "limit": limit}, retries=2
    ).get("account_txs", [])[:limit]
    ok = {t["transaction_hash"]: t.get("is_success", True) for t in index}
    if not ok:
        return []
    hashes, txs = list(ok), []
    for i in range(0, len(hashes), 20):  # the API takes at most 20 hashes per request
        found = get_json(f"{TXAPI}/transactions", {"tx_hashes": hashes[i : i + 20]}, retries=2)  # fmt: skip
        txs += [t for t in found.get("transactions", []) if t and "transaction" in t]
    for t in txs:
        t["is_success"] = ok.get(t["transaction"]["hash"], True)
    return sorted(txs, key=lambda t: -int(t["execution_outcome"]["block_timestamp"]))


def get_account(account: str) -> dict | None:
    return rpc_query(
        {"request_type": "view_account", "finality": "final", "account_id": account}
    )


INTENTS = "intents.near"


def intents_balances(account: str, token_ids: list[str]) -> dict[str, int]:
    """Get an account's token balances held inside intents.near (base units)."""
    args = base64.b64encode(
        json.dumps({"account_id": account, "token_ids": token_ids}).encode()
    ).decode()
    result = rpc_query(
        {
            "request_type": "call_function",
            "finality": "final",
            "account_id": INTENTS,
            "method_name": "mt_batch_balance_of",
            "args_base64": args,
        }
    )
    values = json.loads(bytes(result["result"])) if result else []
    return {t: int(v) for t, v in zip(token_ids, values, strict=False)}


# Feed of community posts about swap problems.
X_SEARCH = os.environ.get("X_SEARCH_URL", "https://api.x.com/2/tweets/search/recent")
X_QUERY = (
    '("near intents" OR 1click OR @near_intents) '
    "(stuck OR refund OR refunded OR failed OR support OR lost) -is:retweet lang:en"
)
REDDIT_RECENT = os.environ.get(
    "REDDIT_URL",
    "https://arctic-shift.photon-reddit.com/api/posts/search"
    "?subreddit=nearprotocol&limit=100&sort=desc",
)
FEED_TERMS = {
    "stuck": ("stuck", "pending", "lost", "missing", "never arrived"),
    "refund": ("refund",),
    "support": ("support", "ticket", "help"),
}


def tags_of(text: str) -> list[str]:
    low = text.lower()
    return [tag for tag, words in FEED_TERMS.items() if any(w in low for w in words)]


def get_x_posts(max_results: int = 10) -> list[dict]:
    """Get recent X posts about NEAR Intents swap problems. Needs X_BEARER_TOKEN."""
    query = urllib.parse.urlencode(
        {
            "query": X_QUERY,
            "max_results": max_results,
            "tweet.fields": "created_at,author_id",
            "expansions": "author_id",
            "user.fields": "username",
        }
    )
    req = urllib.request.Request(
        f"{X_SEARCH}?{query}",
        headers={
            **HEADERS,
            "Authorization": f"Bearer {os.environ['X_BEARER_TOKEN']}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    users = {u["id"]: u["username"] for u in data.get("includes", {}).get("users", [])}
    return [
        {
            "id": f"x:{t['id']}",
            "source": "x",
            "author": users.get(t["author_id"], ""),
            "created_at": t["created_at"],
            "text": t["text"],
            "url": f"https://x.com/{users.get(t['author_id'], 'i')}/status/{t['id']}",
            "tags": tags_of(t["text"]),
        }
        for t in data.get("data", [])
        if tags_of(t["text"])  # the X search also matches words in links and names
    ]


def get_reddit_posts() -> list[dict]:
    """Get recent r/nearprotocol posts that mention swap problems (Arctic Shift)."""
    posts = get_json(REDDIT_RECENT).get("data") or []
    out = []
    for p in posts:
        text = f"{p.get('title', '')}\n{p.get('selftext', '')}".strip()
        tags = tags_of(text)
        if not tags or ("swap" not in text.lower() and "intents" not in text.lower()):
            continue
        out.append(
            {
                "id": f"reddit:{p['id']}",
                "source": "reddit",
                "author": p.get("author", ""),
                "created_at": datetime.fromtimestamp(p["created_utc"], UTC).isoformat(),
                "text": text[:600],
                "url": f"https://www.reddit.com{p['permalink']}",
                "tags": tags,
            }
        )
    return out
