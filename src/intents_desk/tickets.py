"""Public pseudo-tickets: support cases that someone chose to make public.

A ticket is opened from the page ("Report publicly"), from Telegram (/report), or from
an X or Reddit post in the feed that names a swap 1Click knows. The deposit address is
kept here so the ticket can be re-checked; it is never served. The public view is the
reduced case (support.view full=False): status, reason, route, chain refs without the
deposit, refund or payout tx.
"""

import json
import os
import re

from . import sources, store, support

SCHEMA = """
CREATE TABLE IF NOT EXISTS app.ticket (
    ref TEXT PRIMARY KEY,
    deposit_address TEXT NOT NULL,  -- server only; never in a reply
    source TEXT NOT NULL,           -- web | telegram | x | reddit
    source_url TEXT,
    opened_at TIMESTAMPTZ DEFAULT now(),
    last_status TEXT,
    last_reason TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);
"""
SOURCES = ("web", "telegram", "x", "reddit")
# Swap keys people paste in posts: EVM 0x…, Tron T…, NEAR implicit hex, base58 hashes.
KEY = re.compile(
    r"\b(0x[0-9a-fA-F]{40,64}|T[1-9A-HJ-NP-Za-km-z]{33}|[0-9a-f]{64}|[1-9A-HJ-NP-Za-km-z]{43,88})\b"
)


def ensure(con) -> None:
    con.execute(SCHEMA)


def link(ref: str) -> str:
    return f"https://{os.environ.get('PUBLIC_HOST', 'localhost:8420')}/#case={ref}"


def resolve(key: str) -> tuple[str, dict] | None:
    """(deposit address, 1Click status) for a deposit address or a tx/intent hash."""
    data = sources.get_swap_status(key, retries=1)
    if data.get("status"):
        return key, data
    if not support.HASH.match(key):
        return None
    try:
        row = sources.search_explorer(key)  # the shared Explorer slot: may be busy
    except (sources.ExplorerBusy, OSError, KeyError, ValueError, RuntimeError):
        return None
    if not row:
        return None
    data = sources.get_swap_status(row["depositAddress"], retries=1)
    return (row["depositAddress"], data) if data.get("status") else None


def report(con, key: str, source: str, source_url: str | None = None) -> dict | None:
    """Open (or find) the ticket for a swap key. None when 1Click does not know it.

    Returns the public case plus `new` (True when this call opened the ticket).
    """
    assert source in SOURCES
    found = resolve(key.strip())
    if not found:
        return None
    address, data = found
    ref = store.ref_of(address)
    case = support.build(con, data, ref)
    support.save(con, case)
    new = con.execute(
        "INSERT INTO app.ticket (ref, deposit_address, source, source_url, last_status,"
        " last_reason) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING ref",
        [ref, address, source, source_url, case["status"], case["reason"]],
    ).fetchone()
    return support.view(con, case, full=False) | {"new": new is not None}


def listing(con, limit: int = 20) -> list[dict]:
    """The newest public tickets. No address: only the ref, which links to the case."""
    rows = con.execute(
        "SELECT t.ref, t.source, t.source_url, CAST(t.opened_at AS VARCHAR),"
        " CAST(t.updated_at AS VARCHAR), t.last_status, t.last_reason, c.case_json"
        " FROM app.ticket t LEFT JOIN app.support_case c USING (ref)"
        " ORDER BY t.opened_at DESC LIMIT ?",
        [limit],
    ).fetchall()
    out = []
    for ref, source, url, opened, updated, status, reason, case_json in rows:
        route = None
        if case_json:
            route = json.loads(case_json).get("route")
        out.append(
            {
                "ref": ref,
                "source": source,
                "source_url": url if source in ("x", "reddit") else None,
                "opened_at": opened,
                "updated_at": updated,
                "status": status,
                "reason": reason,
                "route": route,
            }
        )
    return out


def refresh(con, limit: int = 20) -> list[dict]:
    """Re-check unfinished tickets; return the ones whose status changed."""
    rows = con.execute(
        "SELECT ref, deposit_address, last_status FROM app.ticket"
        " WHERE coalesce(last_status, '') NOT IN ('SUCCESS', 'REFUNDED', 'FAILED')"
        " ORDER BY updated_at LIMIT ?",
        [limit],
    ).fetchall()
    changed = []
    for ref, address, last in rows:
        data = sources.get_swap_status(address, retries=1)
        con.execute("UPDATE app.ticket SET updated_at = now() WHERE ref = ?", [ref])
        status = data.get("status")
        if not status or status == last:
            continue
        case = support.build(con, data, ref)
        support.save(con, case)  # no lookup count: this is the server, not a visitor
        con.execute(
            "UPDATE app.ticket SET last_status = ?, last_reason = ? WHERE ref = ?",
            [status, case["reason"], ref],
        )
        changed.append({"ref": ref, "from": last, "to": status, "reason": case["reason"]})  # fmt: skip
    return changed


def keys_in(text: str) -> list[str]:
    """Swap keys named in a post, at most 3."""
    return list(dict.fromkeys(KEY.findall(text or "")))[:3]


def channel_text(ticket: dict, change: dict | None = None) -> str:
    """One public line for the channel. No address, no partner name."""
    ref = ticket["ref"]
    if change:
        head = f"Ticket {ref[:8]}: {change['from'] or 'new'} → {change['to']}"
        reason = change.get("reason")
    else:
        head = (
            f"New ticket {ref[:8]} ({ticket.get('source', 'web')}): {ticket['status']}"
        )
        reason = ticket.get("reason")
    route = f" · {ticket['route']}" if ticket.get("route") else ""
    why = f" · {reason}" if reason else ""
    return f"{head}{why}{route}\n{link(ref)}"
