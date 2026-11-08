// Write the captions as SRT (for X and other players): node srt.mts > ../../docs/video/explainer.srt
import {readFileSync} from 'node:fs';
import {FPS, timeline} from './src/timeline.ts';

const clips = JSON.parse(readFileSync(new URL('./src/clips.json', import.meta.url), 'utf8'));
const stamp = (s: number) =>
  new Date(Math.round(s * 1000)).toISOString().slice(11, 23).replace('.', ',');
let start = 0;
let n = 0;
for (const scene of timeline(clips)) {
  for (const [from, to, text] of scene.captions) {
    console.log(`${++n}\n${stamp(start + from)} --> ${stamp(start + to)}\n${text}\n`);
  }
  start += scene.frames / FPS;
}
