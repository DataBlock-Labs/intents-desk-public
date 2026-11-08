import React from 'react';
import {Series} from 'remotion';
import clips from './clips.json';
import {Captions, Clip, Hook, Outro, Shot} from './parts';
import {FPS, timeline} from './timeline';

export const scenes = timeline(clips);
export const totalFrames = scenes.reduce((n, s) => n + s.frames, 0);

// Camera stops in screenshot pixels (2x device scale, see ../../shots.py).
const body: Record<string, React.ReactNode> = {
  Hook: <Hook />,
  SwapFlow: <Clip name="SwapFlow" />,
  IncidentHours: <Clip name="IncidentHours" />,
  EvidenceChain: <Clip name="EvidenceChain" />,
  Case: (
    <Series>
      <Series.Sequence durationInFrames={4 * FPS}>
        <Shot src="1-lookup" stops={[{at: 0, view: [0, 0, 1136, 432]}]} />
      </Series.Sequence>
      <Series.Sequence durationInFrames={15 * FPS}>
        <Shot
          src="2-case"
          stops={[
            {at: 0, view: [0, 370, 1136, 380]},
            {at: 5, view: [0, 770, 1136, 330]},
            {at: 10, view: [0, 1100, 1136, 260]},
          ]}
        />
      </Series.Sequence>
      <Series.Sequence durationInFrames={6 * FPS}>
        <Shot src="3-copied" stops={[{at: 0, view: [0, 1100, 1136, 400]}]} />
      </Series.Sequence>
    </Series>
  ),
  Incident: (
    <Shot
      src="5-incident"
      stops={[
        {at: 0, view: [0, 0, 1500, 520]},
        {at: 3, view: [0, 520, 1500, 560]},
        {at: 6.5, view: [0, 1170, 1800, 360]},
        {at: 10, view: [1190, 1520, 1146, 640]},
        {at: 13.5, view: [0, 3640, 1500, 560]},
      ]}
    />
  ),
  Outro: <Outro />,
};

export const Explainer: React.FC = () => (
  <Series>
    {scenes.map((s) => (
      <Series.Sequence key={s.id} durationInFrames={s.frames} name={s.id}>
        {body[s.id]}
        <Captions lines={s.captions} />
      </Series.Sequence>
    ))}
  </Series>
);
