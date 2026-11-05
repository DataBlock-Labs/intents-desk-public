import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from intents_desk import web


@pytest.fixture(scope="module")
def base():
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


@pytest.mark.parametrize("path", sorted(web.STATIC))
def test_allowed_static_files_are_served(base, path):
    status, headers, body = get(base + path)
    assert status == 200 and body
    assert headers["Content-Type"] == web.STATIC[path]
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


@pytest.mark.parametrize(
    "path",
    [
        "/static/../web.py",
        "/static/%2e%2e/web.py",
        "/static/web.py",
        "/index.html",
        "/static/",
        "/static/app.js/../../auth.py",
    ],
)
def test_other_paths_are_not_served(base, path):
    status, _, _ = get(base + path)
    assert status == 404


def test_near_logo_is_the_official_file_unchanged():
    from hashlib import sha256
    from importlib.resources import files

    data = files("intents_desk").joinpath("static/near-logo.svg").read_bytes()
    assert sha256(data).hexdigest().startswith("f76009dce4c0d4bb")


def test_page_inlines_the_tokens_file_unchanged(base):
    from importlib.resources import files

    _, _, page = get(base + "/")
    tokens = files("intents_desk").joinpath("static/tokens.css").read_bytes()
    assert tokens in page and web.TOKENS_LINK not in page


def test_page_inlines_both_logos_as_unchanged_bytes(base):
    import base64
    from importlib.resources import files

    _, _, page = get(base + "/")
    for name in ("intents-desk-mark.svg", "near-logo.svg"):
        raw = files("intents_desk").joinpath(f"static/{name}").read_bytes()
        assert base64.b64encode(raw) in page
        assert f"/static/{name}".encode() not in page


def post(base, length, body=b"{}"):
    import http.client
    from urllib.parse import urlparse

    u = urlparse(base)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=5)
    conn.putrequest("POST", "/api/auth/verify")
    conn.putheader("Origin", base)
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", length)
    conn.endheaders()
    conn.send(body)
    return conn.getresponse().status


@pytest.mark.parametrize("length", ["-1", "abc", "20000", "\u00b2"])
def test_bad_content_length_is_refused_quickly(base, length):
    assert post(base, length) == 413


def test_agent_format_handles_missing_usd(monkeypatch):
    status = {
        "status": "PENDING_DEPOSIT",
        "quoteResponse": {"quote": {"amountInUsd": None}},
    }
    monkeypatch.setattr(web.sources, "get_swap_status", lambda _a, **_k: status)
    assert web.swap_view("TTxfvD7a6bxixfxXdAKJqEqFZpUKtun3dq", "agent").startswith(
        "status=PENDING_DEPOSIT"
    )


def test_report_table_escapes_referral_text():
    from intents_desk.report import md_table

    out = md_table(["referral"], [("x|[claim](https://evil)<script>",)])
    assert "<script>" not in out and "x\\|" in out


def test_hsts_only_for_a_public_host(base, monkeypatch):
    _, headers, _ = get(base + "/static/tokens.css")
    assert "Strict-Transport-Security" not in headers
    monkeypatch.setattr(web.auth, "PUBLIC_HOST", "intents.datablock.pro")
    _, headers, _ = get(base + "/static/tokens.css")
    assert headers["Strict-Transport-Security"].startswith("max-age=")
