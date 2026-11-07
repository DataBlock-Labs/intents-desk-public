"""Incident log: find partners (and the whole network) with a burst of failing swaps.

An incident opens when an hour has at least MIN_SWAPS swaps and at least BAD_PCT percent
REFUNDED or FAILED (network: more than NETWORK_PCT percent of all swaps). Bad hours with
at most 2 clean hours between them are one incident. It closes after 2 clean hours.

Detection reads ops.hourly_swap_summary (kept forever) plus the raw hours not rolled up
yet, so it also works after the 24 h purge. ops.incident keeps only totals and public
NEAR transaction hashes: no sender, recipient or full deposit addresses (1Click terms).
"""

import json
import os
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from . import report, sources, telegram
from .ops import CHAIN_MACRO

MIN_SWAPS, BAD_PCT, NETWORK_PCT, CLEAN_HOURS = 50, 50.0, 10.0, 2
NETWORK = "(all)"
# Markdown control characters, plus : and . so URLs don't autolink (md_table does <>|)
MD = re.compile(r"([\\`*_\[\]()!#~:.])")
NEAR_TX = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{43,44}$")  # base58 hash: safe in a link
HOUR = timedelta(hours=1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ops.incident (
    id TEXT PRIMARY KEY,           -- <referral>@<start hour, ISO>
    scope TEXT NOT NULL,           -- partner | network
    referral TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    last_bad_hour TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,          -- NULL while open
    swaps BIGINT, refunded BIGINT, failed BIGINT, bad_pct DOUBLE, others_pct DOUBLE,
    usd_in DOUBLE, senders BIGINT, top_reason TEXT,
    routes JSON, hours JSON, partners JSON, evidence JSON,
    drafted_open BOOLEAN DEFAULT false, drafted_close BOOLEAN DEFAULT false,
    updated_at TIMESTAMPTZ DEFAULT now()
);
"""

# One row per hour, partner, route and status: the rollup, plus raw hours newer than it.
ROWS = """
WITH done AS (SELECT coalesce(max(hour), TIMESTAMPTZ '1970-01-01') AS h
              FROM ops.hourly_swap_summary)
SELECT epoch(hour)::BIGINT, partner, origin_chain || ' → ' || destination_chain, status, swaps, usd_in,
    top_refund_reason
FROM ops.hourly_swap_summary
UNION ALL
SELECT epoch(date_trunc('hour', created_at))::BIGINT, coalesce(nullif(referral, ''), '(none)'),
    ops_chain(origin_asset) || ' → ' || ops_chain(destination_asset), status, count(*),
    sum(amount_in_usd), mode(refund_reason)
FROM silver.swap, done WHERE date_trunc('hour', created_at) > done.h
GROUP BY ALL
"""


def ensure(con) -> None:
    con.execute(SCHEMA)


def _hours(con) -> tuple[dict, datetime | None]:
    """Get {(partner, hour): Counter} of swaps by status, route and reason, and the
    newest hour with data. The network is partner NETWORK."""
    con.execute(CHAIN_MACRO)
    stats: dict = defaultdict(Counter)
    newest = None
    for hour, partner, route, status, n, usd, reason in con.execute(ROWS).fetchall():
        hour = datetime.fromtimestamp(hour, UTC)
        newest = max(newest or hour, hour)
        bad = status in ("REFUNDED", "FAILED")
        for key in ((partner, hour), (NETWORK, hour)):
            c = stats[key]
            c["swaps"] += n
            c["usd"] += usd or 0
            c[status] += n
            c["route:" + route] += n
            if bad:
                c["bad"] += n
                c["badroute:" + route] += n
                c["reason:" + (reason or status)] += n
            if key[0] == NETWORK and bad:
                c["partner:" + partner] += n
    return stats, newest


def _is_bad(partner: str, c: Counter) -> bool:
    if c["swaps"] < MIN_SWAPS:
        return False
    pct = 100 * c["bad"] / c["swaps"]
    return pct > NETWORK_PCT if partner == NETWORK else pct >= BAD_PCT


def find(con) -> list[dict]:
    """Compute every incident from the hourly data. Same input, same output."""
    stats, newest = _hours(con)
    if newest is None:
        return []
    by_partner, bad_in_hour = defaultdict(list), defaultdict(set)
    for (partner, hour), c in stats.items():
        if _is_bad(partner, c):
            by_partner[partner].append(hour)
            if partner != NETWORK:
                bad_in_hour[hour].add(partner)
    found = []
    for partner, bad_hours in by_partner.items():
        bad_hours.sort()
        runs = [[bad_hours[0], bad_hours[0]]]
        for h in bad_hours[1:]:
            if h - runs[-1][1] <= (CLEAN_HOURS + 1) * HOUR:
                runs[-1][1] = h
            else:
                runs.append([h, h])
        for start, last in runs:
            found.append(_incident(stats, bad_in_hour, partner, start, last, newest))
    return sorted(found, key=lambda i: i["started_at"], reverse=True)


def _incident(stats, bad_in_hour, partner, start, last, newest) -> dict:
    total, others = Counter(), Counter()  # others: partners with no incident that hour
    hours, h = [], start
    while h <= last:
        c = stats.get((partner, h), Counter())
        total.update(c)
        others.update(stats.get((NETWORK, h), Counter()))
        for p in bad_in_hour[h] | ({partner} - {NETWORK}):
            others.subtract(stats.get((p, h), Counter()))
        hours.append([h.isoformat(), c["swaps"], _pct(c["bad"], c["swaps"])])
        h += HOUR

    def top(prefix, n):
        keys = [(k[len(prefix) :], v) for k, v in total.items() if k.startswith(prefix)]
        return sorted(keys, key=lambda kv: -kv[1])[:n]

    routes = [[r, n, _pct(total["badroute:" + r], n)] for r, n in top("route:", 8) if n]
    closed = newest >= last + CLEAN_HOURS * HOUR  # 2 later hours are complete and clean
    return {
        "id": f"{partner}@{start.strftime('%Y-%m-%dT%H:00Z')}",
        "scope": "network" if partner == NETWORK else "partner",
        "referral": partner,
        "started_at": start,
        "last_bad_hour": last,
        "ended_at": last + HOUR if closed else None,
        "swaps": total["swaps"],
        "refunded": total["REFUNDED"],
        "failed": total["FAILED"],
        "bad_pct": _pct(total["bad"], total["swaps"]),
        "others_pct": None
        if partner == NETWORK
        else _pct(others["bad"], others["swaps"]),
        "usd_in": round(total["usd"]),
        "top_reason": (top("reason:", 1) or [[None]])[0][0],
        "routes": routes,
        "hours": hours,
        "partners": [[p, n] for p, n in top("partner:", 3) if n],
    }


def _pct(part, whole) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def md(v):
    """Escape Markdown in text from swaps (referral ids, reasons) so it can't make links."""
    return MD.sub(r"\\\1", v) if isinstance(v, str) else v


def short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if address and len(address) > 12 else address


def pick_evidence(rows: list[dict]) -> list[dict]:
    """Choose a small sample of swaps that proves an incident, with public tx hashes:
    the first and last 3 of each status, and the first bad swap of every hour."""
    rows = sorted(rows, key=lambda r: r["createdAt"])
    keep, seen = {}, set()
    by_status = defaultdict(list)
    for r in rows:
        by_status[r["status"]].append(r)
        hour = r["createdAt"][:13]
        if r["status"] in ("REFUNDED", "FAILED") and hour not in seen:
            seen.add(hour)
            keep[_key(r)] = r
    for group in by_status.values():
        for r in group[:3] + group[-3:]:
            keep[_key(r)] = r
    return [
        {
            "created_at": r["createdAt"],
            "status": r["status"],
            "reason": r.get("refundReason"),
            "route": f"{_chain(r.get('originAsset'))} → {_chain(r.get('destinationAsset'))}",
            "amount_in": r.get("amountInFormatted"),
            "usd_in": r.get("amountInUsd"),
            "deposit": short(r.get("depositAddress") or ""),
            "near_tx": [
                t for t in r.get("nearTxHashes") or [] if NEAR_TX.match(str(t))
            ][:2],
        }
        for r in sorted(keep.values(), key=lambda r: r["createdAt"])
    ]


def _key(r: dict) -> str:
    return (r.get("depositAddress") or "") + (r.get("depositMemo") or "")


def _chain(asset: str | None) -> str:
    from .web import chain_of  # one chain-name rule for the page and the reports

    return chain_of(asset or "")


def _raw(con, inc: dict) -> tuple[list[dict], bool]:
    """Raw swaps of the incident still in bronze.explorer_tx, and whether they cover it."""
    if inc["scope"] == "network":  # totals only: no sample of the whole network
        return [], False
    rows = [
        json.loads(r)
        for (r,) in con.execute(
            "SELECT raw FROM bronze.explorer_tx WHERE created_at >= ? AND created_at < ?"
            " AND coalesce(raw->>'referral', '') = ?",
            [inc["started_at"], inc["last_bad_hour"] + HOUR, inc["referral"]],
        ).fetchall()
    ]
    (oldest,) = con.execute(
        "SELECT epoch(min(created_at)) FROM bronze.explorer_tx"
    ).fetchone()
    return rows, oldest is not None and oldest <= inc["started_at"].timestamp()


def save(
    con, inc: dict, rows: list[dict] | None = None, complete: bool = False
) -> None:
    """Upsert one incident. Evidence and senders come from raw rows when given; the
    draft flags and any earlier evidence are kept."""
    old = con.execute(
        "SELECT evidence, senders FROM ops.incident WHERE id = ?", [inc["id"]]
    ).fetchone()
    evidence = json.loads(old[0]) if old and old[0] else []
    senders = old[1] if old else None
    if rows and (
        complete or not evidence
    ):  # partial raw data never replaces a full sample
        evidence = pick_evidence(rows)
        if complete:
            senders = len({r.get("refundTo") for r in rows if r.get("refundTo")})
    con.execute(
        """
        INSERT INTO ops.incident (id, scope, referral, started_at, last_bad_hour, ended_at,
            swaps, refunded, failed, bad_pct, others_pct, usd_in, senders, top_reason,
            routes, hours, partners, evidence, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, now())
        ON CONFLICT (id) DO UPDATE SET last_bad_hour = excluded.last_bad_hour,
            ended_at = excluded.ended_at, swaps = excluded.swaps,
            refunded = excluded.refunded, failed = excluded.failed,
            bad_pct = excluded.bad_pct, others_pct = excluded.others_pct,
            usd_in = excluded.usd_in, senders = excluded.senders,
            top_reason = excluded.top_reason, routes = excluded.routes,
            hours = excluded.hours, partners = excluded.partners,
            evidence = excluded.evidence, updated_at = now()
        """,
        [
            inc["id"], inc["scope"], inc["referral"], inc["started_at"],
            inc["last_bad_hour"], inc["ended_at"], inc["swaps"], inc["refunded"],
            inc["failed"], inc["bad_pct"], inc["others_pct"], inc["usd_in"], senders,
            inc["top_reason"], json.dumps(inc["routes"]), json.dumps(inc["hours"]),
            json.dumps(inc["partners"]), json.dumps(evidence),
        ],
    )  # fmt: skip


def detect(con) -> list[dict]:
    """Find incidents, store them with evidence from raw data. Return what changed."""
    changed = []
    for inc in find(con):
        old = con.execute(
            "SELECT swaps, epoch(ended_at) FROM ops.incident WHERE id = ?", [inc["id"]]
        ).fetchone()
        now = (inc["swaps"], inc["ended_at"] and inc["ended_at"].timestamp())
        save(con, inc, *_raw(con, inc))
        if old is None or tuple(old) != now:
            changed.append(inc)
    return changed


def listing(con, limit: int = 20) -> list[dict]:
    iso = "strftime({0} AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%SZ') AS {0}"
    times = ", ".join(
        iso.format(k) for k in ("started_at", "last_bad_hour", "ended_at", "updated_at")
    )
    cur = con.execute(
        f"SELECT * REPLACE ({times}) FROM ops.incident ORDER BY started_at DESC LIMIT ?",
        [limit],
    )
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        d = dict(zip(cols, row, strict=True))
        for k in ("routes", "hours", "partners", "evidence"):
            d[k] = json.loads(d[k]) if d[k] else []
        out.append(d)
    return out


def get(con, incident_id: str) -> dict | None:
    return next((i for i in listing(con, 10_000) if i["id"] == incident_id), None)


def open_count(con) -> int:
    return con.execute(
        "SELECT count(*) FROM ops.incident WHERE ended_at IS NULL"
    ).fetchone()[0]


def gaps(con, since_hours: int = 7 * 24) -> list[list[str]]:
    """UTC hours with no swaps at all in the data: the sync missed them (1Click always
    has swaps). Return [first, last] hour ranges, newest first."""
    stats, newest = _hours(con)
    have = sorted(h for (p, h) in stats if p == NETWORK)
    if not have:
        return []
    h, missing = max(have[0], newest - since_hours * HOUR), []
    present = set(have)
    while h <= newest:
        if h not in present:
            if missing and missing[-1][1] == h - HOUR:
                missing[-1][1] = h
            else:
                missing.append([h, h])
        h += HOUR
    return [
        [a.strftime("%Y-%m-%dT%H:00Z"), b.strftime("%Y-%m-%dT%H:00Z")]
        for a, b in missing[::-1]
    ]


# ---------- Explorer backfill (after the 24 h purge) ----------


def fetch_rows(inc: dict, wait: float = 5.5) -> list[dict]:
    """Get every swap of a partner incident from the Explorer API (referral filter)."""
    import time

    params = {
        "numberOfTransactions": 1000,
        "referral": inc["referral"],
        "startTimestamp": inc["started_at"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTimestamp": (inc["last_bad_hour"] + HOUR).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    rows = []
    while True:
        page = sources.get_explorer_txs(**params)
        rows += page or []
        if not page or len(page) < 1000:
            return rows
        time.sleep(wait)
        params["lastDepositAddress"] = page[-1]["depositAddress"]
        if page[-1].get("depositMemo"):
            params["lastDepositMemo"] = page[-1]["depositMemo"]
        else:
            params.pop("lastDepositMemo", None)


def backfill(con, incident_id: str) -> dict:
    inc = next((i for i in find(con) if i["id"] == incident_id), None)
    if inc is None or inc["scope"] != "partner":
        raise ValueError(f"no partner incident {incident_id!r}")
    rows = fetch_rows(inc)
    save(con, inc, rows, complete=True)
    return {"rows": len(rows), "statuses": dict(Counter(r["status"] for r in rows))}


# ---------- Posts: report, vault note, X draft to the owner's Telegram ----------


def _when(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%b %d %H:%M UTC")


def label(inc: dict) -> str:
    from .web import mask_partner  # the public deploy shows aliases (MASK_PARTNERS=1)

    return (
        "the whole network"
        if inc["scope"] == "network"
        else mask_partner(inc["referral"])
    )


def public(inc: dict) -> dict:
    """The page's view of an incident: partner names masked when MASK_PARTNERS=1."""
    from .web import mask_partner

    who = label(inc)
    return {
        **inc,
        "id": f"{who}@{inc['started_at'][:13]}:00Z",
        "referral": who,
        "partners": [[mask_partner(p), n] for p, n in inc["partners"]],
        # on-chain txs can name the partner: no evidence while names are masked
        "evidence": [] if os.environ.get("MASK_PARTNERS") == "1" else inc["evidence"],
    }


def find_public(con, public_id: str) -> dict | None:
    return next(
        (public(i) for i in listing(con, 10_000) if public(i)["id"] == public_id), None
    )


def chart(inc: dict) -> str:
    hours = [f"{h[5:10]} {h[11:16]}" for h, _n, _p in inc["hours"]]
    return report.line_chart(
        hours,
        [("refunded or failed %", [p for _h, _n, p in inc["hours"]], "s1")],
        f"Incident: {label(inc)}",
        "NEAR Intents Explorer API, via Intents Desk",
    )


def markdown(inc: dict) -> str:
    end = _when(inc["ended_at"]) if inc["ended_at"] else "ongoing"
    moved = [e for e in inc["evidence"] if e["status"] == "FAILED"]
    failed_note = (
        " FAILED swaps in the sample have no transactions: no funds moved."
        if moved and not any(e["near_tx"] for e in moved)
        else ""
    )
    hours = [(f"{h[5:10]} {h[11:16]}", n, p) for h, n, p in inc["hours"]]
    ev = [
        (e["created_at"][:19].replace("T", " "), md(e["status"]), md(e["reason"] or ""),
         md(e["route"]), md(e["amount_in"]), md(e["deposit"]),
         " ".join(f"[{t[:8]}…](https://nearblocks.io/txns/{t})" for t in e["near_tx"]) or "none")
        for e in inc["evidence"]
    ]  # fmt: skip
    others = (
        f" Partners without an incident, same hours: {inc['others_pct']}%."
        if inc["others_pct"] is not None
        else ""
    )

    def esc(s) -> str:  # referral ids come from anyone who makes a swap
        return report.md_table(["x"], [(md(s),)]).splitlines()[-1][2:-2]

    return f"""# Incident: {esc(label(inc))}

- **When:** {_when(inc["started_at"])} → {end}
- **Swaps:** {inc["swaps"]:,}; refunded {inc["refunded"]:,}, failed {inc["failed"]:,} ({inc["bad_pct"]}% bad).{others}
- **Top reason:** {esc(inc["top_reason"] or "–")}. Senders: {inc["senders"] if inc["senders"] is not None else "not counted"}.
- **Money in:** ${inc["usd_in"]:,.0f} (counted once per attempt; retries repeat the same funds).
{"- **Top partners:** " + ", ".join(f"{esc(p)} ({n:,} bad)" for p, n in inc["partners"]) if inc["scope"] == "network" else ""}

![Refunded or failed share per hour](charts/{slug(inc["id"])}.svg)

## Routes

{report.md_table(["route", "swaps", "bad %"], [tuple(map(md, r)) for r in inc["routes"]])}

## Per hour (UTC)

{report.md_table(["hour", "swaps", "bad %"], hours)}

## Evidence: sample swaps with NEAR transactions

{report.md_table(["created (UTC)", "status", "reason", "route", "amount", "deposit", "NEAR tx"], ev) if ev else "No raw swaps were stored for this incident. Run `intents-desk incidents --evidence <id>`."}
{failed_note}

Check it yourself: Explorer API `GET /api/v0/transactions?referral=<id>&startTimestamp=…`.
Rule: at least {MIN_SWAPS} swaps and {BAD_PCT:.0f}% refunded or failed in an hour (network: over {NETWORK_PCT:.0f}%).
Prepared by Intents Desk (DataBlock). Not financial advice.
"""


def slug(incident_id: str) -> str:
    """A file name per incident. The hash keeps ids like a:pda and a-pda apart."""
    import hashlib

    name = "".join(c if c.isalnum() or c in "-_" else "-" for c in incident_id)
    return (
        name.strip("-")[:120]
        + "-"
        + hashlib.sha256(incident_id.encode()).hexdigest()[:8]
    )


def write_files(inc: dict) -> list[str]:
    """Write the report (INCIDENT_REPORTS_DIR, default reports/incidents) and the
    vault note (VAULT_INCIDENTS_DIR, only when set)."""
    paths, text = [], markdown(inc)
    folders = [os.environ.get("INCIDENT_REPORTS_DIR", "reports/incidents")]
    folders += (
        [os.environ["VAULT_INCIDENTS_DIR"]]
        if os.environ.get("VAULT_INCIDENTS_DIR")
        else []
    )
    for folder in map(Path, folders):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{slug(inc['id'])}.md"
        path.write_text(text)
        (folder / "charts").mkdir(exist_ok=True)
        (folder / "charts" / f"{slug(inc['id'])}.svg").write_text(chart(inc))
        paths.append(str(path))
    return paths


def draft(inc: dict) -> str:
    """X post text for the owner to post by hand. No links in the body (X rule)."""
    who = label(inc)
    others = (
        f" Other partners: {inc['others_pct']}%."
        if inc["others_pct"] is not None
        else ""
    )
    failed = ""
    if inc["failed"]:
        moved = [e for e in inc["evidence"] if e["status"] == "FAILED"]
        failed = (
            f" {inc['failed']:,} failed before any funds moved."
            if moved and not any(e["near_tx"] for e in moved)
            else f" {inc['failed']:,} failed."
        )
    if inc["ended_at"]:
        body = (
            f"Resolved: NEAR Intents swaps from {who}, {_when(inc['started_at'])} to"
            f" {_when(inc['ended_at'])}. {inc['swaps']:,} swaps, {inc['refunded']:,}"
            f" refunded ({inc['top_reason']}).{failed}{others}"
        )
    else:
        body = (
            f"Heads-up: since {_when(inc['started_at'])}, {inc['bad_pct']}% of NEAR Intents"
            f" swaps from {who} are refunded or failed ({inc['top_reason']}),"
            f" {inc['swaps']:,} swaps so far.{failed}{others}"
        )
    host = os.environ.get("PUBLIC_HOST", "localhost:8420")
    return (
        "X draft (post it yourself, or ignore):\n\n"
        + body
        + f"\n\nFirst reply: https://{host}/#incident={quote(public(inc)['id'])}"
    )


def owner_chat(con) -> int | None:
    return telegram.linked_chat(con, os.environ.get("OWNER_ACCOUNT", "datablock.near"))


def publish(con, ids: list[str] | None = None, send_drafts: bool = True) -> dict:
    """Write the report and vault note of each incident in `ids` (default: all). Send
    every open and close draft not sent yet, once. send_drafts=False marks them sent."""
    sent, files, skipped = 0, 0, ""
    chat = owner_chat(con) if send_drafts and telegram.enabled() else None
    for inc in listing(con, 10_000):
        if ids is None or inc["id"] in ids:
            files += len(write_files(inc))
        flag = "drafted_close" if inc["ended_at"] else "drafted_open"
        if inc[flag]:
            continue
        if send_drafts and chat is None:
            skipped = "owner Telegram chat not linked"
            continue
        if send_drafts:
            telegram.send(chat, draft(inc))
            sent += 1
        con.execute(
            "UPDATE ops.incident SET drafted_open = true"
            + (", drafted_close = true" if flag == "drafted_close" else "")
            + " WHERE id = ?",
            [inc["id"]],
        )
    return {"drafts": sent, "files": files, "skipped": skipped}


def run(con, send_drafts: bool = True) -> dict:
    """The job: detect, then publish. Return details for ops.run_log."""
    changed = [i["id"] for i in detect(con)]
    return {
        "rows": len(changed),
        "open": open_count(con),
        **publish(con, changed, send_drafts),
    }
