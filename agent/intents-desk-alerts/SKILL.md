---
name: intents-desk-alerts
version: 0.1.0
description: Create, list and stop Intents Desk alerts for the owner (Telegram, the public channel, X drafts) from a plain request. Start a message with $alerts to use it.
activation:
  keywords:
    - alerts
    - alert me
    - tell me when
    - notify me
    - stop alert
  patterns:
    - "(alert|tell|notify|ping) me (when|if)"
    - "(list|show|stop|turn off|remove).*alerts?"
  tags:
    - near
    - intents
    - alerts
  max_context_tokens: 1500
requires:
  bins:
    - curl
  env:
    - INTENTS_DESK_URL
    - INTENTS_DESK_TOKEN
---

# Intents Desk alerts

The owner tells you what to watch. You turn it into ONE command and send it to Intents Desk.
Intents Desk checks it every 10 minutes and sends one message when it starts and one when it ends.

## The command (exactly one of these)

| The owner wants | Command |
|---|---|
| refunds on one route over N% | `route <from> <to> <N>`: chains are short names: tron, eth, sol, near, base, arb, btc, bsc… |
| all swaps over N% bad | `network <N>` |
| N+ refunds an hour with one reason | `reason <REASON> <N>`: e.g. QUOTE_EXPIRED, NO_LIQUIDITY, INTENT_SUBMIT_FAILED |
| incidents of one partner | `partner <id or alias>` |
| one refund of $N or more | `refund <N>` |
| one swap's status | `swap <deposit address>` |
| see the alerts | `list` |
| stop alert number K | `off <K>` |

Add ` +channel` to also post to the public Telegram channel, and ` +x` to also get an X draft
(the owner presses Post in Telegram; nothing is posted without that press). Telegram is always on.

## Steps

1. If the request is unclear (no route, no number, which channel), ask ONE short question. Do not guess numbers.
2. Build the command. Check it matches `^[A-Za-z0-9 _.:%+$-]{2,120}$`. If not, say you cannot build it.
3. Run this one command, with the command text in place of COMMAND (keep the JSON quotes):
   `curl -s --max-time 20 -X POST "$INTENTS_DESK_URL/api/agent/alerts" -H "Authorization: Bearer $INTENTS_DESK_TOKEN" -H "Content-Type: application/json" -d '{"text": "COMMAND"}'`
4. Reply with the `reply` field, in at most 4 lines. If there is an `error` field, show it and suggest the fix.

## Examples

- "Tell me when tron to eth refunds pass 20%, and draft a tweet" → `route tron eth 20 +x`
- "Ping the channel if the whole network goes over 10% bad" → `network 10 +channel`
- "What alerts do I have?" → `list`
- "Stop alert 3" → `off 3`

## Rules

- Never print, repeat or change `$INTENTS_DESK_TOKEN`. Never put it in a reply.
- Never send, move or sign funds. Alerts only read public data.
- One command per request. Never chain commands with `;`, `&&` or `|`.
