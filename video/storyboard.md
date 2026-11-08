# Intents Desk explainer: storyboard

- **Format:** 1920×1080, 30 fps, about 2 min, captions only.
- **Data:** the numbers come from `data/incident.json`, a masked export from `export_data.py`.
- **Screens:** the screenshots use demo data from `tests/fakes.py`, not live swaps.
- **Never show** a partner name, a user address or a key.
- **Captions:** at most 2 lines per card, about 12 words per line. `remotion/src/captions.ts` holds the same text and must stay in step with this file.

| # | Scene | Tool | About | Captions |
|---|---|---|---|---|
| 1 | Hook | Remotion `Hook` | 8 s | "Nov 4, 13:00 UTC." / "43,500 swaps in one day. 95% came back refunded." / "Users saw only: refunded." |
| 2 | How a swap works | Manim `SwapFlow` | 22 s | "A 1Click swap: quote, deposit, intent on NEAR, solver fill." / "When no solver fills it, the funds go back: a refund." |
| 3 | The incident | Manim `IncidentHours` | 25 s | "Intents Desk counts every swap, every hour." / "One partner's swaps: 100% bad for 24 hours." / "Rule: ≥ 50 swaps and ≥ 50% bad in an hour opens an incident." |
| 4 | Where's my swap? | Remotion `Shots` | 25 s | "Paste a deposit address or any tx hash." / "One answer first: what happened to your funds." / "The timeline and every tx, with its NEAR block." / "Copy a support message. No addresses in it." |
| 5 | Every claim has a reference | Manim `EvidenceChain` | 15 s | "Case → intent hash → NEAR block → explorer." / "Anyone can check it on chain." |
| 6 | Engineer report | Remotion `Shots` | 17 s | "Incidents get a report for the teams who can fix them." / "Who should look, block range, commands to reproduce." / "Download the evidence as CSV or JSON." |
| 7 | Outro | Remotion `Outro` | 8 s | "Intents Desk: data for NEAR Intents support." / "NEAR Intents track 02 · DataBlock" |
