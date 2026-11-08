"""Wallet activity for one NEAR account in plain words, from public chain data.

One transaction becomes one line: "Wrapped 1 NEAR into wNEAR", "Sent 1.02 NEAR to a
deposit address", "Swapped 5 USDC on Rhea". A deposit address (a 64-hex implicit account)
may be a 1Click swap; the web layer checks its status.
"""

import base64
import json
import re
from datetime import UTC, datetime

IMPLICIT = re.compile(r"[0-9a-f]{64}")
INTENTS = "intents.near"
RHEA = "v2.ref-finance.near"
# contract -> (decimals, symbol). Others show raw units with the contract name.
TOKENS = {
    "wrap.near": (24, "wNEAR"),
    "17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1": (6, "USDC"),
    "usdt.tether-token.near": (6, "USDT"),
}


def near(yocto: int | str) -> str:
    return f"{int(yocto) / 10**24:.4g} NEAR"


def token(contract: str, raw: int | str) -> str:
    decimals, symbol = TOKENS.get(contract, (0, contract))
    return f"{int(raw) / 10**decimals:.6g} {symbol}"


def short(account: str) -> str:
    if account in TOKENS:
        return TOKENS[account][1]
    return account[:8] + "…" if IMPLICIT.fullmatch(account) else account


def _calls(actions: list) -> list[tuple[str, dict, int]]:
    """(method, args, deposit) for each FunctionCall; args {} when not JSON."""
    out = []
    for a in actions:
        call = a.get("FunctionCall") if isinstance(a, dict) else None
        if not call:
            continue
        try:
            args = json.loads(base64.b64decode(call.get("args") or "e30="))
        except (ValueError, TypeError):
            args = {}
        out.append((call.get("method_name", ""), args if isinstance(args, dict) else {},
                    int(call.get("deposit") or 0)))  # fmt: skip
    return out


def _transfer(actions: list) -> int | None:
    for a in actions:
        if isinstance(a, dict) and "Transfer" in a:
            return int(a["Transfer"]["deposit"])
    return None


def describe(item: dict, account: str) -> dict:
    """One line for one transaction. `deposit` is set when funds went to a deposit address."""
    tx = item["transaction"]
    signer, receiver, actions = (
        tx["signer_id"],
        tx["receiver_id"],
        tx.get("actions", []),
    )
    ns = int(item["execution_outcome"]["block_timestamp"])
    line = {
        "tx": tx["hash"],
        "when": datetime.fromtimestamp(ns / 1e9, UTC).isoformat(timespec="seconds"),
        "ok": bool(item.get("is_success", True)),
        "kind": "other",
        "text": "",
        "deposit": None,
    }
    relayed = ""
    inner = (actions[0].get("Delegate") or {}).get("delegate_action") if len(actions) == 1 and isinstance(actions[0], dict) else None  # fmt: skip
    if (
        inner and inner.get("sender_id") == account
    ):  # a meta transaction: our action, their gas
        signer, receiver, actions = (
            account,
            inner["receiver_id"],
            inner.get("actions", []),
        )
        relayed = " (gas paid by a relayer)"
    calls, sent = _calls(actions), _transfer(actions)
    methods = {m: (args, dep) for m, args, dep in calls}
    if receiver == INTENTS and "execute_intents" in methods:
        mine = _intents_for(methods["execute_intents"][0], account)
        line.update(kind="intents", text="NEAR Intents: " + ("; ".join(mine) or "a settlement that touched this account"))  # fmt: skip
        return line

    if signer != account:  # someone else's transaction that touched this account
        if sent is not None and receiver == account:
            line.update(
                kind="received", text=f"Received {near(sent)} from {short(signer)}"
            )
        elif "ft_transfer" in methods and methods["ft_transfer"][0].get("receiver_id") == account:  # fmt: skip
            args = methods["ft_transfer"][0]
            line.update(kind="received", text=f"Received {token(receiver, args.get('amount', 0))} from {short(signer)}")  # fmt: skip
        else:
            line["text"] = f"{short(signer)} called {short(receiver)}"
        return line

    if receiver == "wrap.near" and "near_deposit" in methods:
        line.update(kind="wrap", text=f"Wrapped {near(methods['near_deposit'][1])} into wNEAR (1:1)")  # fmt: skip
    elif receiver == "wrap.near" and "near_withdraw" in methods:
        line.update(kind="wrap", text=f"Unwrapped {near(methods['near_withdraw'][0].get('amount', 0))} wNEAR into NEAR")  # fmt: skip
    elif sent is not None and IMPLICIT.fullmatch(receiver):
        line.update(kind="deposit", deposit=receiver, text=f"Sent {near(sent)} to deposit address {short(receiver)}")  # fmt: skip
    elif "ft_transfer" in methods and IMPLICIT.fullmatch(methods["ft_transfer"][0].get("receiver_id", "")):  # fmt: skip
        args = methods["ft_transfer"][0]
        line.update(kind="deposit", deposit=args["receiver_id"],
                    text=f"Sent {token(receiver, args.get('amount', 0))} to deposit address {short(args['receiver_id'])}")  # fmt: skip
    elif "ft_transfer_call" in methods:
        args = methods["ft_transfer_call"][0]
        amount, to = token(receiver, args.get("amount", 0)), args.get("receiver_id", "")
        if to == INTENTS:
            line.update(kind="intents", text=f"Deposited {amount} into NEAR Intents")
        elif to == RHEA:
            line.update(kind="dex", text=f"Swapped {amount} on Rhea")
        else:
            line.update(kind="transfer", text=f"Sent {amount} to {short(to)}")
    elif sent is not None:
        line.update(kind="transfer", text=f"Sent {near(sent)} to {short(receiver)}")
    elif methods and set(methods) == {"storage_deposit"}:
        line.update(kind="setup", text=f"Registered with {short(receiver)} (one-time storage fee)")  # fmt: skip
    elif any(k in a for a in actions if isinstance(a, dict) for k in ("AddKey", "DeleteKey")):  # fmt: skip
        line.update(kind="keys", text="Changed the account's access keys")
    elif "deposit_and_stake" in methods:
        line.update(kind="stake", text=f"Staked {near(methods['deposit_and_stake'][1])} with {short(receiver)}")  # fmt: skip
    elif "unstake" in methods or "unstake_all" in methods:
        line.update(kind="stake", text=f"Unstaked from {short(receiver)}")
    else:
        first = calls[0][0] if calls else "an action"
        line["text"] = f"Called {first} on {short(receiver)}"
    line["text"] += relayed
    return line


def _intents_for(args: dict, account: str) -> list[str]:
    """The intents in an execute_intents call that name this account, in words."""
    out = []
    for signed in args.get("signed", []):
        try:
            msg = json.loads((signed.get("payload") or {}).get("message", "{}"))
        except (ValueError, TypeError, AttributeError):
            continue
        for it in msg.get("intents", []):
            kind = it.get("intent", "")
            if it.get("receiver_id") == account and kind == "native_withdraw":
                out.append(f"sent {near(it.get('amount', 0))} to this account")
            elif it.get("receiver_id") == account and kind == "ft_withdraw":
                out.append(f"sent {token(it.get('token', ''), it.get('amount', 0))} to this account")  # fmt: skip
            elif msg.get("signer_id") == account:
                out.append(f"{kind.replace('_', ' ')} signed by this account")
    return out
