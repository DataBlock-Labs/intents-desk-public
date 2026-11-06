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


def test_api_data_older_than_24_hours_is_purged(tmp_path):
    from intents_desk.store import purge_api_data

    con = connect(tmp_path / "t.duckdb")
    now = datetime.now(UTC)
    rows = [
        {
            "depositAddress": f"addr{i:04d}",
            "createdAt": (now - timedelta(hours=h)).isoformat(),
            "status": "SUCCESS",
        }
        for i, h in enumerate([1, 23, 25, 48])
    ]
    load_explorer_txs(con, rows)
    assert purge_api_data(con) == 2
    left = con.execute("SELECT count(*) FROM bronze.explorer_tx").fetchone()[0]
    assert left == 2


def test_summary_without_data_or_key_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("INTENTS_EXPLORER_JWT", raising=False)
    monkeypatch.setattr(web, "_db", {"con": connect(tmp_path / "t.duckdb")})
    web._cache.update(at=0.0, data=None)
    assert web.get_summary()["swaps"] == 0


def test_large_replies_are_gzipped_only_when_asked(monkeypatch):
    import gzip
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/static/near-connect.js"
    try:
        req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req) as r:
            assert r.headers["Content-Encoding"] == "gzip"
            zipped = r.read()
        with urllib.request.urlopen(url) as r:  # no Accept-Encoding: plain bytes
            assert r.headers["Content-Encoding"] is None
            plain = r.read()
        assert gzip.decompress(zipped) == plain and len(zipped) < len(plain) / 2
    finally:
        server.shutdown()


def test_page_names_app_js_by_version_so_phones_get_new_code():
    import hashlib
    import re
    from importlib.resources import files

    app = files("intents_desk").joinpath("static/app.js").read_bytes()
    found = re.findall(rb'/static/app\.js\?v=([0-9a-f]{10})"', web.page_html())
    assert found == [hashlib.sha256(app).hexdigest()[:10].encode()]
