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
        " ('2026-11-01 10:00+00', 'partner-good', 'eth', 'sol', 'SUCCESS', 9, 900, NULL),"
        " ('2026-11-01 11:00+00', 'partner-good', 'eth', 'sol', 'REFUNDED', 1, 100, 100)"
    )
    daily = partner_detail(con, "partner-good")["daily"]
    assert daily == [("2026-11-01", 10, 10.0, 100.0)]


def test_unknown_partner_is_empty_not_an_error(con):
    d = partner_detail(con, "nobody")
    assert d["swaps_24h"] == 0 and d["routes"] == [] and d["hourly"] == []


def test_recent_swaps_filters(con):
    assert len(recent_swaps(con, limit=200)) == 121
    refunded = recent_swaps(con, status="REFUNDED", limit=200)
    assert refunded and {s["status"] for s in refunded} == {"REFUNDED"}
    kit = recent_swaps(con, partner="partner-good", limit=5)
    assert len(kit) == 5 and {s["partner"] for s in kit} == {"partner-good"}
    assert recent_swaps(con, route="tron → eth", limit=3)[0]["route"] == "tron → eth"
    times = [s["created_at"] for s in recent_swaps(con, limit=10)]
    assert times == sorted(times, reverse=True)
    assert all(t.endswith("Z") for t in times)  # UTC, whatever the server time zone


def test_public_list_has_no_full_address_but_ref_resolves(con, tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_KEY_FILE", str(tmp_path / "s.key"))
    from intents_desk.store import address_for_ref

    rows = recent_swaps(con, limit=200)
    full = {
        a for (a,) in con.execute("SELECT deposit_address FROM silver.swap").fetchall()
    }
    blob = json.dumps(rows)
    assert not any(a in blob for a in full)  # no full address anywhere in the response
    assert address_for_ref(con, rows[0]["ref"]) in full
    assert address_for_ref(con, "0" * 20) is None


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


def test_ref_lookup_reply_has_no_addresses(base, con, monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_KEY_FILE", str(tmp_path / "s.key"))
    from tests.fakes import STATUS

    secret = {
        **STATUS,
        "depositAddress": "SECRETDEP0001",
        "quoteResponse": {
            **STATUS["quoteResponse"],
            "quote": {
                **STATUS["quoteResponse"]["quote"],
                "depositAddress": "SECRETDEP0001",
            },
        },
    }
    monkeypatch.setattr(web.sources, "get_swap_status", lambda a, **k: secret)
    ref = recent_swaps(con, limit=1)[0]["ref"]
    code, d = get(base + "/api/swap?ref=" + ref)
    assert code == 200 and d["status"] == "REFUNDED"
    blob = json.dumps(d)
    for leaked in ("SECRETDEP0001", "TRefund", "0xabc", "dep0"):
        assert leaked not in blob
    assert get(base + "/api/swap?ref=" + "0" * 20)[0] == 404
    assert get(base + "/api/quote?ref=" + ref)[0] == 400  # quotes need the real address


def test_masked_partners_round_trip(base, monkeypatch, tmp_path):
    monkeypatch.setenv("SESSION_KEY_FILE", str(tmp_path / "s.key"))
    monkeypatch.setenv("MASK_PARTNERS", "1")
    alias = web.mask_partner("widget-x:pda")
    assert alias.startswith("partner-") and "widget" not in alias
    code, d = get(base + "/api/partner?id=" + alias)
    assert code == 200 and d["swaps_24h"] == 60 and d["partner"] == alias
    code, rows = get(base + "/api/swaps?partner=" + alias)
    assert code == 200 and rows and {r["partner"] for r in rows} == {alias}
    assert get(base + "/api/partner?id=partner-000000")[0] == 404
    code, rows = get(base + "/api/partners")
    assert code == 200 and all("widget" not in r[0] for r in rows)


def test_swap_api_only_from_page(base, monkeypatch):
    monkeypatch.setenv("BROWSER_ONLY", "1")
    assert get(base + "/api/swap?address=abc")[0] == 403
    assert get(base + "/api/quote?address=abc")[0] == 403
    req = urllib.request.Request(
        base + "/api/swap?address=abc", headers={"Sec-Fetch-Site": "same-origin"}
    )
    try:
        code = urllib.request.urlopen(req).status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 400  # past the gate; a bad address, so no 1Click call


def test_daily_rule_flags_slow_failure(monkeypatch):
    class Rows:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    hourly = [("slow-app", 10, 60.0, 5, 3, "X", "t", False)]
    daily = [("slow-app", 400, 66.9, 900, 50, "X", "t", True)]
    monkeypatch.setattr(web, "db", lambda: None)
    monkeypatch.setattr(
        web.store,
        "partner_report",
        lambda con, h, n, p: Rows(daily if h == 24 else hourly),
    )
    assert web.partner_rows()[0][7] is True
