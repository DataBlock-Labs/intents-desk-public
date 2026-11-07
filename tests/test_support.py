import copy
import json
from datetime import UTC, datetime

import pytest

from intents_desk import incidents, sources, support, web
from intents_desk.store import connect
from tests import fakes
from tests.test_incidents import H0, bad, healthy, tg  # noqa: F401 (fixture)

ADDRESSES = (fakes.REFUNDED, "0xabc", "TRefund")  # deposit, recipient, refund address


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("NEAR_BLOCK_LOOKUP", "0")
    monkeypatch.setenv("SESSION_KEY_FILE", str(tmp_path / "s.key"))
    monkeypatch.delenv("MASK_PARTNERS", raising=False)
    return connect(tmp_path / "t.duckdb")


@pytest.fixture
def fake(monkeypatch):
    server = fakes.Fake()
    base = server.start()
    monkeypatch.setattr(sources, "ONECLICK", f"{base}/oneclick")
    monkeypatch.setattr(sources, "EXPLORER", f"{base}/explorer")
    monkeypatch.setattr(sources, "RPC", f"{base}/rpc")
    monkeypatch.setenv("INTENTS_EXPLORER_JWT", "fake-explorer-key")
    yield server
    server.stop()


def test_case_explains_the_swap_with_chain_refs_and_no_addresses(con):
    con.execute(
        "INSERT INTO app.tx_block VALUES (?, ?, now())",
        [fakes.NEAR_TX, fakes.NEAR_BLOCK],
    )
    case = support.view(con, support.build(con, fakes.STATUS, "a" * 20), full=True)
    assert case["answer"].startswith("Your funds came back: 15006.128946")
    assert case["cause"] == support.REASONS["QUOTE_EXPIRED"][0]
    assert [(r["chain"], r["kind"]) for r in case["refs"]] == [
        ("tron", "deposit"),
        ("near", "intent"),
    ]  # the bad destination hash and its javascript: link are dropped
    assert case["refs"][0]["url"].startswith("https://tronscan.org/")
    assert case["refs"][1]["block"] == fakes.NEAR_BLOCK
    assert [t["done"] for t in case["timeline"]] == [True, True, True, True]
    blob = json.dumps(case)
    assert not any(a in blob for a in ADDRESSES)
    assert "javascript" not in blob
    assert f"block {fakes.NEAR_BLOCK}" in case["message"]
    # a case link or the public list: no deposit or payout tx (their pages show addresses)
    link = support.view(con, support.build(con, fakes.STATUS, "a" * 20), full=False)
    assert [r["kind"] for r in link["refs"]] == ["intent"]
    assert fakes.TRON_TX not in json.dumps(link)


def test_odd_replies_do_not_crash(con):
    data = {"status": "PENDING_DEPOSIT", "swapDetails": None, "quoteResponse": None}
    assert (
        support.view(con, support.build(con, data, "f" * 20), full=True)["refs"] == []
    )
    data = copy.deepcopy(fakes.STATUS)
    data["status"] = "KNOWN_DEPOSIT_TX"
    data["quoteResponse"]["quote"]["amountInUsd"] = "n/a"
    case = support.view(con, support.build(con, data, "f" * 20), full=True)
    assert case["timeline"][1]["done"] and "n/a USD" in case["message"]


def test_failed_swap_without_txs_says_no_funds_moved(con):
    data = copy.deepcopy(fakes.STATUS)
    data["status"] = "FAILED"
    data["swapDetails"] = {"refundReason": "INSUFFICIENT_BALANCE"}
    case = support.build(con, data, "b" * 20)
    assert case["refs"] == [] and "No funds moved" in case["cause"]
    assert case["timeline"][-1] == {
        "step": "Failed",
        "at": data["updatedAt"],
        "done": True,
        "ref": None,
    }
    data["swapDetails"] = {"refundReason": "SOMETHING_NEW"}
    assert "SOMETHING_NEW" in support.build(con, data, "b" * 20)["cause"]


def test_case_links_its_incident_and_masks_the_partner(con, monkeypatch):
    healthy(con, range(4))
    bad(con, 0, "widget-y:pda")
    incidents.detect(con)
    data = copy.deepcopy(fakes.STATUS)
    data["quoteResponse"]["quoteRequest"]["referral"] = "widget-y:pda"
    data["quoteResponse"]["timestamp"] = (H0.replace(minute=20)).isoformat()
    saved = support.build(con, data, "c" * 20)  # built and saved while names show
    inc = support.view(con, saved, full=True)["incident"]
    assert inc["id"] == "widget-y:pda@2026-11-04T13:00Z" and inc["refunded"] == 40
    monkeypatch.setenv("MASK_PARTNERS", "1")
    case = support.view(con, saved, full=True)  # the mask applies when served
    assert "widget-y" not in json.dumps(case)
    assert case["incident"]["label"].startswith("partner-")
    assert all(r["kind"] != "intent" for r in case["refs"])  # nearblocks names it
    data["quoteResponse"]["timestamp"] = "2026-11-01T00:00:00Z"  # before it
    case = support.view(con, support.build(con, data, "c" * 20), full=True)
    assert case["incident"] is None


def test_saved_cases_and_lookup_counts(con):
    case = support.build(con, fakes.STATUS, "d" * 20)
    assert support.save(con, case) is True  # new: count it
    assert support.save(con, case) is False  # polled again within the hour
    assert support.saved(con, "d" * 20) == case
    for _ in range(support.SPIKE):
        support.log_lookup(con, case)
    [ask] = support.asks(con)
    assert ask == {
        "status": "REFUNDED",
        "reason": "QUOTE_EXPIRED",
        "lookups": support.SPIKE,
        "in_incident": None,
    }
    assert support.spikes(con) == [("QUOTE_EXPIRED", support.SPIKE)]


def test_block_lookup_uses_the_rpc_once_then_the_cache(con, fake, monkeypatch):
    monkeypatch.setenv("NEAR_BLOCK_LOOKUP", "1")
    assert support.block_of(con, fakes.NEAR_TX)[0] == fakes.NEAR_BLOCK
    assert support.block_of(con, "1" * 44) is None  # unknown tx: no error
    fake.stop()
    assert support.block_of(con, "1" * 44) is None  # the miss is cached: no RPC
    sql = "SELECT count(*) FROM app.tx_block WHERE block_height IS NULL"
    assert con.execute(sql).fetchone() == (1,)
    assert support.block_of(con, fakes.NEAR_TX)[0] == fakes.NEAR_BLOCK  # cached
    evidence = [{"near_tx": [fakes.NEAR_TX]}, {"near_tx": []}]
    assert [e.get("block") for e in support.add_blocks(con, evidence)] == [
        fakes.NEAR_BLOCK,
        None,
    ]


def test_lookup_by_tx_hash_gives_the_reduced_view(con, fake, monkeypatch):
    monkeypatch.setattr(web, "_db", {"con": con})
    code, body = web.lookup(fakes.TRON_TX)
    assert code == 200 and body["by_hash"] is True
    assert body["case"]["status"] == "REFUNDED"
    assert not any(a in json.dumps(body) for a in ADDRESSES)
    monkeypatch.setattr(sources, "EXPLORER_GAP", 5.0)
    assert web.lookup(fakes.NEAR_TX)[0] == 429  # the shared slot was used < 5 s ago
    monkeypatch.setattr(sources, "EXPLORER_GAP", 0.0)
    assert web.lookup("unknown0hash0000000000")[0] == 404
    assert web.lookup("someone.near")[0] == 404  # not a hash: no Explorer search
    code, body = web.lookup(fakes.REFUNDED)  # the address itself: full view
    assert code == 200 and "by_hash" not in body
    assert support.saved(con, body["case"]["ref"])


def test_explorer_search_needs_an_exact_match(monkeypatch):
    row = {"depositAddress": "dep1", "nearTxHashes": ["other"]}
    monkeypatch.setattr(sources, "get_json", lambda *_a, **_k: [row])
    assert sources.search_explorer("near-prefix") is None


def test_spike_alert_once_per_reason_and_hour(con, tg, monkeypatch):  # noqa: F811
    con.execute(
        "INSERT INTO app.telegram (account_id, chat_id) VALUES ('datablock.near', 7)"
    )
    con.execute(
        "INSERT INTO ops.run_log (run_id, product, job, started_at, finished_at, status)"
        " VALUES ('r1', 'intents-desk', 'sync', now(), now(), 'ok')"
    )
    monkeypatch.setattr(web, "_db", {"con": con})
    monkeypatch.setitem(web._status, "spikes_sent", set())
    case = support.build(con, fakes.STATUS, "e" * 20)
    for _ in range(support.SPIKE):
        support.log_lookup(con, case)
    web.watch_once()
    web.watch_once()
    assert [t for _c, t in tg.sent] == [
        f"Intents Desk: {support.SPIKE} support lookups for QUOTE_EXPIRED this hour."
    ]


def test_reproduce_commands_cannot_run_a_referral(con):
    healthy(con, range(4))
    bad(con, 0, '$(touch /tmp/x)"`id`')
    incidents.detect(con)
    [inc] = [i for i in incidents.listing(con) if i["scope"] == "partner"]
    text = incidents.reproduce(inc)
    assert "$(touch" not in text and "`id`" not in text
    assert "referral=%24%28touch%20%2Ftmp%2Fx%29%22%60id%60&" in text
    assert datetime.now(UTC)  # keeps the UTC import used by fixtures honest
