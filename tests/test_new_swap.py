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
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{server.server_port}"
    server.shutdown()
    fake.stop()


def post(host, body, account="alice.near"):
    headers = {"Content-Type": "application/json", "Origin": f"http://{host}"}
    if account:
        headers["Cookie"] = f"session={auth.make_session(account)}"
    req = urllib.request.Request(
        f"http://{host}/api/me/swap", json.dumps(body).encode(), headers
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
        ({"from": "NEAR", "to": "USDC", "amount": "6"}, 400),  # over the test cap
        ({"from": "USDC", "to": "NEAR", "amount": "1.0000001"}, 400),
        ({"from": "NEAR", "to": "USDC", "amount": "1e3"}, 400),
        ({"from": "NEAR", "to": "USDC", "amount": "1", "dry": False}, 403),
    ],
)
def test_bad_input_and_live_without_switch_are_refused(app, body, code):
    assert post(app, body)[0] == code
    assert fakes.QUOTES == []  # nothing reached 1Click


def test_signed_out_is_refused(app):
    assert post(app, {"from": "NEAR", "to": "USDC", "amount": "1"}, None)[0] == 401


def test_live_quote_is_logged_once_and_shown_in_my_swaps(app, monkeypatch):
    monkeypatch.setenv("SWAP_LIVE", "1")
    code, d = post(app, {"from": "USDC", "to": "NEAR", "amount": "2.5", "dry": False})
    assert code == 200 and d["quote"]["depositAddress"] == "dep00001xxxx"
    assert fakes.QUOTES[-1]["amount"] == "2500000"
    con = web.db()
    rows = con.execute(
        "SELECT account_id, amount_in, fee_bps FROM app.quote"
    ).fetchall()
    assert rows == [("alice.near", "2500000", 40)]
    assert store.quote_list(con, "alice.near") == ["dep00001xxxx"]
    assert store.quote_list(con, "bob.near") == []


def test_intents_balances_reads_the_batch_answer(app):
    got = sources.intents_balances("datablock.near", ["nep141:wrap.near", "usdc"])
    assert got == {"nep141:wrap.near": 1000, "usdc": 2000}
