import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer

import pytest

from intents_desk import web
from intents_desk.store import connect, load_explorer_txs, partner_detail, recent_swaps
from tests.fakes import explorer_swaps


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "t.duckdb")
    load_explorer_txs(c, explorer_swaps(datetime.now(UTC)))
    return c


def test_partner_detail_totals_match_routes_and_statuses(con):
    d = partner_detail(con, "widget-x:pda")
    assert d["swaps_24h"] == 60 and d["refund_pct_24h"] == 100.0
    assert sum(n for _r, n, _p in d["routes"]) == d["swaps_24h"]
    assert sum(n for _s, n in d["statuses"]) == d["swaps_24h"]
    assert d["reasons"] == [("INTENT_SUBMIT_FAILED", 60)]
    assert sum(n for _h, n, _p in d["hourly"]) == d["swaps_24h"]


def test_partner_detail_reads_daily_history_from_aggregates(con):
    con.execute(
        "INSERT INTO ops.hourly_swap_summary (hour, partner, origin_chain, destination_chain,"
        " status, swaps, usd_in, refunded_usd) VALUES"
        " ('2026-11-01 10:00+00', 'partner-b', 'eth', 'sol', 'SUCCESS', 9, 900, NULL),"
        " ('2026-11-01 11:00+00', 'partner-b', 'eth', 'sol', 'REFUNDED', 1, 100, 100)"
    )
    daily = partner_detail(con, "partner-b")["daily"]
    assert daily == [("2026-11-01", 10, 10.0, 100.0)]


def test_unknown_partner_is_empty_not_an_error(con):
    d = partner_detail(con, "nobody")
    assert d["swaps_24h"] == 0 and d["routes"] == [] and d["hourly"] == []


def test_recent_swaps_filters(con):
    assert len(recent_swaps(con, limit=200)) == 121
    refunded = recent_swaps(con, status="REFUNDED", limit=200)
    assert refunded and {s["status"] for s in refunded} == {"REFUNDED"}
    kit = recent_swaps(con, partner="partner-b", limit=5)
    assert len(kit) == 5 and {s["partner"] for s in kit} == {"partner-b"}
    assert recent_swaps(con, route="tron → eth", limit=3)[0]["route"] == "tron → eth"
    times = [s["created_at"] for s in recent_swaps(con, limit=10)]
    assert times == sorted(times, reverse=True)


@pytest.fixture
def base(con, monkeypatch):
    monkeypatch.setattr(web, "_db", {"con": con})
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.mark.parametrize(
    "query",
    [
        "/api/partner?id=" + urllib.parse.quote("x' OR 1=1 --"),
        "/api/partner?id=",
        "/api/swaps?status=DROP",
        "/api/swaps?route=" + urllib.parse.quote("eth -> sol; --"),
        "/api/swaps?partner=" + urllib.parse.quote("<script>"),
    ],
)
def test_bad_view_input_is_400(base, query):
    assert get(base + query)[0] == 400


def test_view_apis_answer(base):
    code, d = get(base + "/api/partner?id=" + urllib.parse.quote("widget-x:pda"))
    assert code == 200 and d["swaps_24h"] == 60
    code, rows = get(base + "/api/swaps?status=SUCCESS&limit=1000")
    assert code == 200 and len(rows) == 61  # capped at 200; 61 successes exist
    code, rows = get(base + "/api/swaps?partner=" + urllib.parse.quote("(none)"))
    assert code == 200 and rows == []
