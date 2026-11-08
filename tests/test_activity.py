import base64
import json

import pytest

from intents_desk import activity, auth, sources, store, telegram, web
from tests import fakes
from tests.test_new_swap import app, post  # noqa: F401  (the app fixture)

ME = "alice.near"
DEP = "a" * 64


def item(signer, receiver, actions, h="H1"):
    tx = {"hash": h, "signer_id": signer, "receiver_id": receiver, "actions": actions}
    return {
        "transaction": tx,
        "execution_outcome": {"block_timestamp": 1794104611 * 10**9},
    }


def call(method, args, deposit="0"):
    raw = base64.b64encode(json.dumps(args).encode()).decode()
    return {"FunctionCall": {"method_name": method, "args": raw, "deposit": deposit}}


@pytest.mark.parametrize(
    ("signer", "receiver", "actions", "kind", "text"),
    [
        (ME, "wrap.near", [call("near_deposit", {}, str(10**24))], "wrap", "Wrapped 1 NEAR into wNEAR (1:1)"),
        (ME, "wrap.near", [call("near_withdraw", {"amount": str(10**24)})], "wrap", "Unwrapped 1 NEAR wNEAR into NEAR"),
        (ME, DEP, [{"Transfer": {"deposit": str(2 * 10**24)}}], "deposit", "Sent 2 NEAR to deposit address aaaaaaaa…"),
        (ME, "usdt.tether-token.near", [call("storage_deposit", {}), call("ft_transfer", {"receiver_id": DEP, "amount": "5000000"})], "deposit", "Sent 5 USDT to deposit address aaaaaaaa…"),
        (ME, "wrap.near", [call("ft_transfer_call", {"receiver_id": "intents.near", "amount": str(10**24)})], "intents", "Deposited 1 wNEAR into NEAR Intents"),
        (ME, "wrap.near", [call("ft_transfer_call", {"receiver_id": "v2.ref-finance.near", "amount": str(10**24)})], "dex", "Swapped 1 wNEAR on Rhea"),
        (ME, "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1", [call("storage_deposit", {})], "setup", "Registered with USDC (one-time storage fee)"),
        ("bob.near", ME, [{"Transfer": {"deposit": str(3 * 10**24)}}], "received", "Received 3 NEAR from bob.near"),
        ("relayer.near", ME, [{"Delegate": {"delegate_action": {"sender_id": ME, "receiver_id": "wrap.near", "actions": [call("near_deposit", {}, str(10**24))]}}}], "wrap", "Wrapped 1 NEAR into wNEAR (1:1) (gas paid by a relayer)"),
    ],
)  # fmt: skip
def test_describe(signer, receiver, actions, kind, text):
    line = activity.describe(item(signer, receiver, actions), ME)
    assert (line["kind"], line["text"]) == (kind, text)
    assert line["deposit"] == (DEP if kind == "deposit" else None)


def test_describe_intents_withdraw_to_me():
    msg = json.dumps({"signer_id": "solver.near", "intents": [{"intent": "native_withdraw", "receiver_id": ME, "amount": str(10**24)}]})  # fmt: skip
    line = activity.describe(item("intents.near", "intents.near", [call("execute_intents", {"signed": [{"payload": {"message": msg}}]})]), ME)  # fmt: skip
    assert line["text"] == "NEAR Intents: sent 1 NEAR to this account"


def get(host, path, account=ME):
    import urllib.error
    import urllib.request

    cookie = {"Cookie": f"session={auth.make_session(account)}"} if account else {}
    req = urllib.request.Request(f"http://{host}{path}", headers=cookie)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


@pytest.fixture
def txapi(app, monkeypatch):  # noqa: F811
    base = sources.ONECLICK.rsplit("/", 1)[0]
    monkeypatch.setattr(sources, "TXAPI", f"{base}/txapi")
    web._activity.clear()
    return app


def test_activity_endpoint_lists_and_checks_deposits(txapi):
    code, d = get(txapi, "/api/me/activity")
    assert code == 200
    texts = [a["text"] for a in d["activity"]]
    assert (
        texts[0].startswith("Sent 1 NEAR") or "Wrapped 1 NEAR into wNEAR (1:1)" in texts
    )
    swap = next(a for a in d["activity"] if a["deposit"] == fakes.NEAR_REFUNDED)
    assert (
        swap["kind"] == "swap" and swap["swap_status"] == swap["status"] == "REFUNDED"
    )
    assert swap["route"] == "near → near" and swap["amount_in"] == "2"
    assert swap["reason"] == "QUOTE_EXPIRED"
    wrap = next(a for a in d["activity"] if a["kind"] == "wrap")
    assert (
        wrap["status"] == "SUCCESS" and wrap["route"] is None
    )  # same names as the swap tables
    assert swap["text"].startswith("Sent 2 NEAR to 1Click swap")
    assert get(txapi, "/api/me/activity", account=None)[0] == 401


def test_autowatch_watches_new_swaps_once_and_tells_telegram(txapi, monkeypatch):
    con = web.db()
    con.execute("INSERT INTO app.telegram (account_id, chat_id) VALUES (?, 42)", [ME])
    sent = []
    monkeypatch.setattr(telegram, "send", lambda chat, text: sent.append((chat, text)))
    assert web.autowatch_once() == 1
    assert store.watch_list(con, ME) == [fakes.NEAR_REFUNDED]
    assert sent and sent[0][0] == 42 and "REFUNDED" in sent[0][1]
    status = con.execute("SELECT last_status FROM app.watch").fetchall()
    assert status == [("REFUNDED",)]  # the alert loop will not message it again
    assert web.autowatch_once() == 0  # already watched

    con.execute("DELETE FROM app.watch")
    code, d = post(txapi, {"on": False}, path="/api/me/telegram/autowatch")
    assert code == 200 and d == {"auto_watch": False}
    assert web.autowatch_once() == 0  # turned off


@pytest.mark.parametrize(
    "actions",
    [
        [call("ft_transfer", {"receiver_id": DEP, "amount": "not-a-number"})],
        [call("ft_transfer", {"receiver_id": ["a", "list"], "amount": "1"})],
        [call("execute_intents", {"signed": [{"payload": {"message": "[1, 2]"}}]})],
        [
            {
                "FunctionCall": {
                    "method_name": "ft_transfer",
                    "args": "e30=",
                    "deposit": "x",
                }
            }
        ],
        ["not-an-object", 7],
    ],
)
def test_describe_never_raises_on_hostile_args(actions):
    receiver = (
        "intents.near"
        if actions[0] and isinstance(actions[0], dict) and "execute" in str(actions[0])
        else "usdt.tether-token.near"
    )
    line = activity.describe(item(ME, receiver, actions), ME)
    assert isinstance(line["text"], str) and line["text"]
    assert line["deposit"] in (None, DEP)


def test_failed_relayed_tx_is_not_ours():
    relayed = [{"Delegate": {"delegate_action": {"sender_id": ME, "receiver_id": "x.near", "actions": [{"Transfer": {"deposit": str(10**24)}}]}}}]  # fmt: skip
    it = item("relayer.near", ME, relayed)
    it["is_success"] = False
    line = activity.describe(it, ME)
    assert line["deposit"] is None and "relayer" in line["text"]


def test_public_account_lookup_lists_activity(txapi):
    code, d = get(
        txapi, "/api/activity?account=Alice.near", account=None
    )  # case folded
    assert code == 200 and d["account"] == "alice.near"
    assert any(a.get("swap_status") == "REFUNDED" for a in d["activity"])
    for bad in ("alice", "0xabc.near.", "a" * 64, "alice.near;drop"):
        assert get(txapi, f"/api/activity?account={bad}", account=None)[0] == 400


def test_account_txs_are_fetched_in_batches_of_20(monkeypatch):
    calls = []

    def fake_get(url, body, retries=2):
        calls.append(body)
        if url.endswith("/account"):
            return {"account_txs": [{"transaction_hash": f"h{i}"} for i in range(45)]}
        assert len(body["tx_hashes"]) <= 20
        return {"transactions": [item(ME, "x.near", [], h) | {"is_success": True} for h in body["tx_hashes"]]}  # fmt: skip

    monkeypatch.setattr(sources, "get_json", fake_get)
    assert len(sources.get_account_txs(ME, limit=45)) == 45
    assert [len(c["tx_hashes"]) for c in calls[1:]] == [20, 20, 5]
