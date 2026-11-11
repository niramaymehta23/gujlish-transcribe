import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { rand } from "./backdrops";
import type { Look } from "./looks";
import { frameGeometry, useLook } from "./theme";

// Collage accents: small abstract shapes — triangles, discs, zigzags, squiggles, halftone patches, tape — that pop in
// around a graphic, float while it is up and pop out with it. Each scene gets its own set, placed in the frame's
// margins so they never sit on the text, and each look draws them in its own manner.

const KINDS = [
  "triangle",
  "disc",
  "ring",
  "half",
  "zigzag",
  "squiggle",
  "plus",
  "spark",
  "dots",
  "tape",
] as const;
type Kind = (typeof KINDS)[number];

// Places in the margins, as shares of the frame. A split scene only uses the ones below the video.
// Places in the margins, as shares of the frame: along the top, half off the side edges, and in the bottom corners,
// so a shape never sits on the graphic's text or pictures, nor on the captions low in the middle. A split scene only
// uses the ones beside its graphic.
const SLOTS = {
  vertical: [
    [0.2, 0.09],
    [0.5, 0.075],
    [0.8, 0.1],
    [0.0, 0.3],
    [1.0, 0.36],
    [0.0, 0.56],
    [1.0, 0.62],
    [0.08, 0.9],
    [0.92, 0.89],
  ],
  wide: [
    [0.25, 0.06],
    [0.55, 0.05],
    [0.82, 0.07],
    [0.0, 0.4],
    [1.0, 0.5],
    [0.03, 0.93],
    [0.97, 0.92],
  ],
};

const colours = (look: Look) =>
  look.name === "engraving"
    ? [look.ink, look.accent, look.ink, look.accent3]
    : [look.accent, look.accent3, look.accent2, look.ink];

const Shape: React.FC<{
  kind: Kind;
  size: number;
  colour: string;
  look: Look;
  seed: number;
}> = ({ kind, size, colour, look, seed }) => {
  const line = look.name === "engraving";
  const stroke = size * 0.11;
  const paint = line
    ? {
        fill: "none",
        stroke: colour,
        strokeWidth: stroke,
        strokeLinecap: "round" as const,
        strokeLinejoin: "round" as const,
      }
    : { fill: colour };
  const s = size;
  const half = s / 2;
  let body: React.ReactNode;
  switch (kind) {
    case "triangle":
      body = (
        <polygon
          points={`${half},${s * 0.08} ${s * 0.94},${s * 0.9} ${s * 0.06},${s * 0.9}`}
          {...paint}
        />
      );
      break;
    case "disc":
      body = <circle cx={half} cy={half} r={s * 0.42} {...paint} />;
      break;
    case "ring":
      body = (
        <circle
          cx={half}
          cy={half}
          r={s * 0.36}
          fill="none"
          stroke={colour}
          strokeWidth={s * 0.13}
        />
      );
      break;
    case "half":
      body = (
        <path
          d={`M ${s * 0.06} ${s * 0.7} A ${s * 0.44} ${s * 0.44} 0 0 1 ${s * 0.94} ${s * 0.7} Z`}
          {...paint}
        />
      );
      break;
    case "zigzag":
      body = (
        <polyline
          points={`${s * 0.04},${s * 0.66} ${s * 0.22},${s * 0.34} ${s * 0.4},${s * 0.66} ${s * 0.58},${s * 0.34} ${s * 0.76},${s * 0.66} ${s * 0.96},${s * 0.34}`}
          fill="none"
          stroke={colour}
          strokeWidth={s * 0.12}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      );
      break;
    case "squiggle":
      body = (
        <path
          d={`M ${s * 0.05} ${half} C ${s * 0.2} ${s * 0.15}, ${s * 0.35} ${s * 0.85}, ${half} ${half} S ${s * 0.8} ${s * 0.15}, ${s * 0.95} ${half}`}
          fill="none"
          stroke={colour}
          strokeWidth={s * 0.11}
          strokeLinecap="round"
        />
      );
      break;
    case "plus":
      body = (
        <path
          d={`M ${half} ${s * 0.12} L ${half} ${s * 0.88} M ${s * 0.12} ${half} L ${s * 0.88} ${half}`}
          stroke={colour}
          strokeWidth={s * 0.18}
          strokeLinecap="round"
        />
      );
      break;
    case "spark":
      body = (
        <path
          d={`M ${half} ${s * 0.04} Q ${s * 0.56} ${s * 0.44} ${s * 0.96} ${half} Q ${s * 0.56} ${s * 0.56} ${half} ${s * 0.96} Q ${s * 0.44} ${s * 0.56} ${s * 0.04} ${half} Q ${s * 0.44} ${s * 0.44} ${half} ${s * 0.04} Z`}
          {...paint}
        />
      );
      break;
    case "dots": {
      const dots = [];
      const step = s / 6;
      for (let x = step / 2; x < s; x += step) {
        for (let y = step / 2; y < s; y += step) {
          const d = Math.hypot(x - half, y - half);
          if (d < half * 0.95) {
            dots.push(
              <circle
                key={`${x}-${y}`}
                cx={x}
                cy={y}
                r={step * 0.32 * (1 - (d / half) * 0.5)}
                fill={colour}
              />,
            );
          }
        }
      }
      body = <g>{dots}</g>;
      break;
    }
    case "tape":
      body = (
        <rect
          x={0}
          y={s * 0.32}
          width={s}
          height={s * 0.36}
          fill={look.name === "paper" ? "rgba(231,176,74,0.75)" : `${colour}aa`}
        />
      );
      break;
  }
  const filter =
    look.name === "paper"
      ? `drop-shadow(0 ${s * 0.05}px ${s * 0.06}px rgba(42,34,24,0.3))`
      : look.name === "editorial"
        ? `drop-shadow(${s * 0.05}px ${s * 0.05}px 0 rgba(0,0,0,0.18))`
        : undefined;
  return (
    <svg
      width={s}
      height={s}
      viewBox={`0 0 ${s} ${s}`}
      style={{ overflow: "visible", filter }}
      data-seed={seed}
    >
      {body}
    </svg>
  );
};

export const Accents: React.FC<{
  beat: number;
  frames: number;
  split: boolean;
}> = ({ beat, frames, split }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { width, height, fps } = useVideoConfig();
  const g = frameGeometry(width, height);
  const below = split && g.vertical ? (g.half.h + 30) / height : 0;
  const left = split && !g.vertical ? 0.53 : 0;
  const slots = (g.vertical ? SLOTS.vertical : SLOTS.wide).filter(
    ([x, y]) => y > below && x > left,
  );
  // A different handful of shapes, in different places, for every scene.
  const count = Math.min(slots.length, split ? 3 : 6);
  const order = slots
    .map((slot, i) => ({ slot, key: rand(beat * 17 + i * 3.3) }))
    .sort((a, b) => a.key - b.key)
    .slice(0, count);
  const palette = colours(look);
  const leave = interpolate(frame, [frames - 6, frames], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      {order.map(({ slot: [x, y] }, i) => {
        const seed = beat * 31 + i * 7;
        const kind = KINDS[Math.floor(rand(seed) * KINDS.length)];
        const size =
          (kind === "tape" ? 150 : 60 + rand(seed + 1) * 60) * g.unit;
        const pop = spring({
          frame: frame - 2 - i * 3,
          fps,
          config: { damping: 10, stiffness: 170, mass: 0.6 },
        });
        const spin = (rand(seed + 2) - 0.5) * 70;
        const float = Math.sin(frame / 18 + i * 1.3) * 10 * g.unit;
        const drift = Math.sin(frame / 40 + i) * 8;
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              left: x * width - size / 2,
              top: y * height - size / 2 + float,
              scale: `${pop * leave}`,
              rotate: `${spin + drift + (1 - pop) * -120}deg`,
            }}
          >
            <Shape
              kind={kind}
              size={size}
              colour={palette[i % palette.length]}
              look={look}
              seed={seed}
            />
          </div>
        );
      })}
    </AbsoluteFill>
  );
};
