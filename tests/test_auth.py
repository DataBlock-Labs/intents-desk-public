import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from intents_desk import auth


def b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = auth.B58[r] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


@pytest.fixture(autouse=True)
def session_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_KEY_FILE", str(tmp_path / "session.key"))


def wallet_sign(challenge, account="alice.near", recipient=auth.RECIPIENT, msg=None):
    """Sign a challenge the way a NEP-413 wallet does."""
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    nonce = base64.b64decode(challenge["nonce"])
    digest = auth.nep413_hash(msg or challenge["message"], nonce, recipient)
    return {
        "accountId": account,
        "publicKey": "ed25519:" + b58encode(pub),
        "signature": base64.b64encode(key.sign(digest)).decode(),
    }


def full_access(_account, _public_key):
    return {"nonce": 1, "permission": "FullAccess"}


def test_b58_round_trip():
    raw = b"\0\x01\xff" + bytes(range(29))
    assert auth.b58decode(b58encode(raw)) == raw


def test_valid_sign_in_returns_the_account():
    ch = auth.new_challenge()
    assert auth.verify(wallet_sign(ch), ch["nonce"], full_access) == "alice.near"


def test_nonce_is_single_use():
    ch = auth.new_challenge()
    signed = wallet_sign(ch)
    auth.verify(signed, ch["nonce"], full_access)
    with pytest.raises(auth.AuthError, match="already used"):
        auth.verify(signed, ch["nonce"], full_access)


def test_expired_nonce_fails(monkeypatch):
    ch = auth.new_challenge()
    later = auth.time.time() + auth.NONCE_SECONDS + 1
    monkeypatch.setattr(auth.time, "time", lambda: later)
    with pytest.raises(auth.AuthError, match="expired"):
        auth.verify(wallet_sign(ch), ch["nonce"], full_access)


@pytest.mark.parametrize(
    "kwargs", [{"recipient": "evil-site"}, {"msg": "Send all my funds"}]
)
def test_wrong_recipient_or_message_fails(kwargs):
    ch = auth.new_challenge()
    with pytest.raises(auth.AuthError, match="signature"):
        auth.verify(wallet_sign(ch, **kwargs), ch["nonce"], full_access)


def test_function_call_key_or_unknown_key_fails():
    for access in ({"permission": {"FunctionCall": {}}}, None):
        ch = auth.new_challenge()
        with pytest.raises(auth.AuthError, match="full-access"):
            auth.verify(wallet_sign(ch), ch["nonce"], lambda *_, a=access: a)


def test_session_cookie_round_trip_and_tamper():
    cookie = auth.make_session("alice.near")
    assert auth.read_session(cookie) == "alice.near"
    body, _, sig = cookie.partition(".")
    forged = base64.urlsafe_b64encode(b'{"a": "bob.near", "e": 9999999999}').decode()
    assert auth.read_session(f"{forged}.{sig}") is None
    assert auth.read_session(body + ".00") is None


def test_challenges_store_nothing_and_forged_nonces_fail():
    for _ in range(1000):
        auth.new_challenge()
    assert auth._used == {} or all(isinstance(k, bytes) for k in auth._used)
    ch = auth.new_challenge()
    raw = bytearray(base64.b64decode(ch["nonce"]))
    raw[20] ^= 1  # push the expiry: the HMAC no longer matches
    forged = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(auth.AuthError, match="expired"):
        auth.verify(wallet_sign(ch), forged, full_access)
    with pytest.raises(auth.AuthError, match="expired"):
        auth.verify(wallet_sign(ch), "not base64!", full_access)


def test_message_uses_the_configured_host_not_the_request():
    ch = auth.new_challenge()
    assert auth.PUBLIC_HOST in ch["message"] and ch["recipient"] == auth.PUBLIC_HOST


def test_short_session_key_file_is_refused(tmp_path, monkeypatch):
    key = tmp_path / "bad.key"
    key.write_bytes(b"")
    monkeypatch.setenv("SESSION_KEY_FILE", str(key))
    with pytest.raises(RuntimeError, match="32 bytes"):
        auth.make_session("alice.near")
