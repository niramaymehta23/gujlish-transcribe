import React from "react";
import {
  AbsoluteFill,
  Easing,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { useLook } from "./theme";

const clamp = {
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
} as const;
const OUT = Easing.bezier(0.16, 1, 0.3, 1);

// A repeatable random number for a seed, so every render of a frame is identical.
export const rand = (seed: number) => {
  const x = Math.sin(seed * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
};

// Film grain that changes every other frame, so flat colour never looks dead.
export const Grain: React.FC<{ opacity: number; blend?: string }> = ({
  opacity,
  blend = "multiply",
}) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const seed = Math.floor(frame / 2) % 12;
  return (
    <AbsoluteFill
      style={{
        opacity,
        mixBlendMode: blend as React.CSSProperties["mixBlendMode"],
      }}
    >
      <svg width={width} height={height}>
        <filter id={`grain-${seed}`}>
          <feTurbulence
            type="fractalNoise"
            baseFrequency="0.85"
            numOctaves="2"
            seed={seed}
            stitchTiles="stitch"
          />
          <feColorMatrix type="saturate" values="0" />
        </filter>
        <rect width="100%" height="100%" filter={`url(#grain-${seed})`} />
      </svg>
    </AbsoluteFill>
  );
};

// 0 → 1 over the first frames of a scene, for moving the backdrop's motifs to their new places on each idea.
const useChange = (beat: number, beatFrame: number) =>
  beat === 0
    ? 1
    : interpolate(beatFrame, [0, 18], [0, 1], { ...clamp, easing: OUT });

type BeatProps = { beat: number; beatFrame: number };

// A torn edge: a jagged line across the frame at height y, for paper sheets.
const tornPath = (
  seed: number,
  width: number,
  y: number,
  depth: number,
  below: boolean,
  far: number,
) => {
  const steps = 26;
  let d = `M -20 ${below ? far : -20} L -20 ${y}`;
  for (let i = 0; i <= steps; i++) {
    const x = (i / steps) * (width + 40) - 20;
    const jag =
      (rand(seed + i * 3.1) - 0.5) * depth +
      Math.sin(i * 0.7 + seed) * depth * 0.4;
    d += ` L ${x.toFixed(1)} ${(y + jag).toFixed(1)}`;
  }
  return `${d} L ${width + 20} ${below ? far : -20} Z`;
};

// A paper petal: two curves meeting at the tips.
const petal = (length: number, girth: number) =>
  `M 0 ${-length / 2} Q ${girth} 0 0 ${length / 2} Q ${-girth} 0 0 ${-length / 2} Z`;

// Paper: a torn teal sheet across the top, a torn strip under the captions, and cut-paper petals drifting at the edges.
const Paper: React.FC<BeatProps> = ({ beat, beatFrame }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const unit = Math.min(width, height) / 1080;
  const change = useChange(beat, beatFrame);
  const vertical = height > width;
  const topEdge = (vertical ? 210 : 120) * unit;
  const bottomEdge = height - (vertical ? 250 : 40) * unit;
  // Petals sit in the side margins and settle somewhere new on every scene. Each stays on its own side, so it never
  // drifts across the text or pictures in the middle.
  const sides = vertical
    ? [
        [
          [0.025, 0.2],
          [0.03, 0.5],
          [0.025, 0.66],
        ],
        [
          [0.975, 0.26],
          [0.97, 0.56],
          [0.975, 0.7],
        ],
      ]
    : [
        [
          [0.02, 0.3],
          [0.025, 0.7],
        ],
        [
          [0.98, 0.25],
          [0.975, 0.72],
        ],
      ];
  const count = vertical ? 4 : 4;
  const colours = [look.accent, look.accent2, look.accent3];
  return (
    <AbsoluteFill style={{ backgroundColor: look.ground, overflow: "hidden" }}>
      <Grain opacity={0.2} />
      <svg width={width} height={height} style={{ position: "absolute" }}>
        <defs>
          <filter id="lift" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow
              dx="0"
              dy={6 * unit}
              stdDeviation={8 * unit}
              floodColor="#2a2218"
              floodOpacity="0.25"
            />
          </filter>
        </defs>
        <path
          d={tornPath(3, width, topEdge + 18 * unit, 16 * unit, false, 0)}
          fill={look.accent3}
          filter="url(#lift)"
        />
        <path
          d={tornPath(9, width, topEdge, 22 * unit, false, 0)}
          fill={look.accent2}
          filter="url(#lift)"
        />
        <path
          d={tornPath(5, width, bottomEdge, 20 * unit, true, height + 20)}
          fill={look.accent}
          filter="url(#lift)"
        />
        {Array.from({ length: count }, (_, i) => {
          const side = sides[i % 2];
          const k = Math.floor(i / 2);
          const from = side[(k + beat - 1 + side.length * 4) % side.length];
          const to = side[(k + beat) % side.length];
          const x = (from[0] + (to[0] - from[0]) * change) * width;
          const y = (from[1] + (to[1] - from[1]) * change) * height;
          const size = (80 + rand(i) * 40) * unit;
          const turn =
            rand(i + 20) * 360 + change * 60 + Math.sin(frame / 45 + i) * 6;
          return (
            <g
              key={i}
              transform={`translate(${x} ${y}) rotate(${turn})`}
              filter="url(#lift)"
            >
              <path d={petal(size, size * 0.42)} fill={colours[i % 3]} />
              <path
                d={`M 0 ${-size * 0.4} L 0 ${size * 0.4}`}
                stroke={look.ground}
                strokeOpacity={0.45}
                strokeWidth={3 * unit}
              />
            </g>
          );
        })}
      </svg>
    </AbsoluteFill>
  );
};

// Engraving: guilloché bands — many fine, interlaced waves — at the top and bottom, and a ruled frame with corner
// ornaments that draws itself in.
const Guilloche: React.FC<{ y: number; band: number; phase: number }> = ({
  y,
  band,
  phase,
}) => {
  const look = useLook();
  const { width, height } = useVideoConfig();
  const unit = Math.min(width, height) / 1080;
  const lines = 18;
  const steps = 60;
  return (
    <g>
      {Array.from({ length: lines }, (_, i) => {
        let d = "";
        for (let s = 0; s <= steps; s++) {
          const x = (s / steps) * width;
          const t = (s / steps) * Math.PI * 6;
          const wave =
            Math.sin(t + phase + i * 0.35) * band * 0.32 +
            Math.sin(t * 0.5 - phase * 0.7 + i * 0.2) * band * 0.16;
          d += `${s ? " L" : "M"} ${x.toFixed(1)} ${(y + wave).toFixed(1)}`;
        }
        return (
          <path
            key={i}
            d={d}
            fill="none"
            stroke={look.ink}
            strokeOpacity={0.32}
            strokeWidth={1.3 * unit}
          />
        );
      })}
    </g>
  );
};

const Engraving: React.FC<BeatProps> = ({ beat, beatFrame }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const unit = Math.min(width, height) / 1080;
  const vertical = height > width;
  const draw = interpolate(frame, [0, 40], [0, 1], { ...clamp, easing: OUT });
  const change = useChange(beat, beatFrame);
  const inset = 34 * unit;
  const corner = 70 * unit;
  const corners: [number, number, number][] = [
    [inset, inset, 0],
    [width - inset, inset, 90],
    [width - inset, height - inset, 180],
    [inset, height - inset, 270],
  ];
  return (
    <AbsoluteFill style={{ backgroundColor: look.ground }}>
      <Grain opacity={0.24} />
      {/* a halftone shadow gathering in the corners */}
      <AbsoluteFill
        style={{
          backgroundImage: `radial-gradient(${look.ink}50 ${2 * unit}px, transparent ${2.4 * unit}px)`,
          backgroundSize: `${12 * unit}px ${12 * unit}px`,
          maskImage:
            "radial-gradient(ellipse at 50% 45%, transparent 55%, black 100%)",
          opacity: 0.6,
        }}
      />
      <svg width={width} height={height} style={{ position: "absolute" }}>
        <Guilloche
          y={(vertical ? 74 : 54) * unit}
          band={70 * unit}
          phase={frame / 40 + beat * 0.9 * change}
        />
        <Guilloche
          y={height - (vertical ? 190 : 70) * unit}
          band={150 * unit}
          phase={-frame / 50 - beat * 0.9 * change}
        />
        <rect
          x={inset}
          y={inset}
          width={width - inset * 2}
          height={height - inset * 2}
          fill="none"
          stroke={look.ink}
          strokeWidth={3 * unit}
          pathLength={1}
          strokeDasharray={1}
          strokeDashoffset={1 - draw}
        />
        <rect
          x={inset + 12 * unit}
          y={inset + 12 * unit}
          width={width - inset * 2 - 24 * unit}
          height={height - inset * 2 - 24 * unit}
          fill="none"
          stroke={look.ink}
          strokeWidth={1.2 * unit}
          pathLength={1}
          strokeDasharray={1}
          strokeDashoffset={1 - draw}
        />
        {corners.map(([x, y, turn]) => (
          <g
            key={turn}
            transform={`translate(${x} ${y}) rotate(${turn})`}
            opacity={draw}
          >
            <path
              d={`M 0 ${corner} Q 0 0 ${corner} 0 M ${corner * 0.35} ${corner * 0.35} m -${8 * unit} 0 a ${8 * unit} ${8 * unit} 0 1 0 ${16 * unit} 0 a ${8 * unit} ${8 * unit} 0 1 0 -${16 * unit} 0`}
              fill="none"
              stroke={look.ink}
              strokeWidth={2.4 * unit}
            />
            <rect
              x={-7 * unit}
              y={-7 * unit}
              width={14 * unit}
              height={14 * unit}
              fill={look.accent}
              transform="rotate(45)"
            />
          </g>
        ))}
      </svg>
    </AbsoluteFill>
  );
};

// Editorial: a few flat blocks of colour on a Swiss grid, rearranged for each scene, always clear of the text.
const EDITORIAL_LAYOUTS = [
  // [x, y, w, h, radius] as shares of the frame, for a slab and a disc
  [
    [-0.1, -0.05, 0.42, 0.16, 0],
    [0.78, 0.86, 0.34, 0.34, 1],
  ],
  [
    [0.7, -0.05, 0.42, 0.16, 0],
    [-0.12, 0.84, 0.34, 0.34, 1],
  ],
  [
    [-0.1, 0.88, 0.5, 0.14, 0],
    [0.8, -0.08, 0.32, 0.32, 1],
  ],
  [
    [0.62, 0.9, 0.5, 0.14, 0],
    [-0.14, -0.06, 0.34, 0.34, 1],
  ],
];

// A wide frame's text and captions reach nearer its edges, so its shapes sit further into the corners: slabs above the
// graphic or below the captions, and discs mostly off the frame.
const EDITORIAL_WIDE = [
  [
    [-0.1, -0.06, 0.38, 0.15, 0],
    [0.856, 0.95, 0.3, 0.3, 1],
  ],
  [
    [0.72, -0.06, 0.38, 0.15, 0],
    [-0.024, 0.95, 0.3, 0.3, 1],
  ],
  [
    [-0.1, 0.955, 0.42, 0.1, 0],
    [0.856, -0.19, 0.3, 0.3, 1],
  ],
  [
    [0.62, 0.955, 0.42, 0.1, 0],
    [-0.024, -0.19, 0.3, 0.3, 1],
  ],
];

const Editorial: React.FC<BeatProps> = ({ beat, beatFrame }) => {
  const look = useLook();
  const { width, height } = useVideoConfig();
  const change = useChange(beat, beatFrame);
  const layouts = height > width ? EDITORIAL_LAYOUTS : EDITORIAL_WIDE;
  const before = layouts[Math.max(0, beat - 1) % layouts.length];
  const after = layouts[beat % layouts.length];
  const colours = [look.accent, look.accent3, look.accent2];
  const side = Math.min(width, height);
  return (
    <AbsoluteFill style={{ backgroundColor: look.ground, overflow: "hidden" }}>
      {after.map((shape, i) => {
        const [x, y, w, h, round] = before[i].map(
          (v, k) => v + (shape[k] - v) * change,
        );
        const disc = round > 0.5;
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              left: x * width,
              top: y * height,
              width: disc ? w * side : w * width,
              height: disc ? w * side : h * height,
              borderRadius: disc ? "50%" : 0,
              backgroundColor: colours[i],
            }}
          />
        );
      })}
      <Grain opacity={0.45} blend="overlay" />
      <Grain opacity={0.12} />
    </AbsoluteFill>
  );
};

// The backdrop for the clip's look. `beat` is the number of the scene on screen and `beatFrame` how far into it we are.
export const Backdrop: React.FC<BeatProps> = (props) => {
  const look = useLook();
  switch (look.name) {
    case "paper":
      return <Paper {...props} />;
    case "engraving":
      return <Engraving {...props} />;
    case "editorial":
      return <Editorial {...props} />;
  }
};
