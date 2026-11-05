"""NEAR wallet sign-in: NEP-413 signed messages and signed session cookies.

The browser wallet signs a message that holds a one-time nonce from this server. No
transaction is made and no key leaves the wallet. Spec:
https://github.com/near/NEPs/blob/master/neps/nep-0413.md
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import threading
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# The site's public host, fixed by config, never taken from the request. Wallets show it
# as the NEP-413 recipient, so a phishing site cannot pass off our challenge as its own.
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "localhost:8420")
RECIPIENT = PUBLIC_HOST
NONCE_SECONDS = 300
SESSION_SECONDS = 7 * 24 * 3600
NEP413_TAG = 2**31 + 413
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

_used: dict[
    bytes, int
] = {}  # nonces already used -> expiry; only signed-in nonces land here
_lock = threading.Lock()


class AuthError(Exception):
    """The sign-in is not valid. The message is safe to show to the user."""


def b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + raw


def _borsh_str(text: str) -> bytes:
    data = text.encode()
    return struct.pack("<I", len(data)) + data


def nep413_hash(message: str, nonce: bytes, recipient: str) -> bytes:
    """Get the bytes a wallet signs for NEP-413 (no callback URL)."""
    payload = _borsh_str(message) + nonce + _borsh_str(recipient) + b"\x00"
    return hashlib.sha256(struct.pack("<I", NEP413_TAG) + payload).digest()


def _message(expiry: int) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expiry))
    return f"Sign in to Intents Desk at {PUBLIC_HOST}. No transaction. Expires {stamp}."


def _nonce_mac(head: bytes) -> bytes:
    return hmac.new(_session_key(), b"nonce" + head, hashlib.sha256).digest()[:8]


def new_challenge() -> dict:
    """Make a one-time nonce and the message the user signs.

    The nonce is 16 random bytes + expiry (8) + an HMAC of both (8). The server stores
    nothing until a sign-in succeeds, so no number of challenge requests can fill memory
    or lock other users out.
    """
    expiry = int(time.time()) + NONCE_SECONDS
    head = secrets.token_bytes(16) + expiry.to_bytes(8, "big")
    nonce = head + _nonce_mac(head)
    return {
        "nonce": base64.b64encode(nonce).decode(),
        "message": _message(expiry),
        "recipient": RECIPIENT,
    }


def verify(signed: dict, nonce_b64: str, access_key_of) -> str:
    """Check a NEP-413 sign-in and return the account id.

    `signed` is {accountId, publicKey, signature} from the wallet. `access_key_of`
    gets the RPC access key for (account, public key), or None if there is none.
    """
    try:
        nonce = base64.b64decode(nonce_b64, validate=True)
    except ValueError:
        nonce = b""
    head, mac = nonce[:24], nonce[24:]
    expiry = int.from_bytes(head[16:], "big") if len(nonce) == 32 else 0
    if not hmac.compare_digest(mac, _nonce_mac(head)) or expiry < time.time():
        raise AuthError("The sign-in request expired. Try again.")
    account = str(signed.get("accountId", ""))
    public_key = str(signed.get("publicKey", ""))
    if not account or not public_key.startswith("ed25519:"):
        raise AuthError("The wallet did not send an ed25519 signature.")
    try:
        key = Ed25519PublicKey.from_public_bytes(b58decode(public_key[8:]))
        signature = base64.b64decode(str(signed.get("signature", "")))
        key.verify(signature, nep413_hash(_message(expiry), nonce, RECIPIENT))
    except (InvalidSignature, ValueError):
        raise AuthError("The signature is not valid.") from None
    with _lock:  # single use: a valid signature can be replayed only once
        now = time.time()
        for k in [k for k, exp in _used.items() if exp < now]:
            del _used[k]
        if nonce in _used:
            raise AuthError("This sign-in request was already used. Try again.")
        _used[nonce] = expiry
    access = access_key_of(account, public_key)
    if not access or access.get("permission") != "FullAccess":
        raise AuthError("That key is not a full-access key of this account.")
    return account


_key_lock = threading.Lock()


def _session_key() -> bytes:
    """Get the 32-byte server key; make it on first run (atomic, mode 600)."""
    path = Path(
        os.environ.get(
            "SESSION_KEY_FILE", Path.home() / ".config/intents-desk/session.key"
        )
    )
    with _key_lock:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))
            os.replace(tmp, path)
        key = path.read_bytes()
    if len(key) != 32:
        raise RuntimeError(f"{path} must hold 32 bytes; delete it to make a new one")
    return key


def make_session(account: str) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps({"a": account, "e": int(time.time()) + SESSION_SECONDS}).encode()
    ).decode()
    sig = hmac.new(_session_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session(cookie: str) -> str | None:
    """Get the account id from a session cookie, or None if it is not valid."""
    body, _, sig = cookie.partition(".")
    good = hmac.new(_session_key(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body))
    except ValueError:
        return None
    return data["a"] if data.get("e", 0) > time.time() else None
