"""Telegram alerts for watched swaps.

A signed-in user gets a one-time link (t.me/<bot>?start=<code>). When they press Start,
the bot links their chat to their NEAR account. Every few minutes the alert job checks
their watched swaps and sends one plain-text message when a status changes.

The bot token is part of every Telegram API URL, so this module never puts a URL or a
raw exception text into logs or error messages.
"""

import json
import os
import secrets
import urllib.error
import urllib.request

from . import sources

CODE_MINUTES = 10
MAX_CHECKS_PER_RUN = 100


class TelegramError(RuntimeError):
    """A Telegram call failed. The message never contains the token."""


def enabled() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


def call(method: str, params: dict, timeout: int = 35) -> dict:
    api = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org")
    url = f"{api}/bot{os.environ['TELEGRAM_BOT_TOKEN']}/{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "intents-desk/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        raise TelegramError(f"telegram {method}: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise TelegramError(f"telegram {method}: no response") from None
    if not data.get("ok"):
        raise TelegramError(f"telegram {method}: {str(data.get('description'))[:100]}")
    return data["result"]


def send(chat_id: int, text: str) -> None:
    call("sendMessage", {"chat_id": chat_id, "text": text[:4000]})  # plain text


def new_link(con, account: str) -> str:
    """Make a one-time code for `account` and return the deep link to the bot."""
    code = secrets.token_urlsafe(18)
    con.execute("DELETE FROM app.link_code WHERE expires_at < now()")
    con.execute(
        "INSERT INTO app.link_code VALUES (?, ?, now() + to_minutes(?))",
        [code, account, CODE_MINUTES],
    )
    bot = (
        os.environ.get("TELEGRAM_BOT_USERNAME")
        or call("getMe", {}, timeout=10)["username"]
    )
    return f"https://t.me/{bot}?start={code}"


def linked_chat(con, account: str) -> int | None:
    row = con.execute(
        "SELECT chat_id FROM app.telegram WHERE account_id = ?", [account]
    ).fetchone()
    return row[0] if row else None


def handle_update(con, update: dict) -> None:
    """React to one Telegram update: /start <code>, /status or /stop."""
    msg = update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = str(msg.get("text") or "").strip()
    if not isinstance(chat_id, int) or not text.startswith("/"):
        return
    command, _, arg = text.partition(" ")
    if command == "/start":
        row = con.execute(
            "DELETE FROM app.link_code WHERE code = ? AND expires_at >= now() RETURNING account_id",
            [arg.strip()],
        ).fetchone()  # single use: the code is deleted as it is read
        if not row:
            send(
                chat_id,
                "This link is not valid or has expired. Make a new one on Intents Desk.",
            )
            return
        con.execute(
            "INSERT OR REPLACE INTO app.telegram (account_id, chat_id) VALUES (?, ?)",
            [row[0], chat_id],
        )
        send(
            chat_id,
            f"Linked to {row[0]}. You will get a message when a watched swap changes.",
        )
        return
    account = con.execute(
        "SELECT account_id FROM app.telegram WHERE chat_id = ?", [chat_id]
    ).fetchone()
    if not account:
        send(
            chat_id,
            "This chat is not linked. Connect it from Intents Desk after signing in.",
        )
    elif command == "/stop":
        con.execute("DELETE FROM app.telegram WHERE chat_id = ?", [chat_id])
        send(chat_id, "Unlinked. You will not get alerts any more.")
    elif command == "/status":
        rows = con.execute(
            "SELECT deposit_address, coalesce(last_status, 'not checked yet') FROM app.watch"
            " WHERE account_id = ? ORDER BY added_at",
            [account[0]],
        ).fetchall()
        lines = [f"{short(a)}: {s}" for a, s in rows] or ["No watched swaps."]
        send(chat_id, "\n".join(lines))


def poll_once(con, offset: int, wait: int = 25) -> int:
    """Read new updates (long poll) and handle them. Return the next offset."""
    for update in call(
        "getUpdates", {"offset": offset, "timeout": wait}, timeout=wait + 10
    ):
        offset = max(offset, update["update_id"] + 1)
        handle_update(con, update)
    return offset


def short(address: str) -> str:
    return address if len(address) <= 14 else f"{address[:6]}…{address[-4:]}"


def alerts_once(con) -> dict:
    """Check watched swaps of linked accounts; message each status change once.

    The first check of a newly watched swap only records its status (no message).
    Finished swaps (SUCCESS, REFUNDED, FAILED) are not checked again; the rest rotate
    by last check time, so no watched swap waits forever behind others.
    """
    rows = con.execute(
        """
        SELECT w.account_id, w.deposit_address, w.last_status, t.chat_id
        FROM app.watch w JOIN app.telegram t USING (account_id)
        WHERE coalesce(w.last_status, '') NOT IN ('SUCCESS', 'REFUNDED', 'FAILED')
        ORDER BY w.last_checked_at NULLS FIRST, w.added_at  -- oldest check first
        LIMIT ?
        """,
        [MAX_CHECKS_PER_RUN],
    ).fetchall()
    sent = changed = 0
    for account, address, last, chat_id in rows:
        status = sources.get_swap_status(address, retries=1)
        con.execute(
            "UPDATE app.watch SET last_checked_at = now()"
            " WHERE account_id = ? AND deposit_address = ?",
            [account, address],
        )
        now = status.get("status")
        if not now or now == last:
            continue
        con.execute(
            "UPDATE app.watch SET last_status = ? WHERE account_id = ? AND deposit_address = ?",
            [now, account, address],
        )
        changed += 1
        if last is None:
            continue
        details = status.get("swapDetails") or {}
        text = f"Swap {short(address)}: {last} → {now}"
        if details.get("refundReason"):
            text += f" ({details['refundReason']})"
        if now == "REFUNDED" and details.get("refundedAmountFormatted"):
            text += (
                f", {details['refundedAmountFormatted']} back to your refund address"
            )
        if now == "SUCCESS" and details.get("amountOutFormatted"):
            text += f", {details['amountOutFormatted']} delivered"
        send(chat_id, text + ".")
        sent += 1
    return {"rows": len(rows), "checked": len(rows), "changed": changed, "sent": sent}
