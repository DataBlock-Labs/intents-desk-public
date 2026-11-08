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
import time
import urllib.error
import urllib.request

from . import sources

CODE_MINUTES = 10
MAX_CHECKS_PER_RUN = 100
# Public channel posts per hour, per kind: ticket spam cannot crowd out incident posts.
CHANNEL_PER_HOUR = {"ticket": 15, "incident": 10, "alert": 10}
_channel_sent: dict[str, list[float]] = {}  # shortcut: in memory, one server process
COMMANDS = [
    ("report", "Report a swap: /report <deposit address or tx hash>"),
    ("alert", "Custom alerts: /alert route tron eth 20, /alert list"),
    ("status", "Your watched swaps"),
    ("stop", "Unlink this chat"),
    ("help", "What this bot does"),
]
HELP = (
    "Intents Desk bot.\n"
    "/report <deposit address or tx hash>: check a NEAR Intents (1Click) swap and open a"
    " public ticket for it. The ticket shows status and reason, never your address.\n"
    "/status: your watched swaps (after you link this chat on Intents Desk).\n"
    "/alert: custom alerts on routes, reasons, partners or one swap. Send /alert help.\n"
    "/stop: unlink this chat."
)


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


def send(chat_id: int | str, text: str, buttons: list | None = None) -> None:
    """Plain text; `buttons` is rows of (label, callback data) shown under the message."""
    params: dict = {"chat_id": chat_id, "text": text[:4000]}
    if buttons:
        params["reply_markup"] = {
            "inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in buttons
            ]
        }
    call("sendMessage", params)


def channel_post(text: str, kind: str = "ticket") -> bool:
    """Post to the public channel (TELEGRAM_CHANNEL). False when off or over the cap."""
    channel = os.environ.get("TELEGRAM_CHANNEL")
    if not (channel and enabled()):
        return False
    now = time.time()
    sent = _channel_sent.setdefault(kind, [])
    sent[:] = [t for t in sent if now - t < 3600]
    if len(sent) >= CHANNEL_PER_HOUR[kind]:
        return False
    send(channel, text)
    sent.append(now)
    return True


def set_commands() -> None:
    """Show the commands in the bot menu."""
    call(
        "setMyCommands",
        {"commands": [{"command": c, "description": d} for c, d in COMMANDS]},
    )


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


def bot_url() -> str | None:
    """Link to the bot, from TELEGRAM_BOT_USERNAME (no API call)."""
    name = os.environ.get("TELEGRAM_BOT_USERNAME")
    return f"https://t.me/{name}" if name and enabled() else None


def auto_watch(con, account: str) -> bool:
    row = con.execute(
        "SELECT coalesce(auto_watch, true) FROM app.telegram WHERE account_id = ?",
        [account],
    ).fetchone()
    return bool(row and row[0])


def linked_chat(con, account: str) -> int | None:
    row = con.execute(
        "SELECT chat_id FROM app.telegram WHERE account_id = ?", [account]
    ).fetchone()
    return row[0] if row else None


def handle_update(con, update: dict) -> None:
    """React to one Telegram update: a button press, /start <code>, /report, /help,
    /status or /stop."""
    if update.get("callback_query"):
        handle_button(con, update["callback_query"])
        return
    msg = update.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    text = str(msg.get("text") or "").strip()
    if not isinstance(chat_id, int) or not text.startswith("/"):
        return
    command, _, arg = text.partition(" ")
    command = command.split("@", 1)[0]  # /report@IntentsDeskBot in groups
    if command == "/help":
        send(chat_id, HELP)
        return
    if command == "/report":
        report_command(con, chat_id, arg.strip())
        return
    if command == "/alert":
        alert_command(con, chat_id, arg.strip())
        return
    if command == "/start" and not arg.strip():
        send(chat_id, HELP)
        return
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


def report_command(con, chat_id: int, key: str) -> None:
    """/report <key>: open a public ticket and answer with its status and link."""
    from . import tickets
    from .web import allow

    if not allow(f"tg:{chat_id}"):  # each report costs 1Click and Explorer calls
        send(chat_id, "Too many reports. Wait a minute.")
        return
    if not key or len(key) > 128 or not tickets.KEY.fullmatch(key):
        send(chat_id, "Send /report followed by the deposit address or tx hash of the swap.")  # fmt: skip
        return
    ticket = tickets.report(con, key, "telegram")
    if not ticket:
        send(chat_id, "1Click does not know this address or hash. Check it and try again.")  # fmt: skip
        return
    reason = f" ({ticket['reason']})" if ticket["reason"] else ""
    send(
        chat_id,
        f"Ticket {ticket['ref'][:8]}: {ticket['status']}{reason}.\n{ticket['answer']}\n"
        f"Next: {ticket['next'] or '–'}\nPublic ticket (no address shown): {tickets.link(ticket['ref'])}",
    )
    account = con.execute(
        "SELECT account_id FROM app.telegram WHERE chat_id = ?", [chat_id]
    ).fetchone()
    address = con.execute(
        "SELECT deposit_address FROM app.ticket WHERE ref = ?", [ticket["ref"]]
    ).fetchone()[0]
    # Watch only when the user typed the deposit address itself: a watch list shows
    # addresses, and a public tx hash must never reveal someone's deposit address.
    if account and address == key:
        from . import store

        try:
            store.set_watch(con, account[0], address, True)
        except ValueError:
            pass
    if ticket["new"]:
        channel_post(tickets.channel_text(ticket))


def alert_command(con, chat_id: int, text: str) -> None:
    """/alert …: add, list or stop custom alerts for the linked account."""
    from . import alerts, incidents
    from .web import allow

    if not allow(f"tg:{chat_id}"):
        send(chat_id, "Too many requests. Wait a minute.")
        return
    account = con.execute(
        "SELECT account_id FROM app.telegram WHERE chat_id = ?", [chat_id]
    ).fetchone()
    if not account:
        send(chat_id, "Link this chat first: sign in on Intents Desk and press Connect Telegram.")  # fmt: skip
        return
    try:
        cmd = alerts.parse(text, owner=chat_id == incidents.owner_chat(con))
        send(chat_id, alerts.run(con, account[0], cmd))
    except alerts.RuleError as exc:
        send(chat_id, str(exc))


def handle_button(con, query: dict) -> None:
    """An inline button press. Only the owner's chat may approve X posts."""
    from . import incidents, xpost

    chat_id = ((query.get("message") or {}).get("chat") or {}).get("id")
    user_id = (query.get("from") or {}).get("id")
    data = str(query.get("data") or "")
    try:
        call("answerCallbackQuery", {"callback_query_id": query.get("id")}, timeout=10)
    except TelegramError:
        pass
    owner = incidents.owner_chat(con)
    if owner is None or chat_id != owner or user_id != owner:
        return  # not the owner's own private chat: ignore silently
    kind, _, rest = data.partition(":")
    action, _, draft_id = rest.partition(":")
    if kind != "x" or not draft_id:
        return
    send(chat_id, xpost.decide(con, draft_id, action))


def poll_once(con, offset: int, wait: int = 25) -> int:
    """Read new updates (long poll) and handle them. Return the next offset."""
    for update in call(
        "getUpdates", {"offset": offset, "timeout": wait}, timeout=wait + 10
    ):
        offset = max(offset, update["update_id"] + 1)
        try:
            handle_update(con, update)
        except (TelegramError, OSError, ValueError, RuntimeError, KeyError) as exc:
            # skip it: a reply we cannot send must not replay the update forever
            print(f"[bot] update skipped: {type(exc).__name__}", flush=True)
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
