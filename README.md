# Intents Desk

**Where's my swap?** Open, re-runnable data on NEAR Intents, and an agent that answers from it.

NEAR Intents moves billions a month across 35+ chains. Users still report swaps stuck for hours or
days, and public volume figures disagree with each other. Intents Desk reads the chain itself
(`intents.near` events), keeps every raw response, and builds tables you can re-run and check.

Built at NEAR HACKATHON (Nov 1–14, 2026) by DataBlock. Companion project: Data Bounty Swarm
(link added on release).

## Status

Work in progress. The table below shows what runs today. Each line is verified by the
command next to it.

| Part | State | Check |
|---|---|---|
| Fetch the last N final blocks and keep every `intents.near` receipt (neardata) | works | `make fetch BLOCKS=200` |
| Parse `EVENT_JSON` logs (dip4, nep245, nep141) into `silver.event` | works | `make stats` |
| One row per swap side from dip4 `token_diff` (`silver.token_diff`) | works | `make test` |
| Re-runs change nothing (keyed inserts) | works | `make test` |
| Swap status trail from the 1Click API | works | `uv run intents-desk swap <deposit-address>` |
| Gold metrics, thesis report, agent, dashboard | planned | — |

## Quick start

Needs Python 3.12+ and [uv](https://docs.astral.sh/uv/). No API keys.

```sh
make test          # unit tests on real recorded transactions
make fetch BLOCKS=200  # about 2 minutes of chain time; resumable
make stats         # what the database holds
```

## Data sources

All public and read-only:

- [neardata](https://mainnet.neardata.xyz) (FastNEAR): full blocks with receipt outcomes and logs.
  The free tier allows about 180 requests a minute per IP, so `fetch` waits 0.35 s per block.
- NEAR RPC `EXPERIMENTAL_tx_status`: one transaction with its receipts (used for test fixtures)
- [1Click API](https://1click.chaindefuser.com/v0/tokens): token list, prices, swap status

## First sample (2026-11-04)

600 blocks (222977243–222977842, about 6.5 minutes of chain time): 575 transactions,
1,573 `intents.near` receipts, 1,695 events, 17 swap intents (`token_diff`) across 9 accounts.

## Layout

- `bronze.receipt`: one raw `intents.near` receipt outcome, unchanged
- `bronze.block_seen`: block heights already fetched (makes `fetch` resumable)
- `silver.event`: one row per event log entry, keyed by (receipt, log, data index)
- `silver.token_diff`: one row per (intent, token) balance change

## License

MIT
