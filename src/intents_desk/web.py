"""Dashboard server: swap lookup, network and partner health, issue feed, wallet sign-in.

Two background threads keep the data fresh, so visitors never call a rate-limited API:
- sync: every 10 minutes, re-read the last 2 hours of swaps from the Explorer API;
- feed: every hour, read community posts from X (if a token is set) and Reddit.
"""

import base64
import gzip
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, quote, urlparse

from . import (
    activity,
    auth,
    incidents,
    ops,
    sources,
    store,
    support,
    telegram,
    tickets,
    xpost,
)

PRODUCT = "intents-desk"
ADDRESS = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
# A named NEAR account (alice.near, bob.tg): the lookup lists its wallet activity.
NEAR_ACCOUNT = re.compile(
    r"^(?=.{2,64}$)([a-z0-9]+[-_])*[a-z0-9]+(\.([a-z0-9]+[-_])*[a-z0-9]+)*\.(near|tg)$"
)
PARTNER = re.compile(r"^(\(none\)|[A-Za-z0-9._:-]{1,80})$")
ROUTE = re.compile(r"^[a-z0-9-]{1,20} → [a-z0-9-]{1,20}$")
REF = re.compile(r"^[0-9a-f]{20}$")
ACCOUNT = re.compile(r"^[a-z0-9._-]{2,64}$")
CACHE_SECONDS = 60
SYNC_SECONDS = 600
INCIDENT_SYNC_SECONDS = 120  # while an incident is open
STALE_MINUTES = 30  # no good sync for this long: tell the owner
SYNC_HOURS = 2  # re-read this much before the newest stored swap (statuses change)
MAX_SYNC_HOURS = 24  # first run, or after a long downtime
RATE_PER_MIN = int(
    os.environ.get("RATE_PER_MIN", "30")
)  # per client IP, RATE_LIMITED routes
RATE_LIMITED = (
    "/api/swap",
    "/api/quote",
    "/api/me",
    "/api/auth/",
    "/api/activity",
    "/api/ticket",
)
FEED_SECONDS = 3600
REVENUE_SECONDS = 300
BACKUP_SECONDS = 24 * 3600
MAX_BODY = 10_000
STATIC = {
    "/static/app.js": "text/javascript",
    "/static/near-connect.js": "text/javascript",
    "/static/tokens.css": "text/css",
    "/static/intents-desk-mark.svg": "image/svg+xml",
    "/static/near-logo.svg": "image/svg+xml",
}
# NEAR Connect loads its wallet list and wallet code from GitHub and jsDelivr and runs
# each wallet in a sandboxed iframe; wallets then call their own HTTPS APIs.
CSP = (
    "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net"
    " https://raw.githubusercontent.com; style-src 'self' 'unsafe-inline';"
    " img-src 'self' data: https:; connect-src 'self' https:; frame-src 'self' https:"
    " blob: data:; object-src 'none'; base-uri 'none'; form-action 'self';"
    " frame-ancestors 'none'"
)


def page_csp(nonce: str) -> str:
    """Get the policy for the page itself.

    NEAR Connect runs wallet code as inline scripts in srcdoc iframes, which inherit
    this policy: they need the nonce. Wallet bridges use websockets (wss:), and wallet
    panels load fonts and stylesheets from their own hosts.
    """
    return (
        CSP.replace("script-src 'self'", f"script-src 'self' 'nonce-{nonce}'")
        .replace("connect-src 'self' https:", "connect-src 'self' https: wss:")
        .replace(
            "style-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline' https:",
        )
        + "; font-src 'self' data: https:"
    )


_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()
_db: dict = {"con": None}
_status: dict = {"synced_at": None, "feed_at": None, "feed_sources": []}


TOKENS_LINK = b'<link rel="stylesheet" href="/static/tokens.css">'


def page_html(nonce: str = "") -> bytes:
    """Get index.html with tokens.css and the two logos inlined (bytes unchanged).

    This saves three requests and the render-blocking stylesheet.
    """
    pkg = files("intents_desk")
    tokens = pkg.joinpath("static/tokens.css").read_bytes()
    page = pkg.joinpath("index.html").read_bytes()
    page = page.replace(TOKENS_LINK, b"<style>" + tokens + b"</style>", 1)
    for name in ("intents-desk-mark.svg", "near-logo.svg"):  # same bytes, no request
        data = base64.b64encode(pkg.joinpath(f"static/{name}").read_bytes())
        page = page.replace(
            f"/static/{name}".encode(), b"data:image/svg+xml;base64," + data
        )
    app = pkg.joinpath("static/app.js").read_bytes()  # new code reaches phones at once
    version = hashlib.sha256(app).hexdigest()[:10].encode()
    page = page.replace(b'"/static/app.js"', b'"/static/app.js?v=' + version + b'"', 1)
    if (
        nonce
    ):  # the page's own script carries the nonce; app.js hands it to NEAR Connect
        page = page.replace(
            b'<script type="module"',
            f'<script type="module" nonce="{nonce}"'.encode(),
            1,
        )
    return page


def db():
    """Get a DuckDB cursor for this thread. The server opens one shared connection."""
    if _db["con"] is None:
        _db["con"] = store.connect(_db.get("path") or "data/desk.duckdb")
    return _db["con"].cursor()


def mask_partner(partner: str, force: bool = False) -> str:
    """Give a stable public alias for a referral id when MASK_PARTNERS=1 (or force).

    Partner names stay private until the partner has been told (AGENTS.md).
    """
    if not (force or os.environ.get("MASK_PARTNERS") == "1") or partner == "(none)":
        return partner
    key = store._ref_secret() + partner
    return "partner-" + hashlib.sha256(key.encode()).hexdigest()[:6]


def unmask_partner(alias: str) -> str | None:
    """Find the referral id behind a public alias. None when no partner matches."""
    if os.environ.get("MASK_PARTNERS") != "1" or alias == "(none)":
        return alias
    rows = (
        db()
        .execute(
            "SELECT DISTINCT coalesce(nullif(referral, ''), '(none)') FROM silver.swap"
        )
        .fetchall()
    )
    return next((p for (p,) in rows if mask_partner(p) == alias), None)


def partner_rows() -> list[list]:
    """The partner table: the hourly alert rule, plus a daily rule for slow failures."""
    rows = [list(r) for r in store.partner_report(db(), 1, 50, 50).fetchall()[:15]]
    daily = {r[0] for r in store.partner_report(db(), 24, 100, 20).fetchall() if r[7]}
    for r in rows:
        r[7] = bool(r[7]) or r[0] in daily
        r[0] = mask_partner(r[0])
    return rows


def chain_of(asset: str) -> str:
    """Get a short chain name from a 1Click asset id, e.g. nep141:tron-…omft.near."""
    if asset.startswith("1cs_v1:"):  # e.g. 1cs_v1:btc:native:coin
        return asset.split(":")[1]
    rest = asset.split(":", 1)[-1]
    if rest.endswith(".omft.near"):
        return rest.split("-")[0].split(".")[0]
    if "omni.hot.tg" in rest:
        return "hot-omni"
    return "near"  # any other token id lives on NEAR (wrap.near, token.sweat, hex ids)


def summarize(txs: list[dict]) -> dict:
    """Make the dashboard numbers from one page of Explorer swaps."""
    n = len(txs)
    status = Counter(t["status"] for t in txs)
    refunds = [t for t in txs if t["status"] == "REFUNDED"]
    routes: Counter = Counter()
    route_refunds: Counter = Counter()
    for t in txs:
        route = f"{chain_of(t['originAsset'])} → {chain_of(t['destinationAsset'])}"
        routes[route] += 1
        route_refunds[route] += t["status"] == "REFUNDED"
    times = sorted(t["createdAt"] for t in txs)
    return {
        "swaps": n,
        "first": times[0] if times else None,
        "last": times[-1] if times else None,
        "usd_in": round(sum(float(t.get("amountInUsd") or 0) for t in txs)),
        "status": [[s, c, round(100 * c / n, 1)] for s, c in status.most_common()],
        "refund_reasons": Counter(
            t.get("refundReason") or "not given" for t in refunds
        ).most_common(),
        "routes": [
            [r, c, round(100 * route_refunds[r] / c, 1)]
            for r, c in routes.most_common(10)
        ],
    }


def get_summary() -> dict:
    """Get the summary of the newest 1,000 swaps, cached for CACHE_SECONDS.

    It reads the local database. With an empty database it calls the Explorer API once
    if a key is set; with neither, it returns an empty summary.
    """
    with _lock:
        if _cache["data"] is None or time.time() - _cache["at"] > CACHE_SECONDS:
            txs = store.newest_raw_swaps(db())
            if not txs and os.environ.get("INTENTS_EXPLORER_JWT"):
                txs = sources.get_explorer_txs(numberOfTransactions=1000)
            # No data and no key (a fresh clone): an empty summary, not an error.
            _cache.update(at=time.time(), data=summarize(txs))
        return _cache["data"]


def swap_view(address: str, fmt: str = "json"):
    """Get the 1Click status of one swap. fmt="agent" gives one short line of text."""
    data = sources.get_swap_status(quote(address), retries=1)
    if fmt != "agent":
        return data
    if "status" not in data:
        return f"not_found address={address}"
    req = data.get("quoteResponse", {}).get("quoteRequest", {})
    q = data.get("quoteResponse", {}).get("quote", {})
    d = data.get("swapDetails", {})
    route = f"{chain_of(req.get('originAsset', '?'))}→"
    route += chain_of(req.get("destinationAsset", "?"))
    parts = {
        "status": data["status"],
        "route": route,
        "in": q.get("amountInFormatted"),
        "in_usd": (q.get("amountInUsd") or "")[:10] or None,
        "out": d.get("amountOutFormatted"),
        "refunded": d.get("refundedAmountFormatted"),
        "reason": d.get("refundReason"),
        "updated": data.get("updatedAt"),
    }
    return " ".join(f"{k}={v}" for k, v in parts.items() if v not in (None, "", "0"))


def case_of(data: dict, ref: str, full: bool = False) -> dict:
    """Build, save and count the support case of one 1Click status reply.
    full=True only when the visitor typed the deposit address (see support.view)."""
    import duckdb

    cur = db()
    case = support.build(cur, data, ref)
    try:
        if support.save(cur, case):
            support.log_lookup(cur, case)
    except duckdb.TransactionException:  # the same row written by a parallel request
        pass
    return support.view(cur, case, full)


def lookup(key: str) -> tuple[int, dict]:
    """Find a swap by its deposit address, else by a tx or intent hash (Explorer).
    A swap found by hash gets the reduced view: the visitor never typed its address."""
    data = swap_view(key)
    if "status" in data:
        return 200, data | {"case": case_of(data, store.ref_of(key), full=True)}
    if not support.HASH.match(key):  # account names, memos: never a tx hash
        return 404, {"error": "1Click does not know this address."}
    try:
        row = sources.search_explorer(key)
    except sources.ExplorerBusy:
        return 429, {"error": "Busy: try the hash again in 5 seconds."}
    except (OSError, KeyError, ValueError, RuntimeError):  # no Explorer key, or down
        row = None
    found = swap_view(row["depositAddress"]) if row else {}
    if "status" not in found:
        return 404, {"error": "1Click does not know this address or hash."}
    ref = store.ref_of(row["depositAddress"])
    return 200, public_view(found) | {"case": case_of(found, ref), "by_hash": True}


def public_view(data: dict) -> dict:
    """The parts of a 1Click status that are safe to show for a swap opened by ref:
    no deposit, recipient or refund address, and no memo."""
    req = (data.get("quoteResponse") or {}).get("quoteRequest") or {}
    q = (data.get("quoteResponse") or {}).get("quote") or {}
    d = data.get("swapDetails") or {}
    keep = lambda src, keys: {k: src[k] for k in keys if k in src}
    return {
        "status": data.get("status"),
        "updatedAt": data.get("updatedAt"),
        "quoteResponse": {
            "quoteRequest": keep(
                req, ("originAsset", "destinationAsset", "depositType")
            ),
            "quote": keep(q, ("amountInFormatted", "amountInUsd")),
        },
        "swapDetails": keep(
            d,
            (
                "amountOutFormatted",
                "amountOutUsd",
                "refundedAmountFormatted",
                "refundedAmountUsd",
                "refundReason",
            ),
        ),
    }


def swap_again(query: dict) -> tuple[int, dict]:
    """Quote a refunded swap again, with the same route, amount and addresses.

    Only ORIGIN_CHAIN swaps can be repeated: the user deposits again from their own
    wallet. Pass dry=0 to get a deposit address.
    """
    address = query.get("address", [""])[0].strip()
    if not ADDRESS.match(address):
        return 400, {"error": "Not a valid deposit address."}
    status = sources.get_swap_status(quote(address), retries=1)
    request = status.get("quoteResponse", {}).get("quoteRequest", {})
    if status.get("status") != "REFUNDED":
        return 400, {"error": "Only refunded swaps can be swapped again."}
    if request.get("depositType") != "ORIGIN_CHAIN":
        return 400, {"error": "This swap started inside NEAR Intents. Use your wallet."}
    dry = query.get("dry", ["1"])[0] != "0"
    if not dry and os.environ.get("SWAP_LIVE") != "1":
        return 403, {"error": "Preview only for now: no deposit addresses yet."}
    recipient, bps = sources.fee_settings()
    result = sources.get_quote(request, dry=dry)
    result["fee"] = {"recipient": recipient, "bps": bps}
    return 200, result


# New swaps from the signed-in account: two NEAR tokens, small amounts while we test.
SWAP_ASSETS = {
    "NEAR": ("nep141:wrap.near", 24),
    "USDC": (
        "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",
        6,
    ),
}
AMOUNT = re.compile(r"^[0-9]{1,12}(\.[0-9]{1,24})?$")


MAX_LIVE_QUOTES_PER_DAY = 20
TX_HASH = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{43,44}$")  # NEAR tx hash, base58


def swap_cap_usd() -> float:
    # shortcut: a $25 cap while we test with real money; raise it before the public launch.
    return float(os.environ.get("SWAP_MAX_USD", "25"))


def base_units(amount: str, symbol: str) -> str:
    """Convert "1.5" NEAR to base units as an integer string. Refuse odd input."""
    if symbol not in SWAP_ASSETS or not AMOUNT.match(amount):
        raise ValueError("Use an amount like 1 or 0.5.")
    decimals = SWAP_ASSETS[symbol][1]
    value = Decimal(amount)
    if value <= 0:
        raise ValueError("Amount must be above 0.")
    units = value.scaleb(decimals)
    if units != units.to_integral_value():
        raise ValueError(f"{symbol} has at most {decimals} decimals.")
    return str(int(units))


def capped_quote(request: dict, dry: bool) -> tuple[int, dict]:
    """Quote with the fee; refuse above the USD cap before any deposit address exists."""
    preview = sources.get_quote(request, dry=True)
    try:
        usd = float((preview.get("quote") or {}).get("amountInUsd") or 0)
    except (TypeError, ValueError):
        usd = 0.0
    if not 0 < usd <= swap_cap_usd():  # no price (or NaN) fails closed
        return 400, {
            "error": f"About ${usd:,.2f}: no price, or above the ${swap_cap_usd():.0f}"
            " limit."
        }
    return 200, preview if dry else sources.get_quote(request, dry=False)


def quote_reply(account: str, request: dict, dry: bool, kind: str) -> tuple[int, dict]:
    """Quote (capped), keep live deposit addresses in app.quote, and shape the reply."""
    recipient, bps = sources.fee_settings()
    if not dry and store.live_quotes_today(db(), account) >= MAX_LIVE_QUOTES_PER_DAY:
        return 429, {"error": "Too many deposit addresses today. Try again tomorrow."}
    code, result = capped_quote(request, dry)
    if code != 200:
        return code, result
    address = (result.get("quote") or {}).get("depositAddress")
    if not dry and address:
        store.log_quote(db(), account, address, request, bps, kind)
    return 200, {
        "quote": result.get("quote") or {},
        "fee": {"recipient": recipient, "bps": bps},
        "dry": dry,
        "origin": request["originAsset"],
    }


def live_allowed(dry: bool) -> bool:
    return dry or os.environ.get("SWAP_LIVE") == "1"


def new_swap(account: str, body: dict) -> tuple[int, dict]:
    """Quote a swap from the signed-in account to itself, with the DataBlock fee.

    Refunds go back to the same account. dry=False (a deposit address) needs SWAP_LIVE=1.
    """
    src, dst = str(body.get("from", "")), str(body.get("to", ""))
    if src not in SWAP_ASSETS or dst not in SWAP_ASSETS or src == dst:
        return 400, {"error": "Pick two different tokens: NEAR or USDC."}
    try:
        amount = base_units(str(body.get("amount", "")).strip(), src)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    dry = body.get("dry", True) is not False
    if not live_allowed(dry):
        return 403, {"error": "Preview only for now: no deposit addresses yet."}
    request = {
        "originAsset": SWAP_ASSETS[src][0],
        "destinationAsset": SWAP_ASSETS[dst][0],
        "amount": amount,
        "recipient": account,
        "refundTo": account,
    }
    return quote_reply(account, request, dry, "swap")


def recover(account: str, address: str, dry: bool) -> tuple[int, dict]:
    """Re-do a refunded swap at today's price, from the account the refund went to.

    Only for refunds paid back on NEAR (an nep141 token) to the signed-in account, so
    the user can send the deposit from their wallet. The original recipient is kept.
    """
    if not ADDRESS.match(address):
        return 400, {"error": "Not a valid deposit address."}
    status = sources.get_swap_status(quote(address), retries=1)
    original = status.get("quoteResponse") or {}
    req = original.get("quoteRequest") or {}
    if status.get("status") != "REFUNDED":
        return 400, {"error": "Only refunded swaps can be recovered."}
    # Anyone can make a 1Click quote that refunds to you and pays them. So recover only
    # swaps that paid this same account, between our two tokens, for the refunded amount.
    if req.get("refundTo") != account or req.get("recipient") != account:
        return 403, {
            "error": "Only swaps to and from your own account can be recovered."
        }
    tokens = {t for t, _ in SWAP_ASSETS.values()}
    origin, dest = req.get("originAsset"), req.get("destinationAsset")
    if req.get("refundType", "ORIGIN_CHAIN") != "ORIGIN_CHAIN" or origin not in tokens:
        return 400, {"error": "This refund went to another chain: use Swap again."}
    refunded = str((status.get("swapDetails") or {}).get("refundedAmount") or "")
    if dest not in tokens or dest == origin or not refunded.isdigit():
        return 400, {"error": "This swap cannot be recovered here."}
    if not live_allowed(dry):
        return 403, {"error": "Preview only for now: no deposit addresses yet."}
    request = {
        "originAsset": origin,
        "destinationAsset": dest,
        "amount": refunded,
        "recipient": account,
        "refundTo": account,
    }
    code, reply = quote_reply(account, request, dry, "recover")
    if code == 200:
        then = original.get("quote") or {}
        reply["then"] = {
            "amountOutFormatted": then.get("amountOutFormatted"),
            "amountOutUsd": then.get("amountOutUsd"),
        }
    return code, reply


def revenue_once() -> int:
    """Settle our quotes: on SUCCESS record our share of the fee in USD (revenue)."""
    cur, n = db(), 0
    me = sources.fee_settings()[0]
    with ops.audited(cur, PRODUCT, "revenue") as run:
        for address in store.open_quotes(cur):
            status = sources.get_swap_status(quote(address), retries=1)
            if not status.get("status"):
                continue
            response = status.get("quoteResponse") or {}
            fees = (response.get("quoteRequest") or {}).get("appFees") or []
            bps = next((f.get("fee") for f in fees if f.get("recipient") == me), None)
            usd = (status.get("swapDetails") or {}).get("amountInUsd") or (
                response.get("quote") or {}
            ).get("amountInUsd")
            store.settle_quote(
                cur, address, status["status"], float(usd) if usd else None, bps
            )
            n += 1
        run["rows"] = n
    return n


ACTIVITY_SECONDS = 60
AUTOWATCH_SECONDS = 300
_activity: dict[
    str, tuple[float, list]
] = {}  # shortcut: per-process cache; fine for one server


def account_activity(account: str, fresh: bool = False) -> list[dict]:
    """The account's newest transactions in plain words; deposits get their 1Click status."""
    hit = _activity.get(account)
    if hit and not fresh and time.time() - hit[0] < ACTIVITY_SECONDS:
        return hit[1]
    txs = sources.get_account_txs(account, limit=50)
    lines = [activity.describe(t, account) for t in txs]
    for line in lines:  # one status name for every row, as in the swap tables
        line["status"] = "SUCCESS" if line["ok"] else "FAILED"
        line.update(
            route=None, in_usd=None, amount_in=None, amount_out=None, reason=None
        )
    for line in [x for x in lines if x["deposit"]][:15]:  # at most 15 status calls
        data = sources.get_swap_status(line["deposit"], retries=1)
        status = data.get("status")
        line["swap_status"] = status  # None: not a 1Click deposit address
        if not status:
            continue
        req = (data.get("quoteResponse") or {}).get("quoteRequest") or {}
        quote_ = (data.get("quoteResponse") or {}).get("quote") or {}
        details = data.get("swapDetails") or {}
        line.update(
            kind="swap",
            text=line["text"].replace("deposit address", "1Click swap"),
            status=status,
            route=f"{chain_of(req.get('originAsset', '?'))} → {chain_of(req.get('destinationAsset', '?'))}",
            in_usd=details.get("amountInUsd") or quote_.get("amountInUsd"),
            amount_in=details.get("amountInFormatted")
            or quote_.get("amountInFormatted"),
            amount_out=details.get("amountOutFormatted")
            or details.get("refundedAmountFormatted"),
            reason=details.get("refundReason"),
        )
    if len(_activity) > 500:  # public lookups: keep the cache small
        _activity.clear()
    _activity[account] = (time.time(), lines)
    return lines


def autowatch_once() -> int:
    """Watch new 1Click swaps of Telegram-linked accounts, and say so once.

    Only swaps from the last 24 h are added. The message carries the status at the time,
    because a fast swap can finish before the alert loop first checks it.
    """
    cur, added = db(), 0
    accounts = cur.execute(
        "SELECT account_id, chat_id FROM app.telegram WHERE coalesce(auto_watch, true)"
    ).fetchall()
    day_ago = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    for account, chat in accounts:
        watched = set(store.watch_list(cur, account))
        try:
            lines = account_activity(account, fresh=True)
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
        ) as exc:  # one account never blocks the rest
            print(f"[autowatch] {account}: {type(exc).__name__}", file=sys.stderr)
            continue
        for line in lines:
            address, status = line["deposit"], line.get("swap_status")
            # Only successful transactions signed by this account count.
            if (
                not status
                or not line["ok"]
                or address in watched
                or line["when"] < day_ago
            ):
                continue
            try:
                store.set_watch(cur, account, address, True)
            except ValueError:  # at the watch limit
                break
            cur.execute(
                "UPDATE app.watch SET last_status = ? WHERE account_id = ? AND deposit_address = ?",
                [status, account, address],
            )
            done = status in ("SUCCESS", "REFUNDED", "FAILED")
            tail = "It is finished." if done else "Watching it for you."
            telegram.send(chat, f"New swap from {account}: {line['text']}. Status: {status}. {tail}")  # fmt: skip
            added += 1
    return added


def account_view(account: str) -> dict:
    """Get the signed-in user's balance, stored swaps and watched swaps."""
    cur = db()
    info = sources.get_account(account) or {}
    yocto = int(info.get("amount", 0))
    watched = []
    for address in store.watch_list(cur, account)[:10]:
        watched.append({"address": address, "line": swap_view(address, "agent")})
    return {
        "account": account,
        "telegram": {
            "available": telegram.enabled(),
            "linked": telegram.linked_chat(cur, account) is not None,
            "auto_watch": telegram.auto_watch(cur, account),
            "bot": telegram.bot_url(),
        },
        "balance_near": round(yocto / 10**24, 4),
        "swap_live": os.environ.get("SWAP_LIVE") == "1",
        "quotes": [
            {"address": a, "line": swap_view(a, "agent")}
            for a in store.quote_list(cur, account)
        ],
        "swaps": store.account_swaps(cur, account),
        "watched": watched,
    }


def sync_start(newest: datetime | None, now: datetime) -> datetime:
    """Re-read from 2 h before the newest stored swap; at most the last 24 h."""
    oldest = now - timedelta(hours=MAX_SYNC_HOURS)
    return max(newest - timedelta(hours=SYNC_HOURS), oldest) if newest else oldest


def sync_once() -> int:
    cur, n = db(), 0
    with ops.audited(cur, PRODUCT, "sync") as run:
        start = sync_start(store.newest_swap_time(cur), datetime.now(UTC))
        for page in sources.iter_explorer_since(start):
            n += store.load_explorer_txs(cur, page)
        run["rollup_rows"] = ops.rollup_swaps(
            cur
        )  # keep the daily numbers before the purge
        run["purged"] = store.purge_api_data(
            cur
        )  # 1Click API terms 4.1(e): at most 24 h
        run["rows"] = n
    _cache.update(at=0.0, data=None)  # show the new data at once, not after 60 s
    _status["synced_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return n


_incident_cache: dict = {"at": 0.0, "data": None}


def incident_list() -> dict:
    """The incidents table and data gaps, cached for CACHE_SECONDS (gaps scan the rollup)."""
    if (
        _incident_cache["data"] is None
        or time.time() - _incident_cache["at"] > CACHE_SECONDS
    ):
        rows = [incidents.public(i) for i in incidents.listing(db())]
        for i in rows:
            i.pop("evidence")
        _incident_cache.update(
            at=time.time(), data={"incidents": rows, "gaps": incidents.gaps(db())}
        )
    return _incident_cache["data"]


def incidents_once() -> int:
    cur = db()
    with ops.audited(cur, PRODUCT, "incidents") as run:
        run.update(incidents.run(cur, send_drafts=telegram.enabled()))
    _incident_cache["data"] = None
    return run["rows"]


def sync_and_incidents() -> int:
    n = sync_once()
    incidents_once()
    return n


def sync_seconds() -> int:
    return INCIDENT_SYNC_SECONDS if incidents.open_count(db()) else SYNC_SECONDS


def stale_once() -> int:
    """Tell the owner once when the sync has not worked for STALE_MINUTES."""
    (age,) = (
        db()
        .execute(
            "SELECT epoch(now()) - epoch(max(finished_at)) FROM ops.run_log"
            " WHERE job = 'sync' AND status = 'ok'"
        )
        .fetchone()
    )
    stale = age is None or age > STALE_MINUTES * 60
    if stale and not _status.get("stale_sent"):
        chat = incidents.owner_chat(db())
        if chat is not None:
            minutes = "never" if age is None else f"{int(age // 60)} min ago"
            telegram.send(chat, f"Intents Desk: no good swap sync (last: {minutes}).")
            _status["stale_sent"] = True
    elif not stale:
        _status["stale_sent"] = False
    return int(stale)


def watch_once() -> int:
    """Every 5 min: the stale-sync alert, and one alert per reason whose support
    lookups jump in an hour (often before the 50-swap incident rule fires)."""
    stale = stale_once()
    chat = None
    for reason, n in support.spikes(db()):
        key = (datetime.now(UTC).strftime("%Y-%m-%dT%H"), reason)
        if key in _status.setdefault("spikes_sent", set()):
            continue
        chat = chat or incidents.owner_chat(db())
        if chat is not None:
            telegram.send(chat, f"Intents Desk: {n} support lookups for {reason} this hour.")  # fmt: skip
            _status["spikes_sent"].add(key)  # shortcut: in memory, a restart may resend
    return stale


def feed_once() -> int:
    cur = db()
    with ops.audited(cur, PRODUCT, "feed") as run:
        posts, used = sources.get_reddit_posts(), ["reddit"]
        if os.environ.get("X_BEARER_TOKEN"):
            posts += sources.get_x_posts(10)
            used.append("x")
        store.save_feed(cur, posts)
        opened = tickets_from_posts(cur, posts)
        run.update(rows=len(posts), sources=used, tickets=opened)
    _status.update(feed_at=datetime.now(UTC).isoformat(timespec="seconds"))
    _status["feed_sources"] = used
    return len(posts)


FEED_LOOKUPS = 5  # swap keys checked per feed run (1Click and the Explorer slot)


def tickets_from_posts(con, posts: list[dict]) -> int:
    """Open a public ticket for each swap a feed post names; draft an X reply for the owner."""
    budget, opened = FEED_LOOKUPS, 0
    for post in posts:
        source = "x" if str(post.get("id", "")).startswith("x:") else "reddit"
        url = post.get("url")
        if not url or con.execute("SELECT 1 FROM app.ticket WHERE source_url = ?", [url]).fetchone():  # fmt: skip
            continue
        for key in tickets.keys_in(post.get("text", "")):
            if budget <= 0:
                return opened
            budget -= 1
            ticket = tickets.report(con, key, source, url)
            if not ticket or not ticket["new"]:
                continue
            opened += 1
            telegram.channel_post(tickets.channel_text(ticket))
            chat = incidents.owner_chat(con) if source == "x" else None
            if chat is not None:
                offer_reply(con, chat, ticket, str(post["id"]).removeprefix("x:"))
            break  # one ticket per post
    return opened


def offer_reply(con, chat: int, ticket: dict, post_id: str) -> None:
    reason = f" ({ticket['reason']})" if ticket["reason"] else ""
    head = f"We checked this NEAR Intents swap: {ticket['status']}{reason}. "
    tail = f" Public ticket {ticket['ref'][:8]} on Intents Desk."
    answer = ticket["answer"][: xpost.MAX_CHARS - len(head) - len(tail)]
    try:
        xpost.offer(con, chat, "reply", head + answer + tail, tickets.link(ticket["ref"]), post_id)  # fmt: skip
    except ValueError:  # a post id that is not numeric, or text with a link
        pass


TICKET_SECONDS = 600


def tickets_once() -> int:
    """Re-check unfinished public tickets; post each status change to the channel."""
    cur = db()
    with ops.audited(cur, PRODUCT, "tickets") as run:
        changes = tickets.refresh(cur)
        for change in changes:
            case = support.saved(cur, change["ref"]) or {}
            telegram.channel_post(tickets.channel_text({"ref": change["ref"], "route": case.get("route")}, change))  # fmt: skip
        run["rows"] = len(changes)
    return len(changes)


def backup_once() -> int:
    """Daily backup of the ops and chain tables; pushed to BACKUP_PUSH (host:/path) if set."""
    cur = db()
    with ops.audited(cur, PRODUCT, "backup") as run:
        folder = ops.backup(cur, PRODUCT, os.environ.get("BACKUP_DIR", "data/backups"))
        problems = ops.verify_backup(folder)
        if os.environ.get("BACKUP_PUSH") and not problems:
            problems = ops.push(folder, os.environ["BACKUP_PUSH"])
            run["pushed_to"] = os.environ["BACKUP_PUSH"]
        run.update(rows=1, path=str(folder), problems=problems)
        if problems:
            raise RuntimeError(f"backup check failed: {problems}")
    return 1


ALERT_SECONDS = 120


def alerts_once() -> int:
    cur = db()
    with ops.audited(cur, PRODUCT, "alerts") as run:
        run.update(telegram.alerts_once(cur))
    return run["sent"]


def _bot_loop() -> None:
    """Long-poll Telegram for /start, /status and /stop. Errors never print the token."""
    offset = 0
    try:
        telegram.set_commands()
    except telegram.TelegramError:
        pass  # the menu is a convenience; the commands work without it
    while True:
        try:
            offset = telegram.poll_once(db(), offset)
        except (telegram.TelegramError, OSError, ValueError, RuntimeError) as exc:
            print(f"[bot] failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(10)


def _loop(name: str, fn, seconds: int) -> None:
    while True:
        try:
            print(f"[{name}] {fn()} rows", file=sys.stderr, flush=True)
        except (
            OSError,
            ValueError,
            RuntimeError,
            KeyError,
            subprocess.SubprocessError,
        ) as exc:
            print(f"[{name}] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        time.sleep(seconds() if callable(seconds) else seconds)


_buckets: dict[str, tuple[float, float]] = {}  # client ip -> (tokens, last time)
_bucket_lock = threading.Lock()


def allow(ip: str, now: float | None = None) -> bool:
    """Token bucket: RATE_PER_MIN requests a minute per client IP."""
    now = time.time() if now is None else now
    with _bucket_lock:
        if len(_buckets) > 50_000:  # drop idle clients so the dict cannot grow forever
            for k in [k for k, (_, t) in _buckets.items() if now - t > 600]:
                del _buckets[k]
        tokens, last = _buckets.get(ip, (RATE_PER_MIN, now))
        tokens = min(RATE_PER_MIN, tokens + (now - last) * RATE_PER_MIN / 60)
        if tokens < 1:
            _buckets[ip] = (tokens, now)
            return False
        _buckets[ip] = (tokens - 1, now)
        return True


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # a client that sends nothing for 10 s is dropped, not kept forever

    def client_ip(self) -> str:
        # Behind Fly's proxy (TRUST_PROXY=1) the socket address is the proxy.
        if os.environ.get("TRUST_PROXY") == "1":
            return self.headers.get("Fly-Client-IP") or self.client_address[0]
        return self.client_address[0]

    def limited(self, path: str) -> bool:
        if path.startswith(RATE_LIMITED) and not allow(self.client_ip()):
            self.send_json(429, {"error": "Too many requests. Wait a minute."})
            return True
        return False

    def send(self, code: int, body: bytes, kind: str = "application/json", **hdr):
        if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, 6)  # about 3x smaller for JS, JSON and HTML
            hdr["Content_Encoding"] = "gzip"
        hdr["Vary"] = "Accept-Encoding"
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", hdr.pop("csp", CSP))
        if not auth.PUBLIC_HOST.startswith(("localhost", "127.0.0.1")):
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for name, value in hdr.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code: int, data, **hdr) -> None:
        self.send(code, json.dumps(data).encode(), **hdr)

    def account(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "session":
                return auth.read_session(value)
        return None

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if self.limited(url.path):
            return
        try:
            self.route_get(url.path, q)
        except KeyError as exc:
            self.send_json(500, {"error": f"Missing setting: {exc}"})
        except (OSError, ValueError, RuntimeError) as exc:  # HTTP, JSON, retries.
            self.send_json(502, {"error": f"{type(exc).__name__}: {exc}"})

    def from_page(self) -> bool:
        """True for a fetch from our own page, or when BROWSER_ONLY is not 1.

        shortcut: a header check stops reuse as an API, not a client that fakes headers;
        move to a signed page token if someone does that.
        """
        if os.environ.get("BROWSER_ONLY") != "1":
            return True
        return self.headers.get("Sec-Fetch-Site") == "same-origin"

    def route_get(self, path: str, q: dict) -> None:
        if path == "/":
            nonce = base64.b64encode(secrets.token_bytes(16)).decode()  # fresh per page
            self.send(
                200, page_html(nonce), "text/html; charset=utf-8", csp=page_csp(nonce)
            )
        elif path in STATIC:  # fixed allow-list: no path from the URL reaches disk
            body = files("intents_desk").joinpath(path.lstrip("/")).read_bytes()
            self.send(200, body, STATIC[path], Cache_Control="max-age=3600")
        elif path == "/api/summary":
            self.send_json(200, {**get_summary(), **_status})
        elif path == "/api/partners":
            self.send_json(200, partner_rows())
        elif path == "/api/partner":
            alias = q.get("id", [""])[0]
            partner = unmask_partner(alias) if PARTNER.match(alias) else None
            if not PARTNER.match(alias):
                self.send_json(400, {"error": "Not a valid partner id."})
            elif partner is None:
                self.send_json(404, {"error": "Partner not found."})
            else:
                detail = store.partner_detail(db(), partner)
                self.send_json(200, {**detail, "partner": alias})
        elif path == "/api/swaps":
            status = q.get("status", [None])[0] or None
            partner = q.get("partner", [None])[0] or None
            route = q.get("route", [None])[0] or None
            try:
                limit = min(max(int(q.get("limit", ["100"])[0]), 1), 200)
            except ValueError:
                limit = 100
            if (
                (status and status not in store.SWAP_STATUSES)
                or (partner and not PARTNER.match(partner))
                or (route and not ROUTE.match(route))
            ):
                self.send_json(400, {"error": "Bad filter."})
            elif partner and (partner := unmask_partner(partner)) is None:
                self.send_json(200, [])
            else:
                rows = store.recent_swaps(db(), status, partner, route, limit)
                for r in rows:
                    r["partner"] = mask_partner(r["partner"])
                self.send_json(200, rows)
        elif path == "/api/incidents":
            self.send_json(200, incident_list())
        elif path == "/api/incident":
            inc = incidents.find_public(db(), q.get("id", [""])[0][:200])
            if inc is None:
                self.send_json(404, {"error": "Incident not found."})
            else:
                self.send_json(200, inc)
        elif path == "/api/asks":
            self.send_json(200, {"asks": support.asks(db())})
        elif path == "/api/hourly":
            self.send_json(200, store.hourly_refunds(db(), 24))
        elif path == "/api/feed":
            tag = q.get("tag", [None])[0]
            tag = tag if tag in sources.FEED_TERMS else None
            self.send_json(200, {"posts": store.read_feed(db(), tag), **_status})
        elif path in ("/api/swap", "/api/quote") and not self.from_page():
            # 1Click terms 4.1(a): no public API, proxy or wrapper over 1Click.
            self.send_json(403, {"error": "Use the page, or the 1Click API directly."})
        elif path in ("/api/swap", "/api/quote"):
            address = q.get("address", [""])[0].strip()
            ref = q.get("ref", [""])[0]
            if path == "/api/swap" and ref and REF.match(ref):  # list or case link
                address = store.address_for_ref(db(), ref) or ""
                data = swap_view(address) if address else {}
                if "status" in data:  # reduced view: the reply never holds an address
                    self.send_json(
                        200, public_view(data) | {"case": case_of(data, ref)}
                    )
                elif old := support.saved(db(), ref):  # 1Click data gone: our copy
                    self.send_json(200, {"case": support.view(db(), old, full=False), "saved": True})  # fmt: skip
                else:  # 1Click's not-found text holds the address: never echo it
                    self.send_json(404, {"error": "1Click does not know this swap."})
                return
            if not ADDRESS.match(address):
                self.send_json(
                    400, {"error": "Not a valid deposit address or tx hash."}
                )
            elif path == "/api/quote":
                self.send_json(*swap_again(q))
            elif q.get("format", [""])[0] == "agent":
                line = swap_view(address, "agent").encode()
                self.send(200, line, "text/plain; charset=utf-8")
            else:
                self.send_json(*lookup(address))
        elif path == "/api/auth/challenge":
            self.send_json(200, auth.new_challenge())
        elif path == "/api/tickets":
            self.send_json(200, {"tickets": tickets.listing(db())})
        elif path == "/api/activity":
            account = q.get("account", [""])[0].strip().lower()
            if not NEAR_ACCOUNT.match(account):
                self.send_json(400, {"error": "Not a NEAR account name (for example alice.near)."})  # fmt: skip
            else:
                self.activity_reply(account)
        elif path == "/api/me/activity":
            account = self.account()
            if not account:
                self.send_json(401, {"error": "Not signed in."})
            else:
                self.activity_reply(account)
        elif path == "/api/me":
            account = self.account()
            # Signed out is a normal state, not an error: 200 with account null.
            self.send_json(200, account_view(account) if account else {"account": None})
        else:
            self.send_json(404, {"error": "Not found."})

    def activity_reply(self, account: str) -> None:
        try:
            self.send_json(
                200, {"account": account, "activity": account_activity(account)}
            )
        except (OSError, RuntimeError, ValueError, KeyError):
            self.send_json(502, {"error": "Chain history is not available right now."})

    def do_POST(self) -> None:
        if self.limited(urlparse(self.path).path):
            return
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin", "")
        length = self.headers.get("Content-Length") or "0"
        size = (
            int(length) if length.isascii() and length.isdigit() else -1
        )  # "-1" or "abc" are refused
        if origin not in (f"http://{host}", f"https://{host}"):
            self.send_json(403, {"error": "Cross-site request refused."})
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.send_json(415, {"error": "Send JSON."})
            return
        if not 0 <= size <= MAX_BODY:
            self.send_json(413, {"error": "Request too large."})
            return
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
            self.route_post(urlparse(self.path).path, body)
        except auth.AuthError as exc:
            self.send_json(401, {"error": str(exc)})
        except (OSError, ValueError, RuntimeError, AttributeError) as exc:
            self.send_json(400, {"error": f"{type(exc).__name__}: {exc}"})

    def swap_followup(self, path: str, body: dict) -> None:
        """POST /api/me/recover and /api/me/swap/sent (the user sent the deposit)."""
        account = self.account()
        address = str(body.get("address", "")).strip()
        tx_hash = str(body.get("txHash", ""))
        if not account:
            self.send_json(401, {"error": "Not signed in."})
        elif path.endswith("recover"):
            dry = body.get("dry", True) is not False
            self.send_json(*recover(account, address, dry))
        elif store.quote_owner(db(), address) != account:
            self.send_json(404, {"error": "Not one of your swaps."})
        elif not TX_HASH.match(tx_hash):
            self.send_json(400, {"error": "Not a NEAR transaction hash."})
        else:
            store.set_quote_tx(db(), address, tx_hash)
            try:  # optional: 1Click also finds the deposit on its own
                sources.submit_deposit(address, tx_hash, account)
                submitted = True
            except (OSError, ValueError, RuntimeError):
                submitted = False
            self.send_json(200, {"ok": True, "submitted": submitted})

    def route_post(self, path: str, body: dict) -> None:
        if path == "/api/auth/verify":
            signed = body.get("signed") or {}
            if not ACCOUNT.match(str(signed.get("accountId", ""))):
                raise auth.AuthError("Not a valid NEAR account id.")
            account = auth.verify(
                signed, str(body.get("nonce", "")), sources.get_access_key
            )
            cookie = (
                f"session={auth.make_session(account)}; HttpOnly; Secure; SameSite=Strict;"
                f" Path=/; Max-Age={auth.SESSION_SECONDS}"
            )
            self.send_json(200, {"account": account}, Set_Cookie=cookie)
        elif path == "/api/auth/logout":
            cookie = "session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0"
            self.send_json(200, {"ok": True}, Set_Cookie=cookie)
        elif path == "/api/ticket":  # "Report publicly" on a case
            key = str(body.get("key", "")).strip()
            if len(key) > 128 or not tickets.KEY.fullmatch(key):
                self.send_json(400, {"error": "Not a deposit address or tx hash."})
                return
            ticket = tickets.report(db(), key, "web")
            if not ticket:
                self.send_json(404, {"error": "1Click does not know this address or hash."})  # fmt: skip
                return
            if ticket["new"]:
                telegram.channel_post(tickets.channel_text(ticket))
            self.send_json(200, {"ref": ticket["ref"], "new": ticket["new"], "link": tickets.link(ticket["ref"])})  # fmt: skip
        elif path == "/api/me/telegram/autowatch":
            account = self.account()
            if not account:
                self.send_json(401, {"error": "Not signed in."})
            else:
                on = bool(body.get("on"))
                db().execute("UPDATE app.telegram SET auto_watch = ? WHERE account_id = ?", [on, account])  # fmt: skip
                self.send_json(200, {"auto_watch": on})
        elif path in ("/api/me/telegram", "/api/me/telegram/unlink"):
            account = self.account()
            if not account:
                self.send_json(401, {"error": "Not signed in."})
            elif not telegram.enabled():
                self.send_json(
                    503, {"error": "Telegram alerts are not set up on this server."}
                )
            elif path.endswith("unlink"):
                db().execute("DELETE FROM app.telegram WHERE account_id = ?", [account])
                self.send_json(200, {"linked": False})
            else:
                self.send_json(200, {"link": telegram.new_link(db(), account)})
        elif path == "/api/me/swap":
            account = self.account()
            if not account:
                self.send_json(401, {"error": "Not signed in."})
            else:
                self.send_json(*new_swap(account, body))
        elif path in ("/api/me/swap/sent", "/api/me/recover"):
            self.swap_followup(path, body)
        elif path == "/api/me/watch":
            account = self.account()
            address = str(body.get("address", "")).strip()
            if not account:
                self.send_json(401, {"error": "Not signed in."})
            elif not ADDRESS.match(address):
                self.send_json(400, {"error": "Not a valid deposit address."})
            else:
                store.set_watch(db(), account, address, bool(body.get("on", True)))
                self.send_json(200, {"watched": store.watch_list(db(), account)})
        else:
            self.send_json(404, {"error": "Not found."})


def serve(
    port: int = 8420, host: str | None = None, db_path: str | None = None
) -> None:
    """Run the dashboard on localhost until Ctrl+C, with the sync and feed threads."""
    _db["path"] = db_path
    db()  # open the database before the threads start
    if os.environ.get("INTENTS_EXPLORER_JWT"):
        threading.Thread(
            target=_loop, args=("sync", sync_and_incidents, sync_seconds), daemon=True
        ).start()
        if telegram.enabled():
            threading.Thread(
                target=_loop, args=("stale", watch_once, 300), daemon=True
            ).start()
    threading.Thread(
        target=_loop, args=("feed", feed_once, FEED_SECONDS), daemon=True
    ).start()
    if telegram.enabled():
        threading.Thread(target=_bot_loop, daemon=True).start()
        threading.Thread(
            target=_loop,
            args=("autowatch", autowatch_once, AUTOWATCH_SECONDS),
            daemon=True,
        ).start()
        threading.Thread(
            target=_loop, args=("tickets", tickets_once, TICKET_SECONDS), daemon=True
        ).start()
        threading.Thread(
            target=_loop, args=("alerts", alerts_once, ALERT_SECONDS), daemon=True
        ).start()
    if os.environ.get("BACKUP_DIR") or os.environ.get("BACKUP_PUSH"):
        threading.Thread(
            target=_loop, args=("backup", backup_once, BACKUP_SECONDS), daemon=True
        ).start()
    host = host or os.environ.get("HOST", "127.0.0.1")
    threading.Thread(
        target=_loop, args=("revenue", revenue_once, REVENUE_SECONDS), daemon=True
    ).start()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Intents Desk on http://localhost:{port}  (Ctrl+C to stop)")
    server.serve_forever()
