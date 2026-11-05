import io

from intents_desk import sources


def capture_headers(monkeypatch, url):
    seen = {}

    def fake_urlopen(req, timeout):
        seen.update(req.headers)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(sources._opener, "open", fake_urlopen)
    sources.get_json(url)
    return seen


def test_oneclick_key_goes_only_to_oneclick(monkeypatch):
    monkeypatch.setenv("ONECLICK_JWT", "k")
    assert (
        capture_headers(monkeypatch, f"{sources.ONECLICK}/tokens")["Authorization"]
        == "Bearer k"
    )
    assert "Authorization" not in capture_headers(
        monkeypatch, f"{sources.NEARDATA}/block/1"
    )


def test_no_key_no_header(monkeypatch):
    monkeypatch.delenv("ONECLICK_JWT", raising=False)
    assert "Authorization" not in capture_headers(
        monkeypatch, f"{sources.ONECLICK}/tokens"
    )


def test_explorer_key_goes_to_explorer(monkeypatch):
    monkeypatch.setenv("INTENTS_EXPLORER_JWT", "e")
    monkeypatch.delenv("ONECLICK_JWT", raising=False)
    headers = capture_headers(monkeypatch, f"{sources.EXPLORER}/transactions")
    assert headers["Authorization"] == "Bearer e"


REQUEST = {
    "originAsset": "nep141:a",
    "destinationAsset": "nep141:b",
    "amount": "100",
    "recipient": "r.near",
    "refundTo": "t.near",
    "depositType": "ORIGIN_CHAIN",
}


def capture_body(monkeypatch, **kw):
    seen = {}
    monkeypatch.setattr(
        sources,
        "get_json",
        lambda url, body, **_k: seen.update(url=url, body=body) or {},
    )
    sources.get_quote(REQUEST, **kw)
    return seen


def test_quote_carries_datablock_fee(monkeypatch):
    monkeypatch.delenv("FEE_BPS", raising=False)
    monkeypatch.delenv("FEE_RECIPIENT", raising=False)
    seen = capture_body(monkeypatch)
    assert seen["url"].endswith("/quote")
    assert seen["body"]["appFees"] == [{"recipient": "datablock.near", "fee": 40}]
    assert seen["body"]["dry"] is True and seen["body"]["amount"] == "100"


def test_fee_zero_sends_no_app_fees(monkeypatch):
    monkeypatch.setenv("FEE_BPS", "0")
    assert "appFees" not in capture_body(monkeypatch, dry=False)["body"]


def test_redirects_are_not_followed():
    """An API key header must never follow a 30x to another host."""
    import threading
    import urllib.error
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import pytest

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://evil.example/steal")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            sources.get_json(f"http://127.0.0.1:{server.server_port}/x", retries=1)
        assert err.value.code == 302
    finally:
        server.shutdown()
