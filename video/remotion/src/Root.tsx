import React from 'react';
import {Composition} from 'remotion';
import {Explainer, totalFrames} from './Explainer';
import {FPS} from './timeline';

export const RemotionRoot: React.FC = () => (
  <Composition
    id="Explainer"
    component={Explainer}
    durationInFrames={totalFrames}
    fps={FPS}
    width={1920}
    height={1080}
  />
);
