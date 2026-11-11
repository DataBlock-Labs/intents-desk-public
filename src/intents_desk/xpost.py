"""X posts that the owner approves one by one in Telegram, then the server posts.

Each draft is shown to the owner with buttons. A press is the owner's explicit yes for
that one post. Posting signs the request with OAuth 1.0a (X_CONSUMER_KEY, X_CONSUMER_SECRET,
X_ACCESS_TOKEN, X_TOKEN_SECRET in the server env) and only where X_POST=1. A draft is single use and expires after
24 hours. Costs (X API, 2026): $0.015 a post, $0.20 when the post holds a link.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS app.x_draft (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,          -- incident | reply
    text TEXT NOT NULL,
    link TEXT,
    reply_to TEXT,               -- the X post id a reply answers
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | posting | posted | skipped | expired | failed
    post_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    decided_at TIMESTAMPTZ
);
"""
MAX_CHARS = 280
EXPIRE_HOURS = 24
URL = re.compile(r"https?://|www\.", re.IGNORECASE)
ACTIONS = ("post", "link", "skip")


def ensure(con) -> None:
    con.execute(SCHEMA)


def enabled() -> bool:
    return os.environ.get("X_POST") == "1"


def new_draft(con, kind: str, text: str, link: str | None = None, reply_to: str | None = None) -> str:  # fmt: skip
    """Store a draft and return its id. The body has no link (X bills links at $0.20)."""
    if kind not in ("incident", "reply"):
        raise ValueError("kind must be incident or reply")
    if len(text) > MAX_CHARS or URL.search(text):
        raise ValueError("draft text must be at most 280 characters with no link")
    if reply_to is not None and not re.fullmatch(r"\d{1,25}", reply_to):
        raise ValueError("reply_to must be an X post id")
    draft_id = secrets.token_urlsafe(9)
    con.execute(
        "INSERT INTO app.x_draft (id, kind, text, link, reply_to) VALUES (?, ?, ?, ?, ?)",
        [draft_id, kind, text, link, reply_to],
    )
    return draft_id


def buttons(con, draft_id: str) -> list[list[tuple[str, str]]]:
    kind, link = con.execute(
        "SELECT kind, link FROM app.x_draft WHERE id = ?", [draft_id]
    ).fetchone()
    word = "Reply" if kind == "reply" else "Post"
    row = [(f"{word} ($0.015)", f"x:post:{draft_id}")]
    if link:
        extra = "with link ($0.20)" if kind == "reply" else "+ link reply ($0.215)"
        row.append((f"{word} {extra}", f"x:link:{draft_id}"))
    return [row, [("Skip", f"x:skip:{draft_id}")]]


def preview(con, draft_id: str) -> str:
    kind, text, link, reply_to = con.execute(
        "SELECT kind, text, link, reply_to FROM app.x_draft WHERE id = ?", [draft_id]
    ).fetchone()
    head = f"X {kind} draft" + (f" (reply to x.com/i/status/{reply_to})" if reply_to else "")  # fmt: skip
    tail = f"\n\nLink (only with the link button): {link}" if link else ""
    return f"{head}. Nothing is posted until you press a button.\n\n{text}{tail}"


TWEETS = "https://api.x.com/2/tweets"
KEYS = ("X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_TOKEN_SECRET")


def oauth1_header(method: str, url: str, params: dict | None = None, nonce: str | None = None, timestamp: str | None = None) -> str:  # fmt: skip
    """The OAuth 1.0a HMAC-SHA1 Authorization header. A JSON body is not signed."""
    ck, cs, tok, ts = (os.environ[k] for k in KEYS)
    oauth = {
        "oauth_consumer_key": ck,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp or str(int(time.time())),
        "oauth_token": tok,
        "oauth_version": "1.0",
    }
    pairs = sorted(
        (quote(k, ""), quote(v, "")) for k, v in {**(params or {}), **oauth}.items()
    )
    base = "&".join(
        quote(x, "") for x in (method, url, "&".join(f"{k}={v}" for k, v in pairs))
    )
    key = f"{quote(cs, '')}&{quote(ts, '')}".encode()
    oauth["oauth_signature"] = base64.b64encode(
        hmac.new(key, base.encode(), hashlib.sha1).digest()
    ).decode()
    return "OAuth " + ", ".join(
        f'{k}="{quote(v, "")}"' for k, v in sorted(oauth.items())
    )


def post(text: str, reply_to: str | None = None) -> str:
    """Post to X as the owner's account; return the new post id."""
    body: dict = {"text": text}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}
    req = urllib.request.Request(TWEETS, json.dumps(body).encode(), method="POST", headers={
        "Authorization": oauth1_header("POST", TWEETS),
        "Content-Type": "application/json",
    })  # fmt: skip
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return str(json.load(r)["data"]["id"])
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"X said HTTP {e.code}"
        ) from None  # the body may echo the request
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("X gave no post id") from None


def decide(con, draft_id: str, action: str) -> str:
    """Apply the owner's button press. Returns the text to show the owner."""
    if action not in ACTIONS:
        return "Unknown action."
    row = con.execute(
        "UPDATE app.x_draft SET status = CASE WHEN created_at < now() - to_hours(?)"
        " THEN 'expired' WHEN ? = 'skip' THEN 'skipped' ELSE 'posting' END,"
        " decided_at = now() WHERE id = ? AND status = 'pending'"
        " RETURNING status, kind, text, link, reply_to",
        [EXPIRE_HOURS, action, draft_id],
    ).fetchone()  # the claim: a second press finds no pending row
    if not row:
        return "This draft was already handled."
    status, kind, text, link, reply_to = row
    if status == "expired":
        return "This draft is older than 24 hours and expired. Nothing was posted."
    if status == "skipped":
        return "Skipped. Nothing was posted."
    if not enabled() or not all(os.environ.get(k) for k in KEYS):
        con.execute(
            "UPDATE app.x_draft SET status = 'pending' WHERE id = ?", [draft_id]
        )
        return (
            "Posting is off on this server (X_POST=1 and the 4 X_* keys are needed)."
            " The draft is still open."
        )
    try:
        if kind == "reply":
            body = f"{text}\n{link}" if action == "link" and link else text
            post_id = post(body, reply_to)
        else:
            post_id = post(text)
            if action == "link" and link:
                post(link, post_id)
    except (RuntimeError, OSError) as exc:
        con.execute("UPDATE app.x_draft SET status = 'failed' WHERE id = ?", [draft_id])
        return f"Posting failed: {exc}. Check that the X app has Read and Write access."
    con.execute(
        "UPDATE app.x_draft SET status = 'posted', post_id = ? WHERE id = ?",
        [post_id, draft_id],
    )
    return f"Posted: https://x.com/i/status/{post_id}"


def offer(con, chat: int, kind: str, text: str, link: str | None = None, reply_to: str | None = None) -> str:  # fmt: skip
    """Store a draft and send it to the owner's chat with buttons. Returns the draft id."""
    from . import telegram

    draft_id = new_draft(con, kind, text, link, reply_to)
    telegram.send(chat, preview(con, draft_id), buttons(con, draft_id))
    return draft_id
