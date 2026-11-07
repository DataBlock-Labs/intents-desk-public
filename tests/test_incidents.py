import json
from datetime import UTC, datetime, timedelta

import pytest

from intents_desk import incidents, web
from intents_desk.store import connect, load_explorer_txs, partner_report
from tests.test_telegram import FakeTelegram

H0 = datetime(2026, 11, 4, 13, tzinfo=UTC)
HOUR = timedelta(hours=1)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("INCIDENT_REPORTS_DIR", str(tmp_path / "reports" / "incidents"))
    monkeypatch.delenv("VAULT_INCIDENTS_DIR", raising=False)
    return connect(tmp_path / "t.duckdb")


def add(con, hour, partner, status, n, reason=None, route=("arb", "base")):
    con.execute(
        "INSERT INTO ops.hourly_swap_summary (hour, partner, origin_chain,"
        " destination_chain, status, swaps, usd_in, refunded_usd, top_refund_reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        [H0 + hour * HOUR, partner, *route, status, n, 10.0 * n, reason],
    )


def healthy(con, hours):
    """A big healthy partner in every hour, so the network rule stays quiet."""
    for h in hours:
        add(con, h, "partner-b", "SUCCESS", 1000)


def bad(con, h, partner="widget-y:pda", refunded=40, failed=20, ok=0):
    add(con, h, partner, "REFUNDED", refunded, "INTENT_SUBMIT_FAILED")
    if failed:
        add(con, h, partner, "FAILED", failed)
    if ok:
        add(con, h, partner, "SUCCESS", ok)


def test_failed_counts_and_49_swaps_do_not_open(con):
    healthy(con, range(4))
    bad(con, 0, "fails-only", refunded=0, failed=25, ok=25)  # 50 swaps, 50% bad
    bad(con, 1, "too-small", refunded=49, failed=0)  # 49 swaps
    found = {i["referral"]: i for i in incidents.find(con)}
    assert set(found) == {"fails-only"}
    assert (
        found["fails-only"]["failed"] == 25 and found["fails-only"]["bad_pct"] == 50.0
    )


def test_short_dip_is_one_incident_and_it_closes_after_two_clean_hours(con):
    healthy(con, range(12))
    for h in (0, 3):  # 2 clean hours between: same incident
        bad(con, h)
    bad(con, 7)  # 3 clean hours before it: a new incident
    found = sorted(incidents.find(con), key=lambda i: i["started_at"])
    assert [(i["started_at"], i["last_bad_hour"]) for i in found] == [
        (H0, H0 + 3 * HOUR),
        (H0 + 7 * HOUR, H0 + 7 * HOUR),
    ]
    assert found[0]["ended_at"] == H0 + 4 * HOUR and found[0]["swaps"] == 120
    assert found[1]["ended_at"] == H0 + 8 * HOUR  # hours 8 and 9 are clean


def test_open_until_two_clean_hours_exist(con):
    healthy(con, range(2))
    bad(con, 0)
    [inc] = incidents.find(con)
    assert inc["ended_at"] is None  # only hour 1 after it
    healthy(con, [2])
    [inc] = incidents.find(con)
    assert inc["ended_at"] == H0 + HOUR


def test_detect_is_idempotent_and_others_exclude_partners_in_an_incident(con):
    healthy(con, range(4))
    bad(con, 0, "a:pda")
    bad(con, 0, "b:pda")
    incidents.detect(con)
    first = incidents.listing(con)
    incidents.detect(con)
    again = incidents.listing(con)
    strip = [{k: v for k, v in i.items() if k != "updated_at"} for i in first]
    assert strip == [{k: v for k, v in i.items() if k != "updated_at"} for i in again]
    a = next(i for i in first if i["referral"] == "a:pda")
    assert a["others_pct"] == 0.0  # b:pda is excluded: it has an incident too


def test_network_rule_lists_top_partners(con):
    healthy(con, range(4))
    for p in ("p1", "p2", "p3"):
        add(con, 0, p, "REFUNDED", 49, "QUOTE_EXPIRED")  # each too small on its own
    add(con, 0, "partner-b", "REFUNDED", 30, "QUOTE_EXPIRED")
    [inc] = incidents.find(con)
    assert inc["scope"] == "network" and inc["bad_pct"] > 10
    assert [p for p, _n in inc["partners"]] == ["p1", "p2", "p3"]


def raw_swaps(start, n=60, referral="leaky:pda"):
    rows = []
    for i in range(n):
        rows.append(
            {
                "depositAddress": f"{i:064x}",
                "depositMemo": None,
                "createdAt": (start + timedelta(seconds=30 * i)).strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z"
                ),
                "status": "FAILED" if i % 10 == 0 else "REFUNDED",
                "referral": referral,
                "refundReason": "INSUFFICIENT_BALANCE" if i % 10 == 0 else "X_FAILED",
                "refundTo": f"secret-sender-{i % 4}",
                "recipient": "secret-recipient",
                "originAsset": "nep141:arb-0xaf88.omft.near",
                "destinationAsset": "nep141:base-0x8335.omft.near",
                "amountInFormatted": "10.0",
                "amountInUsd": "10.0",
                "nearTxHashes": []
                if i % 10 == 0
                else [
                    "7nLmhrycnYcFrTFWm4aABUziPdCwRu9p3kBNR6Q9vf2" + "abcdefgh"[i % 8]
                ],
            }
        )
    return rows


def test_evidence_from_raw_has_tx_hashes_but_no_addresses(con, monkeypatch):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    old = dict(raw_swaps(now - 3 * HOUR, 1, "other")[0], depositAddress="old")
    load_explorer_txs(con, [old, *raw_swaps(now)])
    incidents.detect(con)
    [inc] = [i for i in incidents.listing(con) if i["referral"] == "leaky:pda"]
    assert inc["ended_at"] is None and inc["senders"] == 4 and inc["failed"] == 6
    assert any(e["near_tx"] for e in inc["evidence"])
    assert all(not e["near_tx"] for e in inc["evidence"] if e["status"] == "FAILED")
    blob = json.dumps(incidents.public(inc)) + incidents.markdown(inc)
    assert "secret" not in blob and f"{1:064x}" not in blob  # short deposits only
    assert "failed before any funds moved" in incidents.draft(inc)


def test_public_view_masks_partner_names(con, monkeypatch, tmp_path):
    healthy(con, range(4))
    bad(con, 0, "widget-y:pda")
    incidents.detect(con)
    monkeypatch.setenv("SESSION_KEY_FILE", str(tmp_path / "s.key"))
    monkeypatch.setenv("MASK_PARTNERS", "1")
    [inc] = incidents.listing(con)
    pub = incidents.public(inc)
    assert "widget-y" not in json.dumps(pub) and pub["referral"].startswith("partner-")
    assert incidents.find_public(con, pub["id"])["swaps"] == inc["swaps"]
    assert "widget-y" not in incidents.draft(inc)


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram()
    from tests.test_telegram import TOKEN

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_API_URL", fake.url)
    yield fake
    fake.server.shutdown()


def test_drafts_once_on_open_and_once_on_close(con, tg, tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_INCIDENTS_DIR", str(tmp_path / "vault" / "Incidents"))
    monkeypatch.setenv("INCIDENT_REPORTS_DIR", str(tmp_path / "reports" / "incidents"))
    healthy(con, range(2))
    bad(con, 0)
    assert incidents.run(con)["skipped"] == "owner Telegram chat not linked"
    con.execute(
        "INSERT INTO app.telegram (account_id, chat_id) VALUES ('datablock.near', 7)"
    )
    assert incidents.run(con)["drafts"] == 1
    assert incidents.run(con)["drafts"] == 0
    healthy(con, [2])  # second clean hour: closed
    assert incidents.run(con)["drafts"] == 1
    opened, closed = (text for _chat, text in tg.sent)
    assert "Heads-up" in opened and "Resolved" in closed
    for text in (opened, closed):
        body = text.split("First reply:")[0]
        assert "http" not in body  # no links in the post body
    assert list((tmp_path / "reports" / "incidents").glob("*.md"))
    assert list((tmp_path / "vault" / "Incidents").glob("*.md"))


def test_gaps_are_hours_with_no_swaps(con):
    healthy(con, [0, 1, 4])
    assert incidents.gaps(con, since_hours=10_000) == [
        ["2026-11-04T15:00Z", "2026-11-04T16:00Z"]
    ]


def test_stale_sync_alert_once_then_reset(con, tg, monkeypatch):
    con.execute(
        "INSERT INTO app.telegram (account_id, chat_id) VALUES ('datablock.near', 7)"
    )
    monkeypatch.setattr(web, "_db", {"con": con})
    monkeypatch.setitem(web._status, "stale_sent", False)
    assert web.stale_once() == 1 and web.stale_once() == 1
    assert len(tg.sent) == 1 and "no good swap sync" in tg.sent[0][1]
    con.execute(
        "INSERT INTO ops.run_log (run_id, product, job, started_at, finished_at, status)"
        " VALUES ('r1', 'intents-desk', 'sync', now(), now(), 'ok')"
    )
    assert web.stale_once() == 0 and web._status["stale_sent"] is False


def test_partner_alert_counts_failed_swaps(con):
    now = datetime.now(UTC)
    rows = raw_swaps(now - timedelta(minutes=40))
    for r in rows:
        r["status"] = "FAILED"
    load_explorer_txs(con, rows)
    [row] = partner_report(con, 1, 50, 50).fetchall()
    assert row[0] == "leaky:pda" and row[2] == 0.0 and row[7] is True


def test_report_escapes_referral_ids_and_drops_bad_hashes(con):
    healthy(con, range(4))
    bad(con, 0, "<img src=x onerror=alert(1)>")
    incidents.detect(con)
    [inc] = incidents.listing(con)
    assert "<img" not in incidents.markdown(inc)
    rows = raw_swaps(datetime(2026, 11, 4, 13, tzinfo=UTC), 1)
    rows[0]["nearTxHashes"] = [
        "javascript:alert(1)",
        "7nLmhrycnYcFrTFWm4aABUziPdCwRu9p3kBNR6Q9vf2n",
    ]
    assert incidents.pick_evidence(rows)[0]["near_tx"] == [
        "7nLmhrycnYcFrTFWm4aABUziPdCwRu9p3kBNR6Q9vf2n"
    ]
