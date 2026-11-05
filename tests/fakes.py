"""One fake server for every external API Intents Desk calls, for integration and E2E tests.

Paths: /neardata/v0/..., /explorer/transactions, /oneclick/status|quote, /rpc,
/x/search, /reddit. `env(base)` gives the variables that point the app at it.
neardata answers 429 with its real text on every `rate_limit_every`-th block request.
"""

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).parent / "fixtures"
HEAD = 1000
REFUNDED = "TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq"
USDT_TRON = "nep141:tron-d28a265909efecdcee7c5028585214ea0b96f015.omft.near"
USDT_ETH = "nep141:eth-0xdac17f958d2ee523a2206206994597c13d831ec7.omft.near"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def explorer_swaps(now: datetime) -> list[dict]:
    """120 swaps in the last hour (a failing ':pda' partner and a healthy one) plus one 30 h old."""
    rows = []
    for i in range(120):
        bad = i % 2 == 0
        rows.append(
            {
                "depositAddress": f"dep{i:04d}xxxx",
                "depositMemo": None,
                "createdAt": _iso(now - timedelta(seconds=25 * i)),
                "status": "REFUNDED" if bad else "SUCCESS",
                "referral": "widget-x:pda" if bad else "partner-b",
                "refundReason": "INTENT_SUBMIT_FAILED" if bad else None,
                "refundTo": "sender-a" if bad else f"user{i}",
                "recipient": "test.near" if i == 1 else f"r{i}",
                "originAsset": USDT_TRON,
                "destinationAsset": USDT_ETH,
                "amountInUsd": "100",
                "amountOutUsd": "99.9",
            }
        )
    old = dict(
        rows[1], depositAddress="old0000xxxx", createdAt=_iso(now - timedelta(hours=30))
    )
    return rows + [old]


STATUS = {
    "status": "REFUNDED",
    "updatedAt": "2026-11-04T13:55:46.000Z",
    "swapDetails": {
        "amountOutFormatted": "0",
        "refundedAmountFormatted": "15006.128946",
        "refundedAmountUsd": "14991.1",
        "refundReason": "QUOTE_EXPIRED",
    },
    "quoteResponse": {
        "quoteRequest": {
            "originAsset": USDT_TRON,
            "destinationAsset": USDT_ETH,
            "depositType": "ORIGIN_CHAIN",
            "amount": "15007000000",
            "recipient": "0xabc",
            "refundTo": "TRefund",
        },
        "quote": {"amountInFormatted": "15007.0", "amountInUsd": "14996.39"},
    },
}
QUOTE = {
    "quote": {
        "amountIn": "15007000000",
        "amountInFormatted": "15007.0",
        "amountInUsd": "15000",
        "amountOut": "14945252930",
        "amountOutFormatted": "14945.25293",
        "amountOutUsd": "14938",
        "minAmountOut": "14795800400",
        "timeEstimate": 87,
    }
}


class Fake:
    def __init__(self, rate_limit_every: int = 0):
        self.rate_limit_every = rate_limit_every
        self.block_requests = 0
        self.lock = threading.Lock()
        self.block = json.loads((FIXTURES / "block_intents.json").read_text())

    def handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body, kind="application/json", **headers):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlparse(self.path)
                q = parse_qs(url.query)
                p = url.path
                if p == "/neardata/v0/last_block/final":
                    self.reply(302, b"", Location=f"/neardata/v0/block/{HEAD}")
                elif p.startswith("/neardata/v0/block/"):
                    with fake.lock:
                        fake.block_requests += 1
                        n = fake.block_requests
                    if fake.rate_limit_every and n % fake.rate_limit_every == 0:
                        self.reply(
                            429,
                            {"error": "rate-limit exceeded of 30 blocks per minute"},
                        )
                        return
                    block = json.loads(json.dumps(fake.block))
                    block["block"]["header"]["height"] = int(p.rsplit("/", 1)[1])
                    self.reply(200, block)
                elif p == "/explorer/transactions":
                    n = int(q.get("numberOfTransactions", ["50"])[0])
                    self.reply(200, explorer_swaps(datetime.now(UTC))[:n])
                elif p == "/oneclick/status":
                    if q.get("depositAddress", [""])[0] == REFUNDED:
                        self.reply(200, STATUS)
                    else:
                        self.reply(404, {"message": "Deposit address not found"})
                elif p == "/x/search":
                    self.reply(
                        200,
                        {
                            "data": [
                                {
                                    "id": "1",
                                    "text": "4 days. My funds are still stuck <b>help</b>",
                                    "author_id": "u1",
                                    "created_at": _iso(datetime.now(UTC)),
                                }
                            ],
                            "includes": {
                                "users": [{"id": "u1", "username": "someone"}]
                            },
                        },
                    )
                elif p == "/reddit":
                    self.reply(200, {"data": []})
                else:
                    self.reply(404, {"error": "fake: no route"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                p = urlparse(self.path).path
                if p == "/oneclick/quote":
                    self.reply(200, QUOTE)
                elif p == "/rpc":
                    kind = body["params"]["request_type"]
                    if kind == "view_access_key":
                        result = {"nonce": 1, "permission": "FullAccess"}
                    else:
                        result = {"amount": str(5 * 10**24)}
                    self.reply(200, {"jsonrpc": "2.0", "id": 1, "result": result})
                else:
                    self.reply(404, {"error": "fake: no route"})

        return H

    def start(self) -> str:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def stop(self) -> None:
        self.server.shutdown()


def env(base: str) -> dict[str, str]:
    """Environment that points Intents Desk at the fake server."""
    return {
        "NEARDATA_URL": f"{base}/neardata/v0",
        "EXPLORER_URL": f"{base}/explorer",
        "ONECLICK_URL": f"{base}/oneclick",
        "NEAR_RPC_URL": f"{base}/rpc",
        "X_SEARCH_URL": f"{base}/x/search",
        "REDDIT_URL": f"{base}/reddit",
        "INTENTS_EXPLORER_JWT": "fake-explorer-key",
        "X_BEARER_TOKEN": "fake-x-key",
    }
