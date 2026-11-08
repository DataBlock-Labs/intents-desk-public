import React from 'react';
import {
  AbsoluteFill,
  Easing,
  Img,
  OffthreadVideo,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from 'remotion';
import incident from '../../data/incident.json';
import {FPS} from './timeline';

// src/intents_desk/static/tokens.css
export const c = {
  bg: '#0a0a0a',
  surface: '#1a1a1a',
  border: '#404040',
  text: '#e0e0e0',
  muted: '#a0a0a0',
  green: '#00ff88',
  yellow: '#ffcc00',
  red: '#ff4444',
};
export const mono = '"JetBrains Mono", "DejaVu Sans Mono", ui-monospace, monospace';

const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;
const ease = Easing.bezier(0.22, 1, 0.36, 1); // --db-motion-ease-enter

/** The app background: near black with the faint 48 px grid. */
export const Backdrop: React.FC<{children?: React.ReactNode}> = ({children}) => (
  <AbsoluteFill
    style={{
      backgroundColor: c.bg,
      backgroundImage:
        'linear-gradient(rgba(255,255,255,.03) 1px, transparent 1px),' +
        'linear-gradient(90deg, rgba(255,255,255,.03) 1px, transparent 1px)',
      backgroundSize: '48px 48px',
      fontFamily: mono,
      color: c.text,
    }}
  >
    {children}
  </AbsoluteFill>
);

/** Fade a scene in and out over 8 frames, so cuts are soft. */
const useFade = () => {
  const frame = useCurrentFrame();
  const {durationInFrames: d} = useVideoConfig();
  return interpolate(frame, [0, 8, d - 8, d], [0, 1, 1, 0], clamp);
};

export const Clip: React.FC<{name: string}> = ({name}) => (
  <AbsoluteFill style={{opacity: useFade(), backgroundColor: c.bg}}>
    <OffthreadVideo src={staticFile(`clips/${name}.mp4`)} />
  </AbsoluteFill>
);

const Big: React.FC<{at: number; color: string; children: React.ReactNode}> = ({
  at,
  color,
  children,
}) => {
  const t = interpolate(useCurrentFrame(), [at, at + 15], [0, 1], {...clamp, easing: ease});
  return (
    <div style={{opacity: t, transform: `translateY(${(1 - t) * 24}px)`, color, fontSize: 110, fontWeight: 700}}>
      {children}
    </div>
  );
};

export const Hook: React.FC = () => {
  const frame = useCurrentFrame();
  const n = Math.round(interpolate(frame, [3 * FPS, 5 * FPS], [0, incident.swaps], {...clamp, easing: ease}));
  const pct = ((100 * incident.refunded) / incident.swaps).toFixed(1);
  return (
    <Backdrop>
      <AbsoluteFill style={{opacity: useFade(), justifyContent: 'center', padding: '0 160px', gap: 24}}>
        <div style={{color: c.green, fontSize: 32, fontWeight: 700}}>// NEAR INTENTS · 1CLICK</div>
        <Big at={10} color={c.text}>Nov 4, 13:00 UTC</Big>
        <Big at={3 * FPS} color={c.text}>{n.toLocaleString('en-US')} swaps</Big>
        <Big at={5 * FPS} color={c.yellow}>{pct}% REFUNDED</Big>
      </AbsoluteFill>
    </Backdrop>
  );
};

export const Outro: React.FC = () => (
  <Backdrop>
    <AbsoluteFill style={{opacity: useFade(), justifyContent: 'center', alignItems: 'center', gap: 28}}>
      <div style={{fontSize: 96, fontWeight: 700, letterSpacing: 6}}>
        INTENTS <span style={{color: c.muted}}>DESK</span>
      </div>
      <div style={{fontSize: 36, color: c.muted}}>Support data for NEAR Intents</div>
      <div style={{fontSize: 28, color: c.green}}>NEAR Intents track 02 · DataBlock · 2026</div>
    </AbsoluteFill>
  </Backdrop>
);

/** A camera stop: frame the image rect [x, y, w, h] (image px) from second `at`. */
export type Stop = {at: number; view: [number, number, number, number]};

const SAFE = {w: 1920 - 160, h: 1080 - 260}; // keep clear of the caption bar

/** A screenshot, with the camera moving between stops. */
export const Shot: React.FC<{src: string; stops: Stop[]}> = ({src, stops}) => {
  const t = useCurrentFrame() / FPS;
  const i = Math.max(0, stops.findLastIndex((s) => s.at <= t));
  const a = stops[i];
  const b = stops[Math.min(i + 1, stops.length - 1)];
  const k = interpolate(t, [b.at - 1, b.at], [0, 1], {...clamp, easing: ease});
  const v = a.view.map((n, j) => n + (b.view[j] - n) * (b === a ? 0 : k));
  const scale = Math.min(SAFE.w / v[2], SAFE.h / v[3]);
  const x = 960 - (v[0] + v[2] / 2) * scale;
  const y = 950 / 2 - (v[1] + v[3] / 2) * scale; // centre above the captions
  return (
    <Backdrop>
      <AbsoluteFill style={{opacity: useFade()}}>
        <Img
          src={staticFile(`shots/${src}.png`)}
          style={{position: 'absolute', left: 0, top: 0, transformOrigin: '0 0', transform: `translate(${x}px, ${y}px) scale(${scale})`}}
        />
      </AbsoluteFill>
    </Backdrop>
  );
};

/** One caption at a time, over a solid bar so it reads on any frame. */
export const Captions: React.FC<{lines: [number, number, string][]}> = ({lines}) => {
  const t = useCurrentFrame() / FPS;
  const line = lines.find(([from, to]) => t >= from && t < to);
  if (!line) return null;
  const o = interpolate(t, [line[0], line[0] + 0.2, line[1] - 0.2, line[1]], [0, 1, 1, 0], clamp);
  return (
    <AbsoluteFill style={{justifyContent: 'flex-end', alignItems: 'center', paddingBottom: 56}}>
      <div
        style={{
          opacity: o,
          maxWidth: 1500,
          textAlign: 'center',
          fontFamily: mono,
          fontSize: 40,
          lineHeight: 1.35,
          color: c.text,
          background: 'rgba(10,10,10,0.92)',
          border: `1px solid ${c.border}`,
          padding: '14px 32px',
        }}
      >
        {line[2]}
      </div>
    </AbsoluteFill>
  );
};
