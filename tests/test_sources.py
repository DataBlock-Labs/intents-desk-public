import io

from intents_desk import sources


def capture_headers(monkeypatch, url):
    seen = {}

    def fake_urlopen(req, timeout):
        seen.update(req.headers)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(sources._opener, "open", fake_urlopen)
    monkeypatch.setattr(sources._public, "open", fake_urlopen)
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


def test_keyed_requests_refuse_redirects_public_ones_follow(monkeypatch):
    """An API key must never follow a 30x to another host; neardata's 302 must work."""
    import json
    import threading
    import urllib.error
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import pytest

    class Server(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith("/target"):
                body = json.dumps({"auth": self.headers.get("Authorization")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(302)
                self.send_header("Location", "/target")
                self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert sources.get_json(f"{base}/public", retries=1) == {"auth": None}
        monkeypatch.setattr(sources, "EXPLORER", base)
        monkeypatch.setenv("INTENTS_EXPLORER_JWT", "secret")
        with pytest.raises(urllib.error.HTTPError) as err:
            sources.get_json(f"{base}/keyed", retries=1)
        assert err.value.code == 302
    finally:
        server.shutdown()


def test_rpc_error_inside_result_means_none(monkeypatch):
    reply = {"result": {"error": "access key does not exist while viewing", "logs": []}}
    monkeypatch.setattr(sources, "get_json", lambda *a, **k: reply)
    assert sources.get_access_key("near", "ed25519:x") is None
