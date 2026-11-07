# Page override: Support case and engineer report (design, 2026-11-06)

Status: **built 2026-11-06** (branch `support-cases`). It follows `../MASTER.md` (DataBlock override). This file adds rules for two views.

The source is a talk with a NEAR engineer. The two best uses for users are:
1. Improve the support process with data.
2. Write strong data reports. Each report refers to blocks, trades and other resources, so engineers of the tools involved can act.

## Information architecture

```
Where's my swap?  ──►  Case file (one swap)  ──►  "Part of incident X"  ──►  Incident report
 (address or tx)        #case=<ref>                 link                      #incident=<id>
                              │                                                    │
                              └── Copy support message / Download .md ─────────────┤
                                                                                   └── Reproduce block
                                                                                       Download CSV / JSON
```

- **One input, three kinds of key:** a deposit address, a NEAR tx hash, or an origin-chain tx hash. The server finds the kind from the form of the key. It never asks the user which kind it is.
- **Deep links:** `#case=<ref>` (the public ref, never the address) and `#incident=<id>`. The Back button closes the view. This is the same pattern that the partner and incident views use now.
- **No new top-level section.** The case file replaces the current `#swap` result under "Where's my swap?". The engineer report is the current incident detail with more data.

## View 1: Case file (end users and support staff)

The order goes from the answer, to why it happened, to what to do, to the proof.

```
// CASE  ref 7f3a…  [Copy link] [Copy support message] [Download .md]
┌──────────────────────────────────────────────────────────────────────┐
│ REFUNDED  ·  15,007 USDT (Tron) → USDT (Ethereum)  ·  $14,996        │  ← status word + colour
│ Your funds came back: 15,006.13 USDT to your refund address.         │  ← one-sentence answer
├──────────────────────────────────────────────────────────────────────┤
│ Why        QUOTE_EXPIRED: the deposit arrived after the quote        │
│            deadline, so no solver could fill it.                     │
│ Next step  Get a new quote and send again. [Swap again]              │
│ Incident   Part of "partner-3 @ 2026-11-04 13:00Z" (21,302 refunds). │  ← only if it matches
├──────────────────────────────────────────────────────────────────────┤
│ Timeline (UTC)                                                       │
│  ● 13:05:12  Quote made                                              │
│  ● 13:07:40  Deposit seen        Tron tx 9c1e…a4  [tronscan ↗]       │
│  ● 13:18:02  Refund sent         NEAR tx 7nLm…f2  block 131,224,…    │
│                                  [nearblocks ↗]                      │
│  ○ —         Delivered           (did not happen)                    │
├──────────────────────────────────────────────────────────────────────┤
│ References  table: chain · kind · tx hash · block · explorer link    │
└──────────────────────────────────────────────────────────────────────┘
```

**Rules**
- **The answer comes first.** One sentence in plain words that matches the status, for example "Your funds came back" or "Still working, usually done in 2 min".
- **Status:** the status word plus colour (SUCCESS green, REFUNDED or PROCESSING yellow, FAILED red). Colour is never the only signal.
- **Reason table.** A fixed map in code turns each `refundReason` into a cause and a next step. An unknown reason shows the raw code and the text "Not known yet: copy the support message".
- **Timeline steps:** quote, deposit seen, processing, then delivered or refunded or failed.
  - A step that did not happen gets a hollow dot and the text "did not happen". Grey alone is not enough.
  - This is an ordered list (`<ol>`), not a chart.
- **References table:** one row per transaction.
  - Columns: chain, kind (deposit, intent, refund, delivery), hash (shortened, the full hash is in `title` and the copy button), block height, and the explorer link.
  - NEAR block heights come from `silver.event` when the index has the tx. When it does not, they come from one RPC `tx_status` call. A failed call shows "—", never an error.
- **"Part of incident X"** appears only when the swap's referral and time fall inside an incident. It links to the report. This tells a user that the problem is not their mistake.
- **Copy support message:** a plain-text block to paste into NEAR or wallet support chats. It holds:
  - the ref, status, reason, route, amount and UTC time;
  - the tx hashes with explorer links, and the incident id.
  - It never holds the recipient address, the refund address or the memo. The deposit address is in it only when the user typed it in this session.
- **Download .md:** the same content as a file. A support person attaches it to a ticket.

**States**
| State | Shows |
|---|---|
| Loading | The skeleton takes the final height (CLS < 0.1). The button shows "Checking…" and is disabled. |
| Not found | "1Click does not know this key." This is followed by what to check: the full address, the right network, a wait of 1 min after the deposit. |
| Bad input | An error under the field that names the accepted forms. Focus goes back to the field. |
| Processing | The case reloads every 20 s with `role=status` "Still processing, checked 13:07:40". It stops on a final status. |
| Raw data gone (> 24 h) | "Details are kept for 24 hours." The view shows what is still known: the incident match and the public tx hashes, if a saved case has them. |

## View 2: Engineer report (wallets, partners, solvers)

This view grows the current incident detail and the markdown report into one layout. The page, `reports/incidents/*.md` and the vault note all use it.

```
// INCIDENT  partner-3 @ 2026-11-04T13:00Z   CLOSED · lasted 24 h 3 min
Summary      22,505 swaps · 94.7% refunded · 5.3% failed · 18 senders · $X in
             Top reason INTENT_SUBMIT_FAILED · others in same hours: 1.2% bad
Block range  NEAR 131,220,004 → 131,301,877 (first and last evidence tx)
Who should look
  Wallet / widget team   referral id, deposit type INTENTS, refund type INTENTS
  Solvers                routes and amounts with no fill (table)
  1Click / NEAR          reason codes and sample intent hashes
Chart        per-hour swaps and bad %: one axis per chart (two small charts, not dual axis)
Evidence     time · status · reason · route · amount · NEAR tx · block · origin tx · intent hash · links
Data gaps    "No data 15:00–16:00Z" if any hour is missing
Reproduce    copyable commands (1Click status, Explorer query by referral + time, NEAR RPC tx_status)
Download     evidence.csv · incident.json
```

**Rules**
- **Every claim has a reference.** A number links to the rows behind it. An evidence row links to a block explorer.
  - NEAR uses nearblocks.
  - Other chains use the `explorerUrl` that 1Click returns. Never build those URLs by hand.
- **Block height on every NEAR row.** Each report also gets a block range, so an engineer can replay the incident from an archival node.
- **Reproduce block:** `<pre>` with a copy button. The commands use placeholders such as `$ONECLICK_JWT`, never a key. The referral id and the time window are filled in.
- **Two small charts, not one dual-axis chart:** swaps per hour and bad % per hour, with the shared x axis aligned. Use the `#3987e5` series token and a table fallback (the dataviz rule).
- **Masking:** when `MASK_PARTNERS=1`, the page shows the masked label, and evidence and Reproduce are hidden, as they are now. The full report goes only to files and the owner.
- **Retention:** keep only public chain data (tx hashes, block heights, intent hashes) and totals. 1Click raw rows stay ≤ 24 h, per the 1Click terms.

## Support data loop ("improve the support process with data")

- **Lookup log:** count each case lookup by status, reason, route and whether it matched an incident. It holds no address and no key, only counts per hour.
- **"What people ask about":** a small table under Network health. It shows the top reasons from lookups in the last 24 h.
- **Early warning:** if lookups for one reason jump (for example 10 or more in an hour), the stale or incident alert path tells the owner on Telegram before the 50-swap rule fires.
- **The weekly report gets a "Support" part:** lookups, reasons, the share that matched an incident, and unknown reasons that need a new map entry.

## Storage (decided 2026-11-06)

- **Where:** the app's own DuckDB on the Hetzner server `intents-desk` (`deploy/hetzner/`). edwar controls this server.
  - The tailnet server (ZBOX) keeps its own copy for the lookups made there.
  - No new database and no new service.
- **Backup:** the daily job already writes the backup (`BACKUP_PUSH`). Add the two new tables to its table list.
- **`app.support_case`:** kept until deleted. It holds only public chain data and the facts of the case:
  - `ref` (PK, the public ref), `first_seen`, `updated_at`;
  - `status`, `reason`, `route`, `amount_in`, `usd_in`;
  - `referral`, `incident_id`;
  - `refs` JSON: `[{chain, kind, hash, block, explorer_url}]`.
  - **Never store:** the deposit address, the recipient, the refund address, the memo, or the raw 1Click reply. The 24 h limit of the 1Click terms applies to raw rows. A tx hash and a block height are public chain data.
- **Effect:** a case link (`#case=<ref>`) still works after 24 h. It shows the saved status, references and incident link. Live 1Click data is added only while 1Click still has the swap.
- **`ops.support_lookup`:** counts per hour by status, reason, route and incident match. It holds no address and no key. It feeds "What people ask about" and the early-warning rule.
- **Size:** about 1 KB per case, so 100,000 cases are about 100 MB. That fits the cax11 disk.

## Accessibility and interaction

- **Labels:** the input has a visible label and helper text that lists the 3 accepted forms. The error appears under the field and is linked with `aria-describedby`.
- **Screen readers:** one `role=status` region per view, with atomic messages ("Refunded, 15,006 USDT back").
- **Copy buttons:** each copy button is a 44 px target with a text label ("Copy hash"), not an icon alone. After a copy, the label shows "Copied" for 2 s and `role=status` announces it.
- **Hashes:** hashes use the mono font with `word-break: break-all` inside a horizontal-scroll table wrapper. Links that open an explorer say so ("nearblocks, opens new tab") and use `rel="noopener noreferrer"`.
- **Motion:** none, except the 150 ms copy-state change. Nothing moves when reduced motion is on.

## Open questions (resolve before build)

1. ~~Tx-hash lookup~~: **checked.** The Explorer `search=` finds a swap by its NEAR or origin tx hash.
2. ~~Saved cases~~: **decided (edwar, 2026-11-06).** Store saved cases in a database that edwar controls. See *Storage* below.
3. ~~Timeline times~~: **checked.** The status reply has `quoteResponse.timestamp`, the deadline and `updatedAt`. The NEAR step uses the block time.
