"""Support cases: one swap explained in plain words, with public chain references.

A case keeps only public facts: status, reason, route, amounts, tx hashes, block heights
and explorer links. It never keeps a deposit, recipient or refund address, a memo or the
raw 1Click reply (1Click terms: raw data at most 24 h). Cases live in our own database,
so a case link still works after 1Click data is gone.
"""

import json
import os
import re
from datetime import UTC, datetime
from urllib.parse import quote, urlparse

from . import sources

HASH = re.compile(r"^(0x)?[A-Za-z0-9]{16,128}$")  # tx hashes on all chains: no URL text
NEAR_TX = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{43,44}$")
SPIKE = 10  # lookups with one reason in one hour: tell the owner
RPC_BUDGET = 20  # RPC block lookups per call, so one run cannot hold the sync loop

SCHEMA = """
CREATE TABLE IF NOT EXISTS app.support_case (
    ref TEXT PRIMARY KEY,           -- public ref (keyed hash of the deposit address)
    first_seen TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ,
    case_json JSON NOT NULL         -- the public case, see build()
);
CREATE TABLE IF NOT EXISTS ops.support_lookup (
    hour TIMESTAMPTZ, status TEXT, reason TEXT, route TEXT, incident BOOLEAN,
    lookups INTEGER NOT NULL,
    PRIMARY KEY (hour, status, reason, route, incident)
);
CREATE TABLE IF NOT EXISTS app.tx_block (
    tx_hash TEXT PRIMARY KEY, block_height BIGINT, block_ts TIMESTAMPTZ
);
"""

# refundReason -> (what happened, what to do). Codes seen on 1Click, Nov 2026.
REASONS = {
    "QUOTE_EXPIRED": (
        "The deposit arrived after the quote deadline, so no solver could fill it.",
        "Get a new quote and send again, before its deadline.",
    ),
    "NO_LIQUIDITY": (
        "No solver had enough liquidity for this route and amount.",
        "Try a smaller amount or another route, or try again later.",
    ),
    "INTENT_SUBMIT_FAILED": (
        "1Click could not submit the swap intent on NEAR, so the swap did not start.",
        "This is not a mistake on your side. If many swaps show it, it is an incident.",
    ),
    "AMOUNT_MORE_THAN_BALANCE": (
        "The swap asked for more than the balance that was deposited.",
        "Check the amount and the deposit, then get a new quote.",
    ),
    "PARTIAL_DEPOSIT": (
        "Less than the quoted amount arrived, so the deposit was refunded.",
        "Send the full quoted amount with a new quote.",
    ),
    "AMOUNT_LESS_THAN_MIN_WITHDRAWABLE": (
        "The amount was below the smallest amount that the chain can withdraw.",
        "Use a larger amount.",
    ),
    "INSUFFICIENT_BALANCE": (
        "The NEAR Intents balance was too small when the swap ran. No funds moved.",
        "Check your NEAR Intents balance, then try again.",
    ),
}

ANSWER = {
    "SUCCESS": "Delivered.",
    "REFUNDED": "Your funds came back to your refund address.",
    "FAILED": "The swap failed.",
    "PROCESSING": "Still working. Most swaps finish within a few minutes.",
    "KNOWN_DEPOSIT_TX": "Your deposit was seen. 1Click waits for it to confirm.",
    "PENDING_DEPOSIT": "1Click is waiting for your deposit.",
    "INCOMPLETE_DEPOSIT": "Your deposit was less than the quote.",
}
NEXT = {
    "SUCCESS": "Nothing to do.",
    "REFUNDED": "You can get a new quote and send again.",
    "FAILED": "Copy the support message and send it to NEAR Intents support.",
    "PROCESSING": "Wait. This page checks again every 20 seconds.",
    "KNOWN_DEPOSIT_TX": "Wait. This page checks again every 20 seconds.",
    "PENDING_DEPOSIT": "Send the deposit before the quote deadline.",
    "INCOMPLETE_DEPOSIT": "Send the rest, or wait for the refund.",
}
FINAL = {"SUCCESS": "Delivered", "REFUNDED": "Refunded", "FAILED": "Failed"}


def ensure(con) -> None:
    con.execute(SCHEMA)


def _iso(ts) -> str | None:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def block_of(con, tx_hash: str, rpc: bool = True) -> tuple[int, str] | None:
    """(block height, ISO time) of a NEAR tx: from our index, our cache, then the RPC."""
    iso = "strftime(block_ts AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%SZ')"
    row = con.execute(
        f"SELECT block_height, {iso} FROM silver.event WHERE tx_hash = ?"
        f" UNION ALL SELECT block_height, {iso} FROM app.tx_block WHERE tx_hash = ?"
        " LIMIT 1",
        [tx_hash, tx_hash],
    ).fetchone()
    if row:  # a NULL height is a cached miss: the RPC did not know the tx
        return (row[0], row[1]) if row[0] is not None else None
    if not rpc or os.environ.get("NEAR_BLOCK_LOOKUP") == "0":
        return None
    try:
        found = sources.get_tx_block(tx_hash)
    except (OSError, ValueError, KeyError, RuntimeError):  # nice to have: never fail
        return None
    if not found:
        con.execute(
            "INSERT OR IGNORE INTO app.tx_block VALUES (?, NULL, NULL)", [tx_hash]
        )
        return None
    ts = datetime.fromtimestamp(found[1] / 1e9, UTC)
    con.execute(
        "INSERT OR IGNORE INTO app.tx_block VALUES (?, ?, ?)", [tx_hash, found[0], ts]
    )
    return found[0], _iso(ts)


def add_blocks(con, evidence: list[dict], budget: int = RPC_BUDGET) -> list[dict]:
    """Fill `block` on evidence rows that have a NEAR tx. Uses at most `budget` RPC
    lookups; the rest come from the cache on a later run."""
    for e in evidence:
        if e.get("block") or not e.get("near_tx"):
            continue
        cached = block_of(con, e["near_tx"][0], rpc=False)
        if not cached and budget > 0:
            budget -= 1
            cached = block_of(con, e["near_tx"][0])
        e["block"] = cached[0] if cached else None
    return evidence


def _url(url) -> str | None:
    """Keep only https explorer links from 1Click."""
    parts = urlparse(str(url or ""))
    return url if parts.scheme == "https" and parts.netloc else None


def _refs(con, data: dict) -> list[dict]:
    from .web import chain_of

    req = (data.get("quoteResponse") or {}).get("quoteRequest") or {}
    d = data.get("swapDetails") or {}
    origin, dest = (
        chain_of(req.get("originAsset", "")),
        chain_of(req.get("destinationAsset", "")),
    )
    status = data.get("status")
    refs = []
    for h in d.get("originChainTxHashes") or []:
        if HASH.match(str(h.get("hash", ""))):
            refs.append({"chain": origin, "kind": "deposit", "hash": h["hash"], "url": _url(h.get("explorerUrl"))})  # fmt: skip
    for i, h in enumerate(d.get("nearTxHashes") or []):
        if NEAR_TX.match(str(h)):
            block = block_of(con, h, rpc=i < 4)
            refs.append({"chain": "near", "kind": "intent", "hash": h,
                         "url": f"https://nearblocks.io/txns/{h}",
                         "block": block[0] if block else None, "at": block[1] if block else None})  # fmt: skip
    if status == "REFUNDED":
        kind, chain = (
            "refund",
            origin if req.get("refundType") == "ORIGIN_CHAIN" else "near",
        )
    else:
        kind, chain = (
            "delivery",
            dest if req.get("recipientType") == "DESTINATION_CHAIN" else "near",
        )
    for h in d.get("destinationChainTxHashes") or []:
        if HASH.match(str(h.get("hash", ""))):
            refs.append({"chain": chain, "kind": kind, "hash": h["hash"], "url": _url(h.get("explorerUrl"))})  # fmt: skip
    return refs


def incident_for(con, referral: str | None, at: str | None) -> str | None:
    """The id of the incident this swap falls in: its partner first, else the network."""
    if not at:
        return None
    row = con.execute(
        "SELECT id FROM ops.incident WHERE started_at <= $t"
        " AND $t < coalesce(ended_at, now() + INTERVAL 1 HOUR)"
        " AND (referral = $r OR scope = 'network')"
        " ORDER BY scope = 'network' LIMIT 1",
        {"t": at, "r": referral or "(none)"},
    ).fetchone()
    return row[0] if row else None


def view(con, case: dict, full: bool) -> dict:
    """The case as served. The partner mask is applied now, not when it was saved.

    full=False (case link, public list, lookup by hash): no deposit or payout tx,
    because their explorer pages show the addresses. With MASK_PARTNERS=1 and an
    incident, no NEAR tx either: the intent log on nearblocks names the partner."""
    from . import incidents

    inc = case.get("incident_id") and incidents.get(con, case["incident_id"])
    pub = incidents.public(inc) if inc else None
    hide = set() if full else {"deposit", "refund", "delivery"}
    if pub and os.environ.get("MASK_PARTNERS") == "1":
        hide.add("intent")
    refs = [r for r in case["refs"] if r["kind"] not in hide]
    shown = {r["hash"] for r in refs}
    out = {k: v for k, v in case.items() if k != "incident_id"}
    out["refs"] = refs
    out["timeline"] = [t | {"ref": t.get("ref") if t.get("ref") in shown else None} for t in case["timeline"]]  # fmt: skip
    out["incident"] = pub and {
        "id": pub["id"],
        "label": pub["referral"],
        "swaps": inc["swaps"],
        "refunded": inc["refunded"],
        "failed": inc["failed"],
        "ongoing": inc["ended_at"] is None,
    }
    return out | {"message": message(out)}


def build(con, data: dict, ref: str) -> dict:
    """Turn a 1Click status reply into a public case. No address leaves this function."""
    from .web import chain_of

    req = (data.get("quoteResponse") or {}).get("quoteRequest") or {}
    q = (data.get("quoteResponse") or {}).get("quote") or {}
    d = data.get("swapDetails") or {}
    status = data.get("status", "UNKNOWN")
    reason = d.get("refundReason")
    refs = _refs(con, data)
    created = (data.get("quoteResponse") or {}).get("timestamp")
    cause, todo = REASONS.get(reason, (None, None))
    if reason and not cause:
        cause = f"1Click gave the reason code {reason}. We do not know it yet."
    if status == "FAILED" and not refs:
        cause = (
            cause + " " if cause else ""
        ) + "No transactions exist, so no funds moved."
    near = [r for r in refs if r["chain"] == "near" and r["kind"] == "intent"]
    payout = [r for r in refs if r["kind"] in ("refund", "delivery")]
    final = status in FINAL
    deposit_done = status in (
        "KNOWN_DEPOSIT_TX", "PROCESSING", "SUCCESS", "REFUNDED", "INCOMPLETE_DEPOSIT"
    )  # fmt: skip
    timeline = [
        {"step": "Quote made", "at": created, "done": bool(created)},
        {"step": "Deposit received", "at": None, "done": deposit_done or any(r["kind"] == "deposit" for r in refs),
         "ref": next((r["hash"] for r in refs if r["kind"] == "deposit"), None)},
        {"step": "Settled on NEAR", "at": near[0].get("at") if near else None, "done": bool(near),
         "ref": near[0]["hash"] if near else None},
        {"step": FINAL.get(status, "Delivered or refunded"), "at": data.get("updatedAt") if final else None,
         "done": final, "ref": payout[0]["hash"] if payout else None},
    ]  # fmt: skip
    referral = req.get("referral") or "(none)"
    answer = ANSWER.get(status, f"1Click status: {status}.")
    if status == "SUCCESS" and d.get("amountOutFormatted"):
        answer = f"Delivered: {d['amountOutFormatted']} on {chain_of(req.get('destinationAsset', ''))}."
    if status == "REFUNDED" and d.get("refundedAmountFormatted"):
        answer = f"Your funds came back: {d['refundedAmountFormatted']} to your refund address."
    return {
        "ref": ref,
        "status": status,
        "answer": answer,
        "reason": reason,
        "cause": cause,
        "next": todo or NEXT.get(status, "Copy the support message and send it to support."),
        "route": f"{chain_of(req.get('originAsset', ''))} → {chain_of(req.get('destinationAsset', ''))}",
        "deposit_type": req.get("depositType"),
        "amount_in": q.get("amountInFormatted") or d.get("amountInFormatted"),
        "usd_in": q.get("amountInUsd") or d.get("amountInUsd"),
        "amount_out": d.get("amountOutFormatted"),
        "refunded": d.get("refundedAmountFormatted"),
        "created_at": created,
        "updated_at": data.get("updatedAt"),
        "incident_id": incident_for(con, referral, created),  # internal: see view()
        "timeline": timeline,
        "refs": refs,
    }  # fmt: skip


def save(con, case: dict) -> bool:
    """Upsert the case. True when it was not looked at in the last hour: count it then,
    so page polls and repeat visits do not inflate the lookup counts."""
    (recent,) = con.execute(
        "SELECT count(*) FROM app.support_case"
        " WHERE ref = ? AND updated_at > now() - INTERVAL 1 HOUR",
        [case["ref"]],
    ).fetchone()
    con.execute(
        "INSERT INTO app.support_case (ref, updated_at, case_json) VALUES (?, now(), ?)"
        " ON CONFLICT (ref) DO UPDATE SET updated_at = now(), case_json = excluded.case_json",
        [case["ref"], json.dumps(case)],
    )
    return not recent


def saved(con, ref: str) -> dict | None:
    row = con.execute(
        "SELECT case_json FROM app.support_case WHERE ref = ?", [ref]
    ).fetchone()
    return json.loads(row[0]) if row else None


def log_lookup(con, case: dict) -> None:
    """Count one lookup per hour by status, reason, route and incident. No address."""
    con.execute(
        "INSERT INTO ops.support_lookup VALUES (date_trunc('hour', now()), ?, ?, ?, ?, 1)"
        " ON CONFLICT DO UPDATE SET lookups = lookups + 1",
        [
            case["status"],
            case["reason"] or "",
            case["route"],
            case.get("incident_id") is not None,
        ],
    )


def asks(con, hours: int = 24) -> list[dict]:
    """What people looked up in the last `hours`: lookups by status and reason."""
    cur = con.execute(
        "SELECT status, nullif(reason, '') AS reason, sum(lookups)::INT AS lookups,"
        " sum(lookups) FILTER (WHERE incident)::INT AS in_incident"
        " FROM ops.support_lookup WHERE hour > now() - to_hours(?::INT)"
        " GROUP BY ALL ORDER BY lookups DESC LIMIT 10",
        [hours],
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def spikes(con) -> list[tuple[str, int]]:
    """Reasons with at least SPIKE lookups in the current hour."""
    return con.execute(
        "SELECT reason, sum(lookups)::INT FROM ops.support_lookup"
        " WHERE hour = date_trunc('hour', now()) AND reason != ''"
        " GROUP BY reason HAVING sum(lookups) >= ? ORDER BY 2 DESC",
        [SPIKE],
    ).fetchall()


def _usd(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return f"{value} USD"


def message(case: dict) -> str:
    """Plain text to paste into a NEAR Intents or wallet support chat."""
    host = os.environ.get("PUBLIC_HOST", "localhost:8420")
    lines = [
        "NEAR Intents swap: support case",
        f"Case: https://{host}/#case={case['ref']}",
        f"Status: {case['status']}"
        + (f" ({case['reason']})" if case["reason"] else ""),
        f"What happened: {case['answer']}"
        + (f" {case['cause']}" if case["cause"] else ""),
        f"Route: {case['route']} · sent {case['amount_in'] or '–'}"
        + (f" ({_usd(case['usd_in'])})" if case["usd_in"] else "")
        + (
            f" · refunded {case['refunded']}"
            if case["refunded"] not in (None, "0")
            else ""
        )
        + (
            f" · received {case['amount_out']}"
            if case["amount_out"] not in (None, "0")
            else ""
        ),
        f"Quote time (UTC): {case['created_at'] or '–'}",
    ]
    inc = case["incident"]
    if inc:
        lines.append(
            f"Incident: {inc['label']} ({inc['refunded']:,} refunded, {inc['failed']:,} failed)"
            f" https://{host}/#incident={quote(inc['id'])}"
        )
    lines.append("Transactions:")
    for r in case["refs"] or [{"kind": "none", "chain": "", "hash": "no transactions"}]:
        block = f" block {r['block']}" if r.get("block") else ""
        lines.append(
            f"- {r['chain']} {r['kind']} {r['hash']}{block} {r.get('url') or ''}".rstrip()
        )
    lines.append(
        "Prepared by Intents Desk (DataBlock). No recipient or refund address is included."
    )
    return "\n".join(lines)
