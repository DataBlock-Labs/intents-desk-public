"""X posts that the owner approves one by one in Telegram, then the server posts.

Each draft is shown to the owner with buttons. A press is the owner's explicit yes for
that one post. Posting runs X's official `xurl` CLI (OAuth 1.0a keys in ~/.xurl; this
code never reads them) and only where X_POST=1. A draft is single use and expires after
24 hours. Costs (X API, 2026): $0.015 a post, $0.20 when the post holds a link.
"""

import json
import os
import re
import secrets
import subprocess

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


def run_xurl(args: list[str]) -> str:
    """Run xurl with an argument list (never a shell); return the new post id."""
    out = subprocess.run(
        ["xurl", "--auth", "oauth1", *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"xurl exit {out.returncode}"
        )  # stderr may hold request details
    try:
        return str(json.loads(out.stdout)["data"]["id"])
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("xurl gave no post id") from None


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
    if not enabled():
        con.execute(
            "UPDATE app.x_draft SET status = 'pending' WHERE id = ?", [draft_id]
        )
        return (
            "Posting is off on this server (X_POST is not 1). The draft is still open."
        )
    try:
        if kind == "reply":
            body = f"{text}\n{link}" if action == "link" and link else text
            post_id = run_xurl(["reply", reply_to, body])
        else:
            post_id = run_xurl(["post", text])
            if action == "link" and link:
                run_xurl(["reply", post_id, link])
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        con.execute("UPDATE app.x_draft SET status = 'failed' WHERE id = ?", [draft_id])
        return (
            f"Posting failed ({type(exc).__name__}). Check `xurl --auth oauth1 whoami`."
        )
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
