"""Custom alert rules: "tell me when …", checked every 10 minutes.

Rules come from Telegram (/alert …, any linked user) and from the owner's IronClaw
agent (the same text through POST /api/agent/alerts). A rule fires once when its
condition turns true and sends one "Resolved" message when it turns false.

Every alert has the same four lines, built from data only (no AI text):
what happened, how big, who should act, where to look and how to stop it.
"""

import json
import os
import re

from . import support

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS app.alert_rule_id;
CREATE TABLE IF NOT EXISTS app.alert_rule (
    id BIGINT PRIMARY KEY DEFAULT nextval('app.alert_rule_id'),
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL,          -- route | partner | network | reason | refund
    params TEXT NOT NULL,        -- JSON
    targets TEXT NOT NULL,       -- comma list: telegram, channel, x
    active BOOLEAN DEFAULT true,
    state BOOLEAN DEFAULT false, -- the condition at the last check
    last_fired_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""
MAX_RULES = 10
MIN_SWAPS = 20  # a route or network hour with fewer swaps never fires
CHAIN = re.compile(r"^[a-z0-9-]{2,20}$")
PARTNER = re.compile(r"^[A-Za-z0-9._:-]{2,80}$")
REASON = re.compile(r"^[A-Z_]{3,40}$")
ALIAS = re.compile(r"partner-[0-9a-f]{6}")
USAGE = (
    "Alerts, checked every 10 minutes:\n"
    "/alert route <from> <to> <percent>: refunds on a route, e.g. /alert route tron eth 20\n"
    "/alert network <percent>: all swaps\n"
    "/alert reason <REASON> <count>: refunds with one reason per hour\n"
    "/alert partner <id or alias>: incidents of one partner\n"
    "/alert swap <deposit address>: one swap's status\n"
    "/alert list · /alert off <number>"
)
# Who can act on a refund reason (the user line comes from support.REASONS).
TEAM = {
    "INTENT_SUBMIT_FAILED": "1Click and the wallet or widget that sent the swaps",
    "QUOTE_EXPIRED": "wallet teams: check quote deadlines and deposit speed",
    "NO_LIQUIDITY": "solvers: the route has no liquidity at this size",
    "AMOUNT_MORE_THAN_BALANCE": "wallet teams: check the amount the deposit sends",
}


class RuleError(ValueError):
    """A rule the parser refused. The message says how to fix it."""


def ensure(con) -> None:
    con.execute(SCHEMA)


def parse(text: str, owner: bool = False) -> dict:
    """Parse an /alert command (without "/alert"). Returns {"op": ...}.

    ops: list, off (id), swap (address), add (kind, params, targets).
    Only the owner may add the +channel and +x targets, and the refund kind.
    """
    words = text.split()
    targets = ["telegram"]
    while words and words[-1] in ("+channel", "+x"):
        if not owner:
            raise RuleError("Only the owner can send alerts to the channel or X.")
        targets.append(words.pop()[1:])
    if not words or words[0] in ("help", "?"):
        raise RuleError(USAGE)
    kind, args = words[0].lower(), words[1:]

    def number(s: str, lo: float, hi: float) -> float:
        try:
            v = float(s.rstrip("%").lstrip("$").replace(",", ""))
        except ValueError:
            raise RuleError(f"{s!r} is not a number.") from None
        if not lo <= v <= hi:
            raise RuleError(f"Use a number from {lo:g} to {hi:g}.")
        return v

    if kind == "list" and not args:
        return {"op": "list"}
    if kind == "off" and len(args) == 1 and args[0].isdigit():
        return {"op": "off", "id": int(args[0])}
    if kind == "swap" and len(args) == 1:
        return {"op": "swap", "address": args[0]}
    if kind == "route" and len(args) == 3:
        src, dst = args[0].lower(), args[1].lower()
        if not (CHAIN.match(src) and CHAIN.match(dst)):
            raise RuleError("Chains are short names, e.g. tron, eth, sol, near, base.")
        params = {"from": src, "to": dst, "pct": number(args[2], 1, 100)}
    elif kind == "network" and len(args) == 1:
        params = {"pct": number(args[0], 1, 100)}
    elif kind == "reason" and len(args) == 2 and REASON.match(args[0].upper()):
        params = {"reason": args[0].upper(), "count": int(number(args[1], 1, 100_000))}
    elif kind == "partner" and len(args) == 1 and PARTNER.match(args[0]):
        # Others may use only the public alias: matching a guessed raw name would tell
        # them which wallet is behind an alias (partner names stay private, AGENTS.md).
        if not owner and not ALIAS.fullmatch(args[0]):
            raise RuleError(
                "Use the partner alias shown on the page, e.g. partner-ee7bd4."
            )
        params = {"partner": args[0]}
    elif kind == "refund" and len(args) == 1:
        if not owner:
            raise RuleError("Large-refund alerts are for the owner only.")
        params = {"usd": number(args[0], 100, 10_000_000)}
    else:
        raise RuleError(USAGE)
    return {"op": "add", "kind": kind, "params": params, "targets": targets}


def add(con, account: str, rule: dict) -> int:
    (n,) = con.execute(
        "SELECT count(*) FROM app.alert_rule WHERE account_id = ? AND active", [account]
    ).fetchone()
    if n >= MAX_RULES:
        raise RuleError(f"You have {MAX_RULES} alerts. Turn one off first: /alert list")
    (rule_id,) = con.execute(
        "INSERT INTO app.alert_rule (account_id, kind, params, targets) VALUES (?, ?, ?, ?)"
        " RETURNING id",
        [account, rule["kind"], json.dumps(rule["params"]), ",".join(rule["targets"])],
    ).fetchone()
    return rule_id


def off(con, account: str, rule_id: int) -> bool:
    row = con.execute(
        "UPDATE app.alert_rule SET active = false WHERE id = ? AND account_id = ? AND active"
        " RETURNING id",
        [rule_id, account],
    ).fetchone()
    return row is not None


def describe(kind: str, p: dict) -> str:
    """The rule in words, e.g. "route tron → eth over 20% refunded or failed"."""
    return {
        "route": lambda: (
            f"route {p.get('from')} → {p.get('to')} over {p.get('pct', 0):g}% refunded or failed"
        ),
        "network": lambda: f"all swaps over {p.get('pct', 0):g}% refunded or failed",
        "reason": lambda: f"{p.get('count')}+ refunds an hour with {p.get('reason')}",
        "partner": lambda: f"incidents for {p.get('partner')}",
        "refund": lambda: f"one refund of ${p.get('usd', 0):,.0f} or more",
    }[kind]()


def listing(con, account: str) -> list[dict]:
    rows = con.execute(
        "SELECT id, kind, params, targets, state FROM app.alert_rule"
        " WHERE account_id = ? AND active ORDER BY id",
        [account],
    ).fetchall()
    return [
        {
            "id": i,
            "text": describe(k, json.loads(p)),
            "targets": t.split(","),
            "firing": s,
        }
        for i, k, p, t, s in rows
    ]


# ---------- Checking ----------


def _hour(con) -> tuple[float, str] | None:
    """The newest full hour: (epoch seconds for queries, "YYYY-MM-DD HH:MM" UTC to show)."""
    row = con.execute(
        "SELECT epoch(max(hour)), strftime(timezone('UTC', max(hour)), '%Y-%m-%d %H:%M')"
        " FROM ops.hourly_swap_summary"
    ).fetchone()
    return None if row[0] is None else (row[0], row[1])


def _bad(con, hour, where: str, args: list) -> dict:
    """Swaps, bad swaps, refunded USD and the top reason in one hour for a filter."""
    swaps, bad, usd = con.execute(
        "SELECT coalesce(sum(swaps), 0),"
        " coalesce(sum(swaps) FILTER (WHERE status IN ('REFUNDED', 'FAILED')), 0),"
        " coalesce(sum(refunded_usd), 0)"
        f" FROM ops.hourly_swap_summary WHERE epoch(hour) = ? {where}",
        [hour[0], *args],
    ).fetchone()
    reason = con.execute(
        "SELECT top_refund_reason FROM ops.hourly_swap_summary"
        f" WHERE epoch(hour) = ? AND status = 'REFUNDED' {where}"
        " AND top_refund_reason IS NOT NULL ORDER BY swaps DESC LIMIT 1",
        [hour[0], *args],
    ).fetchone()
    pct = round(100 * bad / swaps, 1) if swaps else 0.0
    return {"swaps": int(swaps), "bad": int(bad), "pct": pct, "usd": float(usd),
            "reason": reason[0] if reason else None}  # fmt: skip


def facts(con, kind: str, p: dict) -> tuple[bool, dict]:
    """(condition now, the numbers behind it) for one rule."""
    hour = _hour(con)
    if kind in ("route", "network", "reason") and hour is None:
        return False, {}
    if kind == "route":
        f = _bad(con, hour, "AND origin_chain = ? AND destination_chain = ?", [p["from"], p["to"]])  # fmt: skip
        return f["swaps"] >= MIN_SWAPS and f["pct"] > p["pct"], f | {"hour": hour[1]}
    if kind == "network":
        f = _bad(con, hour, "", [])
        return f["swaps"] >= MIN_SWAPS and f["pct"] > p["pct"], f | {"hour": hour[1]}
    if kind == "reason":
        (n,) = con.execute(
            "SELECT coalesce(sum(swaps), 0) FROM ops.hourly_swap_summary"
            " WHERE epoch(hour) = ? AND status = 'REFUNDED' AND top_refund_reason = ?",
            [hour[0], p["reason"]],
        ).fetchone()
        return int(n) >= p["count"], {"count": int(n), "reason": p["reason"], "hour": hour[1]}  # fmt: skip
    if kind == "partner":
        from . import incidents
        from .web import mask_partner

        want = p["partner"]
        for inc in incidents.listing(con, 50):
            if inc["ended_at"] is None and want in (inc["referral"], mask_partner(inc["referral"], force=True)):  # fmt: skip
                return True, {"pct": inc["bad_pct"], "swaps": inc["swaps"], "reason": inc["top_reason"],
                              "started": inc["started_at"], "referral": inc["referral"]}  # fmt: skip
        return False, {}
    if kind == "refund":
        row = con.execute(
            "SELECT amount_in_usd, refund_reason, origin_asset, destination_asset"
            " FROM silver.swap WHERE status = 'REFUNDED' AND amount_in_usd >= ?"
            " AND created_at > now() - INTERVAL 1 HOUR ORDER BY amount_in_usd DESC LIMIT 1",
            [p["usd"]],
        ).fetchone()
        if not row:
            return False, {}
        from .web import chain_of

        return True, {"usd": row[0], "reason": row[1], "route": f"{chain_of(row[2])} → {chain_of(row[3])}"}  # fmt: skip
    raise KeyError(kind)


def check(con) -> list[dict]:
    """Evaluate every active rule. Return the transitions (fired or resolved)."""
    owner = os.environ.get("OWNER_ACCOUNT", "datablock.near")
    rows = con.execute(  # only rules someone can receive: a linked chat, or the owner
        "SELECT r.id, r.account_id, r.kind, r.params, r.targets, r.state"
        " FROM app.alert_rule r LEFT JOIN app.telegram t USING (account_id)"
        " WHERE r.active AND (t.chat_id IS NOT NULL OR r.account_id = ?)",
        [owner],
    ).fetchall()
    out, seen = [], {}
    for rule_id, account, kind, params, targets, state in rows:
        p = json.loads(params)
        key = (kind, json.dumps(p, sort_keys=True))
        if key not in seen:  # the same rule from many accounts: one evaluation
            seen[key] = facts(con, kind, p)
        now, f = seen[key]
        if now == state:
            continue
        con.execute(
            "UPDATE app.alert_rule SET state = ?, last_fired_at = CASE WHEN ? THEN now()"
            " ELSE last_fired_at END WHERE id = ?",
            [now, now, rule_id],
        )
        out.append({"id": rule_id, "account": account, "kind": kind, "params": p,
                    "targets": targets.split(","), "resolved": not now, "facts": f})  # fmt: skip
    return out


# ---------- Wording ----------


def _host() -> str:
    return os.environ.get("PUBLIC_HOST", "localhost:8420")


def render(t: dict, public: bool = False) -> str:
    """The four lines. public=True: partner alias only, and no stop line."""
    from .web import mask_partner

    kind, p, f = t["kind"], t["params"], t["facts"]
    rule = describe(kind, p)
    if kind == "partner" and public:
        rule = f"incidents for {mask_partner(f.get('referral') or p['partner'], force=True)}"  # fmt: skip
    if t["resolved"]:
        lines = [f"RESOLVED · {rule}: back under the threshold at the last check."]
    elif kind in ("route", "network"):
        where = f"Route {p['from']} → {p['to']}" if kind == "route" else "All swaps"
        lines = [
            f"ALERT · {where}: {f['pct']:g}% refunded or failed in the hour from {f['hour'][:16]} UTC (your rule: over {p['pct']:g}%)",
            f"Size: {f['bad']:,} of {f['swaps']:,} swaps, ${f['usd']:,.0f} refunded. Top reason: {f['reason'] or '–'}.",
        ]
    elif kind == "reason":
        lines = [
            f"ALERT · {f['count']:,} refunds with {f['reason']} in the hour from {f['hour'][:16]} UTC (your rule: {p['count']}+)",
            f"What it means: {support.REASONS.get(f['reason'], ('A refund reason.',))[0]}",
        ]
    elif kind == "partner":
        who = mask_partner(f["referral"], force=True) if public else p["partner"]
        lines = [
            f"ALERT · Incident open for {who}: {f['pct']}% of its swaps refunded or failed since {str(f['started'])[:16]} UTC",
            f"Size: {f['swaps']:,} swaps so far. Top reason: {f['reason'] or '–'}.",
        ]
    else:  # refund
        lines = [
            f"ALERT · A ${f['usd']:,.0f} swap was refunded in the last hour (your rule: ${p['usd']:,.0f}+)",
            f"Route {f['route']}. Reason: {f['reason'] or '–'}.",
        ]
    if not t["resolved"]:
        reason = f.get("reason")
        user = support.REASONS.get(reason, (None, "Check the swap on Intents Desk."))[1]
        team = TEAM.get(reason, "1Click and the wallet that sent the swaps")
        lines.append(f"Act: users: {user} Teams: {team}.")
    tail = f"Open: https://{_host()}/"
    if not public:
        tail += f" · Stop this alert: /alert off {t['id']}"
    return "\n".join([*lines, tail])


def x_text(t: dict) -> str:
    """At most 280 characters, no link: the first two lines of the public render."""
    text = "\n".join(render(t, public=True).split("\n")[:2])
    return text if len(text) <= 280 else text[:277] + "…"


def run(con, account: str, cmd: dict) -> str:
    """Apply a parsed command for `account`; return the reply text."""
    if cmd["op"] == "list":
        rules = listing(con, account)
        if not rules:
            return "No alerts yet.\n" + USAGE
        return "\n".join(
            f"{r['id']} · {r['text']} → {', '.join(r['targets'])}"
            + (" (firing)" if r["firing"] else "")
            for r in rules
        )
    if cmd["op"] == "off":
        return f"Alert {cmd['id']} is off." if off(con, account, cmd["id"]) else f"No alert {cmd['id']} on this account."  # fmt: skip
    if cmd["op"] == "swap":
        from . import store
        from .web import ADDRESS

        if not ADDRESS.match(cmd["address"]):
            return "That is not a deposit address."
        store.set_watch(con, account, cmd["address"], True)
        return "Watching that swap. You get a message when its status changes."
    rule_id = add(con, account, cmd)
    return (
        f"Alert {rule_id} on: {describe(cmd['kind'], cmd['params'])}.\n"
        f"Delivered to: {', '.join(cmd['targets'])}. Checked every 10 minutes; one message when it"
        f" starts, one when it ends.\nStop it: /alert off {rule_id}"
    )
