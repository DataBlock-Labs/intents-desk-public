"""Dashboard server: swap lookup, network and partner health, issue feed, wallet sign-in.

Two background threads keep the data fresh, so visitors never call a rate-limited API:
- sync: every 10 minutes, re-read the last 2 hours of swaps from the Explorer API;
- feed: every hour, read community posts from X (if a token is set) and Reddit.
"""

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, quote, urlparse

from . import auth, ops, sources, store

PRODUCT = "intents-desk"
ADDRESS = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
ACCOUNT = re.compile(r"^[a-z0-9._-]{2,64}$")
CACHE_SECONDS = 60
SYNC_SECONDS = 600
SYNC_HOURS = 2  # re-read this much before the newest stored swap (statuses change)
MAX_SYNC_HOURS = 24  # first run, or after a long downtime
RATE_PER_MIN = 30  # per client IP, for the routes in RATE_LIMITED
RATE_LIMITED = ("/api/swap", "/api/quote", "/api/me", "/api/auth/")
FEED_SECONDS = 3600
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
_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()
_db: dict = {"con": None}
_status: dict = {"synced_at": None, "feed_at": None, "feed_sources": []}


TOKENS_LINK = b'<link rel="stylesheet" href="/static/tokens.css">'


def page_html() -> bytes:
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
    return page


def db():
    """Get a DuckDB cursor for this thread. The server opens one shared connection."""
    if _db["con"] is None:
        _db["con"] = store.connect(_db.get("path") or "data/desk.duckdb")
    return _db["con"].cursor()


def chain_of(asset: str) -> str:
    """Get a short chain name from a 1Click asset id, e.g. nep141:tron-…omft.near."""
    if asset.startswith("1cs_v1:"):  # e.g. 1cs_v1:btc:native:coin
        return asset.split(":")[1]
    rest = asset.split(":", 1)[-1]
    if rest.endswith(".omft.near"):
        return rest.split("-")[0].split(".")[0]
    if "omni.hot.tg" in rest:
        return "hot-omni"
    if rest.endswith(".near"):
        return "near"
    return rest.split(".")[0]


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
        "balance_near": round(yocto / 10**24, 4),
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


def feed_once() -> int:
    cur = db()
    with ops.audited(cur, PRODUCT, "feed") as run:
        posts, used = sources.get_reddit_posts(), ["reddit"]
        if os.environ.get("X_BEARER_TOKEN"):
            posts += sources.get_x_posts(10)
            used.append("x")
        store.save_feed(cur, posts)
        run.update(rows=len(posts), sources=used)
    _status.update(feed_at=datetime.now(UTC).isoformat(timespec="seconds"))
    _status["feed_sources"] = used
    return len(posts)


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
        time.sleep(seconds)


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
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
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

    def route_get(self, path: str, q: dict) -> None:
        if path == "/":
            self.send(200, page_html(), "text/html; charset=utf-8")
        elif path in STATIC:  # fixed allow-list: no path from the URL reaches disk
            body = files("intents_desk").joinpath(path.lstrip("/")).read_bytes()
            self.send(200, body, STATIC[path], Cache_Control="max-age=3600")
        elif path == "/api/summary":
            self.send_json(200, {**get_summary(), **_status})
        elif path == "/api/partners":
            rows = store.partner_report(db(), 1, 50, 50).fetchall()
            self.send_json(200, rows[:15])
        elif path == "/api/hourly":
            self.send_json(200, store.hourly_refunds(db(), 24))
        elif path == "/api/feed":
            tag = q.get("tag", [None])[0]
            tag = tag if tag in sources.FEED_TERMS else None
            self.send_json(200, {"posts": store.read_feed(db(), tag), **_status})
        elif path in ("/api/swap", "/api/quote"):
            address = q.get("address", [""])[0].strip()
            if not ADDRESS.match(address):
                self.send_json(400, {"error": "Not a valid deposit address."})
            elif path == "/api/quote":
                self.send_json(*swap_again(q))
            elif q.get("format", [""])[0] == "agent":
                line = swap_view(address, "agent").encode()
                self.send(200, line, "text/plain; charset=utf-8")
            else:
                self.send_json(200, swap_view(address))
        elif path == "/api/auth/challenge":
            self.send_json(200, auth.new_challenge())
        elif path == "/api/me":
            account = self.account()
            # Signed out is a normal state, not an error: 200 with account null.
            self.send_json(200, account_view(account) if account else {"account": None})
        else:
            self.send_json(404, {"error": "Not found."})

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
            target=_loop, args=("sync", sync_once, SYNC_SECONDS), daemon=True
        ).start()
    threading.Thread(
        target=_loop, args=("feed", feed_once, FEED_SECONDS), daemon=True
    ).start()
    if os.environ.get("BACKUP_DIR") or os.environ.get("BACKUP_PUSH"):
        threading.Thread(
            target=_loop, args=("backup", backup_once, BACKUP_SECONDS), daemon=True
        ).start()
    host = host or os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Intents Desk on http://localhost:{port}  (Ctrl+C to stop)")
    server.serve_forever()
