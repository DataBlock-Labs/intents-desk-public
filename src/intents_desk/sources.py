"""HTTP clients for the NEAR data sources and the 1Click swap API."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

NEARDATA = "https://mainnet.neardata.xyz/v0"
RPC = "https://rpc.mainnet.near.org"
ONECLICK = "https://1click.chaindefuser.com/v0"
EXPLORER = "https://explorer.near-intents.org/api/v0"  # needs INTENTS_EXPLORER_JWT
HEADERS = {"User-Agent": "intents-desk/0.1", "Content-Type": "application/json"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, so an API key in a header never follows a 30x to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def get_json(url: str, body: dict | None = None, retries: int = 6) -> dict:
    """Get JSON from a URL. Retry with backoff on rate limits and server errors."""
    data = json.dumps(body).encode() if body is not None else None
    headers = dict(HEADERS)
    # 1Click works without a key. Send the partner key (ONECLICK_JWT) when it is set.
    if url.startswith(ONECLICK) and os.environ.get("ONECLICK_JWT"):
        headers["Authorization"] = f"Bearer {os.environ['ONECLICK_JWT']}"
    if url.startswith(EXPLORER):
        headers["Authorization"] = f"Bearer {os.environ['INTENTS_EXPLORER_JWT']}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with _opener.open(req, timeout=30) as resp:
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
    """Get a 1Click quote that repeats an earlier swap request, with the DataBlock fee.

    `request` is the quoteRequest of an earlier swap (asset ids, amount, addresses).
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
        "recipientType": "DESTINATION_CHAIN",
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
    if bps > 0:
        body["appFees"] = [{"recipient": recipient, "fee": bps}]
    return get_json(f"{ONECLICK}/quote", body, retries=retries)


def rpc_query(params: dict) -> dict | None:
    """Run one NEAR RPC `query` call. None if the account or key does not exist."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "query", "params": params}
    return get_json(RPC, body, retries=1).get("result")  # only web requests use this


def get_access_key(account: str, public_key: str) -> dict | None:
    return rpc_query(
        {
            "request_type": "view_access_key",
            "finality": "final",
            "account_id": account,
            "public_key": public_key,
        }
    )


def get_account(account: str) -> dict | None:
    return rpc_query(
        {"request_type": "view_account", "finality": "final", "account_id": account}
    )


# Feed of community posts about swap problems.
X_SEARCH = "https://api.x.com/2/tweets/search/recent"
X_QUERY = (
    '("near intents" OR 1click OR @near_intents) '
    "(stuck OR refund OR refunded OR failed OR support OR lost) -is:retweet lang:en"
)
REDDIT_RECENT = (
    "https://arctic-shift.photon-reddit.com/api/posts/search"
    "?subreddit=nearprotocol&limit=100&sort=desc"
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
