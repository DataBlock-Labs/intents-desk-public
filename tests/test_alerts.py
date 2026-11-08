import hashlib
import json
import urllib.error
import urllib.request

import pytest

from intents_desk import alerts, store, telegram, web, xpost
from tests.test_new_swap import app  # noqa: F401  (the app fixture)
from tests.test_telegram import TOKEN, FakeTelegram

HOUR = "2026-11-08 10:00:00+00"


@pytest.fixture
def con(tmp_path, monkeypatch):
    c = store.connect(tmp_path / "a.duckdb")
    monkeypatch.setattr(web, "_db", {"con": c})
    monkeypatch.setenv("PUBLIC_HOST", "desk.example")
    monkeypatch.delenv("MASK_PARTNERS", raising=False)
    web._buckets.clear()
    return c


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_API_URL", fake.url)
    telegram._channel_sent.clear()
    yield fake
    fake.server.shutdown()


def hour_rows(con, good: int, bad: int, src="tron", dst="eth", partner="wallet-a:x"):
    for status, n, usd in (("SUCCESS", good, None), ("REFUNDED", bad, 100.0 * bad)):
        con.execute(
            "INSERT INTO ops.hourly_swap_summary (hour, partner, origin_chain,"
            " destination_chain, status, swaps, usd_in, refunded_usd, top_refund_reason)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            [
                HOUR,
                partner,
                src,
                dst,
                status,
                n,
                usd,
                "QUOTE_EXPIRED" if status == "REFUNDED" else None,
            ],
        )


@pytest.mark.parametrize(
    ("text", "kind", "params"),
    [
        ("route tron eth 20", "route", {"from": "tron", "to": "eth", "pct": 20.0}),
        ("route TRON eth 20%", "route", {"from": "tron", "to": "eth", "pct": 20.0}),
        ("network 10", "network", {"pct": 10.0}),
        ("reason quote_expired 30", "reason", {"reason": "QUOTE_EXPIRED", "count": 30}),
        ("partner partner-ee7bd4", "partner", {"partner": "partner-ee7bd4"}),
    ],
)
def test_parse_kinds(text, kind, params):
    cmd = alerts.parse(text)
    assert cmd == {"op": "add", "kind": kind, "params": params, "targets": ["telegram"]}


@pytest.mark.parametrize(
    "text",
    ["", "route tron eth", "route tron eth 0", "route tron eth 101", "network abc",
     "route tr0n;rm eth 5", "partner a b", "reason x 5", "nonsense"],
)  # fmt: skip
def test_parse_refuses(text):
    with pytest.raises(alerts.RuleError):
        alerts.parse(text)


def test_only_the_owner_gets_channel_x_and_refund_rules():
    with pytest.raises(alerts.RuleError, match="owner"):
        alerts.parse("network 10 +x")
    with pytest.raises(alerts.RuleError, match="owner"):
        alerts.parse("refund 5000")
    assert alerts.parse("network 10 +channel +x", owner=True)["targets"] == ["telegram", "x", "channel"]  # fmt: skip


def link_alice(con):
    con.execute(
        "INSERT INTO app.telegram (account_id, chat_id) VALUES ('alice.near', 5)"
    )


def test_route_rule_fires_once_then_resolves_once(con):
    link_alice(con)
    rid = alerts.add(con, "alice.near", alerts.parse("route tron eth 20"))
    hour_rows(con, good=70, bad=30)
    (t,) = alerts.check(con)
    assert t["id"] == rid and not t["resolved"] and t["facts"]["pct"] == 30.0
    assert alerts.check(con) == []  # still true: quiet
    text = alerts.render(t)
    lines = text.split("\n")
    assert len(lines) == 4
    assert lines[0].startswith("ALERT · Route tron → eth: 30% refunded or failed")
    assert "30 of 100 swaps, $3,000 refunded. Top reason: QUOTE_EXPIRED." in lines[1]
    assert (
        lines[2].startswith("Act: users: Get a new quote")
        and "quote deadlines" in lines[2]
    )
    assert (
        lines[3] == f"Open: https://desk.example/ · Stop this alert: /alert off {rid}"
    )
    con.execute("DELETE FROM ops.hourly_swap_summary")
    hour_rows(con, good=95, bad=5)
    (r,) = alerts.check(con)
    assert r["resolved"] and alerts.render(r).startswith("RESOLVED · route tron → eth")


def test_small_hours_never_fire(con):
    link_alice(con)
    alerts.add(con, "alice.near", alerts.parse("network 10"))
    hour_rows(con, good=5, bad=10)  # 15 swaps: below MIN_SWAPS
    assert alerts.check(con) == []


def test_public_render_has_no_raw_partner(con, monkeypatch):
    t = {"id": 1, "kind": "partner", "params": {"partner": "evil.example:pda"}, "resolved": False,
         "facts": {"pct": 99.0, "swaps": 500, "reason": "INTENT_SUBMIT_FAILED",
                   "started": "2026-11-08T10:00Z", "referral": "evil.example:pda"}}  # fmt: skip
    public = alerts.render(t, public=True)
    assert "evil" not in public and "partner-" in public and "/alert off" not in public
    assert len(alerts.x_text(t)) <= 280 and "http" not in alerts.x_text(t)
    assert "evil.example:pda" in alerts.render(t)  # the rule owner's own Telegram


def test_ten_rule_limit_and_off(con):
    for i in range(alerts.MAX_RULES):
        alerts.add(con, "alice.near", alerts.parse(f"network {i + 1}"))
    with pytest.raises(alerts.RuleError, match="10 alerts"):
        alerts.add(con, "alice.near", alerts.parse("network 50"))
    assert alerts.off(con, "alice.near", 1) and not alerts.off(con, "bob.near", 2)
    assert len(alerts.listing(con, "alice.near")) == alerts.MAX_RULES - 1


def test_telegram_alert_command_and_delivery(con, tg, monkeypatch):
    con.execute(
        "INSERT INTO app.telegram (account_id, chat_id) VALUES ('alice.near', 5)"
    )
    say = lambda text: telegram.handle_update(
        con, {"message": {"chat": {"id": 5}, "text": text}}
    )
    say("/alert route tron eth 20 +x")  # not the owner: refused
    say("/alert route tron eth 20")
    say("/alert list")
    refused, added, listed = (t for _c, t in tg.sent)
    assert "Only the owner" in refused and added.startswith(
        "Alert 1 on: route tron → eth"
    )
    assert listed.startswith(
        "1 · route tron → eth over 20% refunded or failed → telegram"
    )
    hour_rows(con, good=60, bad=40)
    assert web.alert_rules_once() == 1
    assert tg.sent[-1][0] == 5 and tg.sent[-1][1].startswith(
        "ALERT · Route tron → eth: 40%"
    )


def test_owner_rule_goes_to_channel_and_x_draft(con, tg, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@desk_test")
    con.execute(
        "INSERT INTO app.telegram (account_id, chat_id) VALUES ('datablock.near', 7)"
    )
    alerts.add(
        con, "datablock.near", alerts.parse("route tron eth 20 +channel +x", owner=True)
    )
    hour_rows(con, good=60, bad=40)
    web.alert_rules_once()
    chats = [c for c, _t in tg.sent]
    assert chats == [7, "@desk_test", 7]  # own alert, channel, X draft with buttons
    channel = tg.sent[1][1]
    assert "/alert off" not in channel and "Route tron → eth" in channel
    (kind, status) = con.execute("SELECT kind, status FROM app.x_draft").fetchone()
    assert (kind, status) == ("incident", "pending")
    assert (
        xpost.URL.search(con.execute("SELECT text FROM app.x_draft").fetchone()[0])
        is None
    )


def post_agent(host, body, token):
    headers = {"Content-Type": "application/json"}  # no Origin: a server-side caller
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://{host}/api/agent/alerts", json.dumps(body).encode(), headers)  # fmt: skip
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_agent_api_needs_the_token(app, monkeypatch):  # noqa: F811
    monkeypatch.setenv(
        "AGENT_TOKEN_SHA256", hashlib.sha256(b"s3cret-token").hexdigest()
    )
    assert post_agent(app, {"text": "network 10"}, None)[0] == 401
    assert post_agent(app, {"text": "network 10"}, "wrong")[0] == 401
    code, d = post_agent(app, {"text": "network 10 +x"}, "s3cret-token")
    assert code == 200 and d["reply"].startswith("Alert") and d["rules"][0]["targets"] == ["telegram", "x"]  # fmt: skip
    assert post_agent(app, {"text": "nonsense"}, "s3cret-token")[0] == 400
    monkeypatch.delenv("AGENT_TOKEN_SHA256")
    assert (
        post_agent(app, {"text": "list"}, "s3cret-token")[0] == 401
    )  # off without a hash


def test_non_owner_partner_rules_take_the_alias_only():
    with pytest.raises(alerts.RuleError, match="alias"):
        alerts.parse("partner widget-guess:pda")  # a guess at a raw name
    assert alerts.parse("partner widget-guess:pda", owner=True)["params"] == {"partner": "widget-guess:pda"}  # fmt: skip


def test_rules_without_a_linked_chat_are_not_checked(con):
    alerts.add(con, "nobody.near", alerts.parse("network 10"))
    hour_rows(con, good=60, bad=40)
    assert alerts.check(con) == []
