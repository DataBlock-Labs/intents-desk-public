// The scene order, lengths and captions (see ../../storyboard.md).
// No imports, so `node srt.mts` can read it too.

export const FPS = 30;

export type Scene = {
  id: string;
  frames: number;
  captions: [from: number, to: number, text: string][]; // seconds in the scene
};

export const timeline = (clips: Record<string, number>): Scene[] => [
  {
    id: 'Hook',
    frames: 8 * FPS,
    captions: [
      [0.3, 3, 'Nov 4, 13:00 UTC.'],
      [3, 7.8, '43,500 swaps in one day. 95% came back refunded.'],
    ],
  },
  {
    id: 'SwapFlow',
    frames: clips.SwapFlow,
    captions: [
      [1, 11, 'A 1Click swap: quote, deposit, intent on NEAR, solver fill.'],
      [11, 22, 'When no solver fills it, the funds go back: a refund.'],
    ],
  },
  {
    id: 'IncidentHours',
    frames: clips.IncidentHours,
    captions: [
      [1, 7, 'Intents Desk counts every swap, every hour.'],
      [7, 13, "One partner's swaps: 100% bad for 24 hours."],
      [13, 22, 'Rule: 50+ swaps and 50% bad in an hour opens an incident.'],
    ],
  },
  {
    id: 'Case',
    frames: 25 * FPS,
    captions: [
      [0.3, 4, 'Paste a deposit address or any tx hash.'],
      [4, 9, 'One answer first: what happened to your funds.'],
      [9, 19, 'The timeline and every tx, with its NEAR block.'],
      [19, 24.8, 'Copy a support message. No addresses in it.'],
    ],
  },
  {
    id: 'EvidenceChain',
    frames: clips.EvidenceChain,
    captions: [
      [1, 7, 'Case → intent hash → NEAR block → explorer.'],
      [7, 13.5, 'Anyone can check it on chain.'],
    ],
  },
  {
    id: 'Incident',
    frames: 17 * FPS,
    captions: [
      [0.3, 6, 'Incidents get a report for the teams who can fix them.'],
      [6, 13, 'Who should look, block range, commands to reproduce.'],
      [13, 16.8, 'Download the evidence as CSV or JSON.'],
    ],
  },
  {
    id: 'Outro',
    frames: 8 * FPS,
    captions: [[0.5, 7.8, 'Intents Desk: data for NEAR Intents support.']],
  },
];
