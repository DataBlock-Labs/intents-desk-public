import json
import subprocess

import pytest

from intents_desk import store, telegram, tickets, web, xpost
from tests import fakes
from tests.test_telegram import TOKEN, FakeTelegram


@pytest.fixture
def con(tmp_path, monkeypatch):
    fake = fakes.Fake()
    base = fake.start()
    from intents_desk import sources

    monkeypatch.setattr(sources, "ONECLICK", f"{base}/oneclick")
    monkeypatch.setattr(sources, "RPC", f"{base}/rpc")
    monkeypatch.setenv("NEAR_BLOCK_LOOKUP", "0")
    monkeypatch.setenv("PUBLIC_HOST", "desk.example")
    c = store.connect(tmp_path / "t.duckdb")
    monkeypatch.setattr(web, "_db", {"con": c})
    yield c
    fake.stop()


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_API_URL", fake.url)
    telegram._channel_sent.clear()
    from intents_desk import web as _web

    _web._buckets.clear()
    yield fake
    fake.server.shutdown()


def test_report_opens_one_ticket_and_the_public_view_has_no_address(con):
    t = tickets.report(con, fakes.NEAR_REFUNDED, "web")
    assert t["new"] and t["status"] == "REFUNDED" and t["reason"] == "QUOTE_EXPIRED"
    assert tickets.report(con, fakes.NEAR_REFUNDED, "telegram")["new"] is False
    rows = tickets.listing(con)
    assert (
        len(rows) == 1
        and rows[0]["source"] == "web"
        and rows[0]["status"] == "REFUNDED"
    )
    served = json.dumps([rows, t])
    assert fakes.NEAR_REFUNDED not in served  # the address never leaves the server
    assert tickets.report(con, "nope-not-a-swap-0000", "web") is None


def test_refresh_records_a_status_change(con, monkeypatch):
    tickets.report(con, fakes.NEAR_REFUNDED, "web")
    con.execute("UPDATE app.ticket SET last_status = 'PROCESSING'")
    changes = tickets.refresh(con)
    assert changes == [{"ref": changes[0]["ref"], "from": "PROCESSING", "to": "REFUNDED", "reason": "QUOTE_EXPIRED"}]  # fmt: skip
    assert tickets.refresh(con) == []  # final: not checked again


def test_feed_post_with_a_swap_key_opens_a_ticket_and_drafts_a_reply(con, tg):
    con.execute("INSERT INTO app.telegram (account_id, chat_id) VALUES ('datablock.near', 7)")  # fmt: skip
    posts = [
        {
            "id": "x:123",
            "url": "https://x.com/a/status/123",
            "text": f"stuck!! {fakes.NEAR_REFUNDED} help",
        },
        {"id": "r:1", "url": "https://reddit.com/r/x/1", "text": "no key in this one"},
    ]
    assert web.tickets_from_posts(con, posts) == 1
    assert web.tickets_from_posts(con, posts) == 0  # the same post is not read twice
    (row,) = tickets.listing(con)
    assert row["source"] == "x" and row["source_url"] == "https://x.com/a/status/123"
    (chat, text), = tg.sent  # fmt: skip
    assert (
        chat == 7
        and "reply to x.com/i/status/123" in text
        and fakes.NEAR_REFUNDED not in text
    )
    draft = con.execute(
        "SELECT kind, reply_to, length(text) FROM app.x_draft"
    ).fetchone()
    assert draft[0] == "reply" and draft[1] == "123" and draft[2] <= 280


def test_channel_posts_are_capped(tg, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHANNEL", "@desk_test")
    monkeypatch.setattr(telegram, "CHANNEL_PER_HOUR", {"ticket": 2, "incident": 1})
    assert [telegram.channel_post(f"m{i}") for i in range(3)] == [True, True, False]
    assert telegram.channel_post("incident", "incident")  # its own cap: not crowded out
    assert [c for c, _ in tg.sent] == ["@desk_test"] * 3


def test_telegram_report_and_help(con, tg):
    telegram.handle_update(con, {"message": {"chat": {"id": 5}, "text": "/help"}})
    telegram.handle_update(con, {"message": {"chat": {"id": 5}, "text": f"/report {fakes.NEAR_REFUNDED}"}})  # fmt: skip
    telegram.handle_update(con, {"message": {"chat": {"id": 5}, "text": "/report"}})
    help_text, reply, usage = (t for _c, t in tg.sent)
    assert "/report" in help_text
    assert (
        "REFUNDED (QUOTE_EXPIRED)" in reply and "https://desk.example/#case=" in reply
    )
    assert fakes.NEAR_REFUNDED not in reply
    assert usage.startswith("Send /report")


def _draft(con, monkeypatch, kind="incident"):
    con.execute("INSERT INTO app.telegram (account_id, chat_id) VALUES ('datablock.near', 7)")  # fmt: skip
    return xpost.new_draft(con, kind, "Heads-up: test", "https://desk.example/#incident=1", "99" if kind == "reply" else None)  # fmt: skip


def press(con, chat, data, user=None):
    query = {"id": "q", "data": data, "message": {"chat": {"id": chat}}, "from": {"id": chat if user is None else user}}  # fmt: skip
    telegram.handle_update(con, {"callback_query": query})


def test_only_the_owner_can_approve_and_each_draft_posts_once(con, tg, monkeypatch):
    calls = []

    def fake_run(args, **kw):
        assert kw.get("shell") in (None, False) and args[:3] == [
            "xurl",
            "--auth",
            "oauth1",
        ]
        calls.append(args[3:])
        return subprocess.CompletedProcess(args, 0, json.dumps({"data": {"id": str(1000 + len(calls))}}), "")  # fmt: skip

    monkeypatch.setattr(xpost.subprocess, "run", fake_run)
    monkeypatch.setenv("X_POST", "1")
    d = _draft(con, monkeypatch)
    press(con, 8, f"x:link:{d}")  # not the owner's chat: ignored
    assert calls == [] and tg.sent == []
    press(con, 7, f"x:link:{d}")
    assert calls == [["post", "Heads-up: test"], ["reply", "1001", "https://desk.example/#incident=1"]]  # fmt: skip
    assert tg.sent[-1] == (7, "Posted: https://x.com/i/status/1001")
    press(con, 7, f"x:post:{d}")  # a second press
    assert len(calls) == 2 and tg.sent[-1][1] == "This draft was already handled."


def test_reply_skip_expiry_and_posting_off(con, tg, monkeypatch):
    monkeypatch.setattr(xpost.subprocess, "run", lambda *a, **k: pytest.fail("must not post"))  # fmt: skip
    d = _draft(con, monkeypatch, "reply")
    monkeypatch.delenv("X_POST", raising=False)
    press(con, 7, f"x:post:{d}")
    assert "Posting is off" in tg.sent[-1][1]
    press(con, 7, f"x:skip:{d}")  # still pending after "off": skip works
    assert tg.sent[-1][1].startswith("Skipped")
    old = xpost.new_draft(con, "incident", "old one")
    con.execute("UPDATE app.x_draft SET created_at = now() - INTERVAL 25 HOUR WHERE id = ?", [old])  # fmt: skip
    monkeypatch.setenv("X_POST", "1")
    press(con, 7, f"x:post:{old}")
    assert "expired" in tg.sent[-1][1]


@pytest.mark.parametrize(
    ("text", "reply_to"),
    [("x" * 281, None), ("see https://evil.example", None), ("ok", "12; rm -rf /")],
)
def test_draft_rules(con, text, reply_to):
    with pytest.raises(ValueError):
        xpost.new_draft(con, "reply" if reply_to else "incident", text, None, reply_to)


def test_group_member_cannot_approve_in_the_owner_chat(con, tg, monkeypatch):
    monkeypatch.setattr(xpost.subprocess, "run", lambda *a, **k: pytest.fail("must not post"))  # fmt: skip
    monkeypatch.setenv("X_POST", "1")
    d = _draft(con, monkeypatch)
    press(con, 7, f"x:post:{d}", user=99)  # owner chat, another person's press
    assert tg.sent == []


def test_report_by_hash_does_not_watch(con, tg, monkeypatch):
    con.execute(
        "INSERT INTO app.telegram (account_id, chat_id) VALUES ('alice.near', 5)"
    )
    monkeypatch.setattr(tickets, "resolve", lambda key: (fakes.NEAR_REFUNDED, {"status": "REFUNDED"}) if key == "H" * 44 else None)  # fmt: skip
    monkeypatch.setattr(tickets.support, "build", lambda con, data, ref: {"ref": ref, "status": "REFUNDED", "reason": None, "answer": "a", "next": "n", "refs": [], "timeline": [], "route": "near → near", "amount_in": None, "usd_in": None, "refunded": None, "amount_out": None, "created_at": None, "cause": None, "incident": None})  # fmt: skip
    telegram.handle_update(con, {"message": {"chat": {"id": 5}, "text": "/report " + "H" * 44}})  # fmt: skip
    assert store.watch_list(con, "alice.near") == []  # a hash never reveals the address


def test_a_failing_update_is_skipped_not_replayed(con, tg, monkeypatch):
    tg.updates = [{"update_id": 41, "message": {"chat": {"id": 5}, "text": "/help"}}]
    monkeypatch.setattr(telegram, "handle_update", lambda con, u: (_ for _ in ()).throw(telegram.TelegramError("blocked")))  # fmt: skip
    assert telegram.poll_once(con, 0, wait=0) == 42


def test_channel_incident_text_always_uses_the_alias(monkeypatch):
    from intents_desk import incidents

    monkeypatch.delenv("MASK_PARTNERS", raising=False)
    inc = {"scope": "partner", "referral": "evil.example/phish:pda"}
    assert incidents.label(inc) == "evil.example/phish:pda"  # the owner's own views
    assert incidents.label(inc, masked=True).startswith("partner-")
