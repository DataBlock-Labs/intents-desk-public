"""New swaps from the signed-in account: fee, referral, gates and the quote log."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from intents_desk import auth, sources, store, web
from tests import fakes


@pytest.fixture
def app(tmp_path, monkeypatch):
    fake = fakes.Fake()
    base = fake.start()
    monkeypatch.setattr(sources, "ONECLICK", f"{base}/oneclick")
    monkeypatch.setattr(sources, "RPC", f"{base}/rpc")
    monkeypatch.setenv("SESSION_KEY_FILE", str(tmp_path / "s.key"))
    monkeypatch.delenv("SWAP_LIVE", raising=False)
    monkeypatch.setattr(web, "_db", {"con": store.connect(tmp_path / "t.duckdb")})
    web._buckets.clear()
    fakes.QUOTES.clear()
    fakes.SUBMITTED.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_port}"
    server.shutdown()
    fake.stop()


def post(host, body, account="alice.near", path="/api/me/swap"):
    headers = {"Content-Type": "application/json", "Origin": f"http://{host}"}
    if account:
        headers["Cookie"] = f"session={auth.make_session(account)}"
    req = urllib.request.Request(
        f"http://{host}{path}", json.dumps(body).encode(), headers
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_preview_sends_fee_and_referral_and_swaps_to_the_same_account(app):
    code, d = post(app, {"from": "NEAR", "to": "USDC", "amount": "1"})
    assert code == 200 and d["dry"] is True and d["fee"]["bps"] == 40
    sent = fakes.QUOTES[-1]
    assert sent["appFees"] == [{"recipient": "datablock.near", "fee": 40}]
    assert sent["referral"] == "intents-desk"
    assert sent["originAsset"] == "nep141:wrap.near"
    assert sent["amount"] == str(10**24)
    assert sent["recipient"] == sent["refundTo"] == "alice.near"
    assert "depositAddress" not in d["quote"]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"from": "NEAR", "to": "NEAR", "amount": "1"}, 400),
        ({"from": "NEAR", "to": "BTC", "amount": "1"}, 400),
        ({"from": "NEAR", "to": "USDC", "amount": "7"}, 400),  # $26 > the $25 cap
        ({"from": "USDC", "to": "NEAR", "amount": "1.0000001"}, 400),
        ({"from": "NEAR", "to": "USDC", "amount": "1e3"}, 400),
        ({"from": "NEAR", "to": "USDC", "amount": "1", "dry": False}, 403),
    ],
)
def test_bad_input_and_live_without_switch_are_refused(app, body, code):
    assert post(app, body)[0] == code
    assert not any(q["dry"] is False for q in fakes.QUOTES)  # no deposit address made


def test_signed_out_is_refused(app):
    assert post(app, {"from": "NEAR", "to": "USDC", "amount": "1"}, None)[0] == 401


def test_live_quote_is_logged_once_and_shown_in_my_swaps(app, monkeypatch):
    monkeypatch.setenv("SWAP_LIVE", "1")
    code, d = post(app, {"from": "USDC", "to": "NEAR", "amount": "2.5", "dry": False})
    addr = d["quote"]["depositAddress"]
    assert code == 200 and addr.startswith("dep")
    assert [q["dry"] for q in fakes.QUOTES] == [True, False]  # cap check, then live
    assert fakes.QUOTES[-1]["amount"] == "2500000"
    con = web.db()
    rows = con.execute(
        "SELECT account_id, amount_in, fee_bps FROM app.quote"
    ).fetchall()
    assert rows == [("alice.near", "2500000", 40)]
    assert store.quote_list(con, "alice.near") == [addr]
    assert store.quote_list(con, "bob.near") == []


def test_intents_balances_reads_the_batch_answer(app):
    got = sources.intents_balances("datablock.near", ["nep141:wrap.near", "usdc"])
    assert got == {"nep141:wrap.near": 1000, "usdc": 2000}


TX = "8kY6pnEhGKNrDhX8kCWt8rfm8ZRZx4yNHjDSU7r1vAZt"  # base58, 44 chars


def test_sent_only_for_your_own_quote_and_a_real_hash(app, monkeypatch):
    monkeypatch.setenv("SWAP_LIVE", "1")
    d = post(app, {"from": "NEAR", "to": "USDC", "amount": "1", "dry": False})[1]
    addr = d["quote"]["depositAddress"]
    sent = "/api/me/swap/sent"
    assert post(app, {"address": addr, "txHash": TX}, "bob.near", sent)[0] == 404
    assert post(app, {"address": addr, "txHash": "0xabc"}, path=sent)[0] == 400
    assert post(app, {"address": addr, "txHash": TX}, None, sent)[0] == 401
    code, r = post(app, {"address": addr, "txHash": TX}, path=sent)
    assert code == 200 and r["submitted"] is True
    assert fakes.SUBMITTED == [
        {"depositAddress": addr, "txHash": TX, "nearSenderAccount": "alice.near"}
    ]
    row = web.db().execute("SELECT tx_hash FROM app.quote").fetchone()
    assert row == (TX,)


def refunded(
    refund_to="alice.near",
    origin="nep141:wrap.near",
    status="REFUNDED",
    recipient="alice.near",
    refunded_amount=str(2 * 10**24),
):
    return {
        "status": status,
        "quoteResponse": {
            "quoteRequest": {
                "originAsset": origin,
                "destinationAsset": fakes.USDC_NEAR,
                "amount": str(2 * 10**24),
                "recipient": recipient,
                "recipientType": "DESTINATION_CHAIN",
                "refundTo": refund_to,
                "refundType": "ORIGIN_CHAIN",
            },
            "quote": {"amountOutFormatted": "7.10", "amountOutUsd": "7.10"},
        },
        "swapDetails": {"refundedAmount": refunded_amount},
    }


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (refunded(refund_to="mallory.near"), 403),
        (refunded(status="SUCCESS"), 400),
        (refunded(origin="tron:usdt"), 400),
        # an attacker's quote: refunds to you, pays them
        (refunded(recipient="attacker.near"), 403),
        (refunded(origin="nep141:evil.near"), 400),  # not one of our tokens
        (refunded(refunded_amount=None), 400),  # no refunded amount: no fallback
    ],
)
def test_recover_refuses_what_is_not_yours_or_not_on_near(
    app, monkeypatch, status, code
):
    monkeypatch.setattr(sources, "get_swap_status", lambda a, **k: status)
    assert post(app, {"address": "dep0refund01xx"}, path="/api/me/recover")[0] == code
    assert fakes.QUOTES == []


def test_recover_quotes_today_for_the_refunded_amount_to_the_first_recipient(
    app, monkeypatch
):
    monkeypatch.setattr(sources, "get_swap_status", lambda a, **k: refunded())
    code, d = post(app, {"address": "dep0refund01xx"}, path="/api/me/recover")
    assert code == 200 and d["then"]["amountOutFormatted"] == "7.10"
    sent = fakes.QUOTES[-1]
    assert sent["amount"] == str(2 * 10**24)
    assert sent["recipient"] == sent["refundTo"] == "alice.near"
    assert sent["appFees"] == [{"recipient": "datablock.near", "fee": 40}]


def test_revenue_records_our_fee_on_success_only(app, monkeypatch):
    con = web.db()
    req = {"originAsset": "a", "destinationAsset": "b", "amount": "1"}
    for addr in ("depOK0001xxxx", "depBAD001xxxx"):
        store.log_quote(con, "alice.near", addr, req, 40)
    answers = {
        "depOK0001xxxx": {
            "status": "SUCCESS",
            "quoteResponse": {
                "quoteRequest": {
                    "appFees": [
                        {"recipient": "datablock.near", "fee": 20},
                        {"recipient": "5880ad2b", "fee": 20},
                    ]
                }
            },
            "swapDetails": {"amountInUsd": "3.74"},
        },
        "depBAD001xxxx": {"status": "REFUNDED"},
    }
    monkeypatch.setattr(sources, "get_swap_status", lambda a, **k: answers[a])
    assert web.revenue_once() == 2
    rows = dict(
        con.execute("SELECT deposit_address, fee_usd FROM app.quote").fetchall()
    )
    assert rows == {"depOK0001xxxx": pytest.approx(0.00748), "depBAD001xxxx": 0.0}
    day = con.execute("SELECT swaps, fee_usd FROM ops.daily_revenue").fetchall()
    assert day == [(1, pytest.approx(0.00748))]
    job = con.execute("SELECT status FROM ops.run_log WHERE job = 'revenue'").fetchall()
    assert job == [("ok",)]
    assert store.open_quotes(con) == []  # both final: not checked again


def test_cap_fails_closed_without_a_price(app, monkeypatch):
    monkeypatch.setitem(fakes.PRICES, "nep141:wrap.near", 0)  # 1Click has no USD price
    code, d = post(app, {"from": "NEAR", "to": "USDC", "amount": "1"})
    assert code == 400 and "no price" in d["error"]


def test_at_most_20_live_quotes_per_account_a_day(app, monkeypatch):
    monkeypatch.setenv("SWAP_LIVE", "1")
    monkeypatch.setattr(web, "MAX_LIVE_QUOTES_PER_DAY", 2)
    body = {"from": "NEAR", "to": "USDC", "amount": "1", "dry": False}
    assert [post(app, body)[0] for _ in range(3)] == [200, 200, 429]
    assert post(app, body, "bob.near")[0] == 200  # other accounts are not affected
