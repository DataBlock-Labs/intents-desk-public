from datetime import UTC, datetime, timedelta

from intents_desk import web
from intents_desk.store import connect, load_explorer_txs, newest_swap_time

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def test_rate_limit_allows_30_a_minute_then_refuses():
    web._buckets.clear()
    assert all(web.allow("1.2.3.4", now=100.0) for _ in range(30))
    assert not web.allow("1.2.3.4", now=100.0)
    assert web.allow("5.6.7.8", now=100.0)  # other clients are not affected
    assert web.allow("1.2.3.4", now=103.0)  # one token back after 2 s


def test_sync_window_empty_db_reads_24_hours():
    assert web.sync_start(None, NOW) == NOW - timedelta(hours=24)


def test_sync_window_rereads_2_hours_before_newest_capped_at_24():
    assert web.sync_start(NOW - timedelta(hours=1), NOW) == NOW - timedelta(hours=3)
    assert web.sync_start(NOW - timedelta(days=5), NOW) == NOW - timedelta(hours=24)


def test_newest_swap_time(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    assert newest_swap_time(con) is None
    load_explorer_txs(
        con,
        [
            {
                "depositAddress": "a1234567",
                "createdAt": "2026-11-05T10:00:00Z",
                "status": "SUCCESS",
            }
        ],
    )
    assert newest_swap_time(con) == datetime(2026, 11, 5, 10, 0, tzinfo=UTC)


def test_deposit_addresses_need_swap_live(monkeypatch):
    status = {
        "status": "REFUNDED",
        "quoteResponse": {"quoteRequest": {"depositType": "ORIGIN_CHAIN"}},
    }
    monkeypatch.setattr(web.sources, "get_swap_status", lambda _a, **_k: status)
    monkeypatch.setattr(web.sources, "get_quote", lambda r, dry, **_k: {"quote": {}})
    q = {"address": ["TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq"], "dry": ["0"]}
    monkeypatch.delenv("SWAP_LIVE", raising=False)
    assert web.swap_again(q)[0] == 403
    monkeypatch.setenv("SWAP_LIVE", "1")
    assert web.swap_again(q)[0] == 200
    q["dry"] = ["1"]
    monkeypatch.delenv("SWAP_LIVE")
    assert web.swap_again(q)[0] == 200  # previews always work
