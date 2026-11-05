import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from intents_desk import sources, store, telegram
from intents_desk.store import connect

TOKEN = "123456:SECRET-TOKEN-never-in-logs"
ADDR = "TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq"


class FakeTelegram:
    """Records sendMessage calls; serves queued updates; can fail on purpose."""

    def __init__(self):
        self.sent, self.updates, self.fail = [], [], False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = self.path.rsplit("/", 1)[1]
                if fake.fail or not self.path.startswith(f"/bot{TOKEN}/"):
                    self.send_response(401)
                    self.end_headers()
                    return
                result = {
                    "getMe": {"username": "IntentsDeskTestBot"},
                    "sendMessage": {"message_id": 1},
                    "getUpdates": fake.updates,
                }[method]
                if method == "sendMessage":
                    fake.sent.append((body["chat_id"], body["text"]))
                if method == "getUpdates":
                    fake.updates = []
                data = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_API_URL", fake.url)
    yield fake
    fake.server.shutdown()


@pytest.fixture
def con(tmp_path):
    return connect(tmp_path / "t.duckdb")


def start(con, chat, text):
    telegram.handle_update(con, {"message": {"chat": {"id": chat}, "text": text}})


def code_of(link):
    return link.split("start=")[1]


def test_link_once_then_code_is_spent(con, tg):
    link = telegram.new_link(con, "alice.near")
    assert link.startswith("https://t.me/IntentsDeskTestBot?start=")
    start(con, 42, "/start " + code_of(link))
    assert telegram.linked_chat(con, "alice.near") == 42
    start(con, 99, "/start " + code_of(link))  # reused by someone else
    assert telegram.linked_chat(con, "alice.near") == 42
    assert tg.sent[-1][0] == 99 and "not valid" in tg.sent[-1][1]


def test_expired_code_is_refused(con, tg):
    link = telegram.new_link(con, "alice.near")
    con.execute("UPDATE app.link_code SET expires_at = now() - INTERVAL 1 MINUTE")
    start(con, 42, "/start " + code_of(link))
    assert telegram.linked_chat(con, "alice.near") is None


def test_alert_once_per_change_and_none_after_stop(con, tg, monkeypatch):
    statuses = iter(["PROCESSING", "PROCESSING", "REFUNDED", "REFUNDED", "SUCCESS"])
    monkeypatch.setattr(
        sources,
        "get_swap_status",
        lambda a, **k: {
            "status": next(statuses),
            "swapDetails": {
                "refundReason": "QUOTE_EXPIRED",
                "refundedAmountFormatted": "15006.13",
            },
        },
    )
    store.set_watch(con, "alice.near", ADDR, True)
    start(con, 42, "/start " + code_of(telegram.new_link(con, "alice.near")))
    tg.sent.clear()
    assert telegram.alerts_once(con)["sent"] == 0  # first look: record only
    assert telegram.alerts_once(con)["sent"] == 0  # no change
    assert telegram.alerts_once(con)["sent"] == 1  # PROCESSING -> REFUNDED
    assert tg.sent == [
        (
            42,
            (
                "Swap TTxfvD…n3dq: PROCESSING → REFUNDED (QUOTE_EXPIRED),"
                " 15006.13 back to your refund address."
            ),
        )
    ]
    assert telegram.alerts_once(con)["sent"] == 0  # same status again
    start(con, 42, "/stop")
    tg.sent.clear()
    assert telegram.alerts_once(con)["checked"] == 0 and tg.sent == []


def test_status_command_and_unlinked_chat(con, tg):
    store.set_watch(con, "alice.near", ADDR, True)
    start(con, 7, "/status")
    assert "not linked" in tg.sent[-1][1]
    start(con, 42, "/start " + code_of(telegram.new_link(con, "alice.near")))
    start(con, 42, "/status")
    assert tg.sent[-1] == (42, "TTxfvD…n3dq: not checked yet")


def test_errors_never_contain_the_token(con, tg):
    tg.fail = True
    with pytest.raises(telegram.TelegramError) as err:
        telegram.send(42, "hi")
    assert TOKEN not in str(err.value) and "SECRET" not in str(err.value)


def test_watch_limit(con):
    for i in range(store.MAX_WATCHED):
        store.set_watch(con, "bob.near", f"addr{i:04d}xxxx", True)
    with pytest.raises(ValueError, match="at most"):
        store.set_watch(con, "bob.near", "one-more-xxxx", True)
