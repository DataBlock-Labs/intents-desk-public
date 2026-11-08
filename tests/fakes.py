"""One fake server for every external API Intents Desk calls, for integration and E2E tests.

Paths: /neardata/v0/..., /explorer/transactions, /oneclick/status|quote, /rpc,
/x/search, /reddit. `env(base)` gives the variables that point the app at it.
neardata answers 429 with its real text on every `rate_limit_every`-th block request.
"""

import base64
import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).parent / "fixtures"
HEAD = 1000
REFUNDED = "TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq"
NEAR_REFUNDED = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
USDT_TRON = "nep141:tron-d28a265909efecdcee7c5028585214ea0b96f015.omft.near"
USDT_ETH = "nep141:eth-0xdac17f958d2ee523a2206206994597c13d831ec7.omft.near"
# public chain references of the STATUS swap (lookup by hash, case file)
TRON_TX = "9c1e5f00aa11bb22cc33dd44ee55ff6600778899aabbccddeeff001122334455"
NEAR_TX = "7nLmhrycnYcFrTFWm4aABUziPdCwRu9p3kBNR6Q9vf2n"
NEAR_BLOCK = 131_224_001


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
        "nearTxHashes": [NEAR_TX],
        "originChainTxHashes": [
            {
                "hash": TRON_TX,
                "explorerUrl": f"https://tronscan.org/#/transaction/{TRON_TX}",
            }
        ],
        "destinationChainTxHashes": [
            {"hash": "bad hash <x>", "explorerUrl": "javascript:alert(1)"}
        ],
    },
    "quoteResponse": {
        "timestamp": "2026-11-04T13:05:12.000Z",
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
# A refunded NEAR -> USDC swap whose refund came back to test.near (for Recover).
NEAR_STATUS = {
    "status": "REFUNDED",
    "updatedAt": "2026-11-05T10:00:00.000Z",
    "swapDetails": {
        "refundedAmount": str(2 * 10**24),
        "refundedAmountFormatted": "2",
        "refundReason": "QUOTE_EXPIRED",
    },
    "quoteResponse": {
        "quoteRequest": {
            "depositType": "ORIGIN_CHAIN",
            "originAsset": "nep141:wrap.near",
            "destinationAsset": "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1",
            "amount": str(2 * 10**24),
            "recipient": "test.near",
            "recipientType": "DESTINATION_CHAIN",
            "refundTo": "test.near",
            "refundType": "ORIGIN_CHAIN",
        },
        "quote": {
            "amountInFormatted": "2",
            "amountOutFormatted": "7.10",
            "amountOutUsd": "7.10",
        },
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


QUOTES: list[dict] = []  # every quote request body the fake received
SUBMITTED: list[dict] = []  # every deposit/submit body
USDC_NEAR = "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1"
PRICES = {"nep141:wrap.near": 3.73e-24, USDC_NEAR: 1e-6}


def _tx(h, signer, receiver, actions, minutes_ago):
    ns = int((datetime.now(UTC).timestamp() - 60 * minutes_ago) * 1e9)
    tx = {"hash": h, "signer_id": signer, "receiver_id": receiver, "actions": actions}
    return {"transaction": tx, "execution_outcome": {"block_timestamp": ns}}


def account_txs() -> dict[str, dict]:
    """alice.near: a 1Click NEAR deposit (refunded swap), a wrap, a storage deposit."""
    return {
        "TxDeposit1": _tx("TxDeposit1", "alice.near", NEAR_REFUNDED,
                          [{"Transfer": {"deposit": str(2 * 10**24)}}], 30),
        "TxWrap1": _tx("TxWrap1", "alice.near", "wrap.near", [{"FunctionCall": {
            "method_name": "near_deposit", "args": "e30=", "deposit": str(10**24)}}], 20),
        "TxStorage1": _tx("TxStorage1", "alice.near", "wrap.near", [{"FunctionCall": {
            "method_name": "storage_deposit", "args": "e30=", "deposit": "1250000000000000000000"}}], 21),
        **{f"TxSend{i}": _tx(f"TxSend{i}", "alice.near", "bob.near",
                             [{"Transfer": {"deposit": str(10**24)}}], 40 + i) for i in range(4)},
    }  # fmt: skip


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
                elif p == "/explorer/transactions" and "search" in q:
                    key = q["search"][0]
                    hit = key in (TRON_TX, NEAR_TX)
                    row = {"depositAddress": REFUNDED, "nearTxHashes": [NEAR_TX],
                           "originChainTxHashes": [TRON_TX]}  # fmt: skip
                    self.reply(200, [row] if hit else [])
                elif p == "/explorer/transactions":
                    n = int(q.get("numberOfTransactions", ["50"])[0])
                    self.reply(200, explorer_swaps(datetime.now(UTC))[:n])
                elif p == "/oneclick/status":
                    if q.get("depositAddress", [""])[0] == REFUNDED:
                        self.reply(200, STATUS)
                    elif q.get("depositAddress", [""])[0] == NEAR_REFUNDED:
                        self.reply(200, NEAR_STATUS)
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
                if p == "/txapi/account":
                    txs = (
                        account_txs() if body.get("account_id") == "alice.near" else {}
                    )
                    rows = [{"transaction_hash": h, "is_success": True} for h in txs]
                    self.reply(200, {"account_txs": rows})
                    return
                if p == "/txapi/transactions":
                    if len(body["tx_hashes"]) > 20:  # the real limit
                        self.reply(400, {"error": "Too many transactions requested. Limit is 20"})  # fmt: skip
                        return
                    txs = account_txs()
                    self.reply(200, {"transactions": [txs[h] for h in body["tx_hashes"] if h in txs]})  # fmt: skip
                    return
                if p == "/oneclick/quote":
                    QUOTES.append(body)  # tests read what we sent to 1Click
                    live = {"depositAddress": f"dep{len(QUOTES):05d}xxxx"}
                    q = {**QUOTE["quote"], **({} if body.get("dry") else live)}
                    price = PRICES.get(body.get("originAsset"))  # USD per base unit
                    if price is not None:  # 0 means 1Click has no USD price
                        usd = int(body["amount"]) * price
                        q["amountInUsd"] = str(usd) if price else None
                        q["amountIn"] = body["amount"]
                        unit = 10**24 if "wrap.near" in body["originAsset"] else 10**6
                        q["amountInFormatted"] = f"{int(body['amount']) / unit:g}"
                    self.reply(200, {"quote": q})
                elif p == "/oneclick/deposit/submit":
                    SUBMITTED.append(body)
                    self.reply(200, {"status": "PENDING_DEPOSIT"})
                elif p == "/rpc" and body.get("method") == "tx":
                    found = body["params"]["tx_hash"] == NEAR_TX
                    outcome = {"transaction_outcome": {"block_hash": "BlockHash1"}}
                    self.reply(200, {"jsonrpc": "2.0", "id": 1, "result": outcome} if found
                               else {"jsonrpc": "2.0", "id": 1, "error": {"name": "UNKNOWN_TRANSACTION"}})  # fmt: skip
                elif p == "/rpc" and body.get("method") == "block":
                    header = {
                        "height": NEAR_BLOCK,
                        "timestamp_nanosec": "1794000000000000000",
                    }
                    self.reply(
                        200, {"jsonrpc": "2.0", "id": 1, "result": {"header": header}}
                    )
                elif p == "/rpc":
                    kind = body["params"]["request_type"]
                    if kind == "view_access_key":
                        result = {"nonce": 1, "permission": "FullAccess"}
                    elif kind == "call_function":  # intents.near mt_batch_balance_of
                        ids = json.loads(
                            base64.b64decode(body["params"]["args_base64"])
                        )
                        values = [
                            str(1000 * (i + 1)) for i in range(len(ids["token_ids"]))
                        ]
                        result = {"result": list(json.dumps(values).encode())}
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
        "TXAPI_URL": f"{base}/txapi",
        "X_SEARCH_URL": f"{base}/x/search",
        "REDDIT_URL": f"{base}/reddit",
        "INTENTS_EXPLORER_JWT": "fake-explorer-key",
        "EXPLORER_GAP_S": "0",  # the fake has no rate limit
        "X_BEARER_TOKEN": "fake-x-key",
    }
