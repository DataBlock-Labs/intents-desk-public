---
name: intents-desk
version: 0.2.0
description: Check one NEAR Intents (1Click) swap by its deposit address and tell the user what happened and what to do next. Start a message with $intents-desk to use it.
activation:
  keywords:
    - intents-desk
    - where's my swap
    - wheres my swap
    - deposit address
    - stuck swap
    - swap refunded
    - 1click
    - near intents
  patterns:
    - "(where|what).*(my|this) swap"
    - "swap.*(stuck|missing|refunded|failed|pending)"
  tags:
    - near
    - intents
    - swaps
  max_context_tokens: 1200
requires:
  bins:
    - curl
---

# Intents Desk: where's my swap?

Built at NEAR HACKATHON by DataBlock. Source: https://github.com/DataBlock-Labs/intents-desk

## Steps

1. Read the deposit address from the user's message. It must match `^[A-Za-z0-9._:-]{8,128}$`.
   If it does not match, reply "That is not a valid deposit address." and stop.
2. Run this one command. Put the address in place of ADDRESS. Do not add other commands to the same line.
   `curl -s --max-time 20 "https://1click.chaindefuser.com/v0/status?depositAddress=ADDRESS"`
3. From the JSON, read:
   - `status`, `updatedAt`
   - `quoteResponse.quoteRequest.originAsset` and `quoteResponse.quoteRequest.destinationAsset` (the chain is the part before the first `-` or `.` after `nep141:`)
   - `quoteResponse.quote.amountInFormatted`, `quoteResponse.quote.amountInUsd`
   - `swapDetails.amountOutFormatted`, `swapDetails.refundedAmountFormatted`, `swapDetails.refundReason`
4. Reply in at most 6 short lines: status, route (origin chain to destination chain), amount sent, amount received or refunded, refund reason, last update.

## What to tell the user

- `SUCCESS`: the swap is complete. Give the amount received.
- `PROCESSING` or `PENDING_DEPOSIT`: the swap is in progress. Tell the user to check again in 10 minutes.
- `INCOMPLETE_DEPOSIT`: the deposit was less than the quote. The rest goes back to the refund address.
- `REFUNDED`: the funds went back to the refund address. Give the reason. A new quote is on the Intents Desk dashboard.
- `FAILED`: the swap failed. Tell the user to contact NEAR Intents support with the deposit address.
- An error, or no `status`: say that 1Click does not know this address.

## Rules

- Never ask for a private key, a seed phrase or a wallet password. If the user offers one, tell them not to share it.
- Never send, move or sign funds. You only read the public status.
- Do not guess a status. Report only what the command returns.
- End every swap answer with: "Not financial advice."
