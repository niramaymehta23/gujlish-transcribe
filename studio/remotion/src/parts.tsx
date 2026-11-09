import React from "react";
import {
  AbsoluteFill,
  Easing,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { theme, useLayout } from "./theme";
import type { CaptionPage } from "./types";

const OUT = Easing.bezier(0.16, 1, 0.3, 1);
const clamp = {
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
} as const;

// The moving backdrop runs through the whole video, so changing scene never flashes.
export const Backdrop: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;
  const x1 = 28 + Math.sin(t / 7) * 14;
  const y1 = 22 + Math.cos(t / 9) * 10;
  const x2 = 74 + Math.cos(t / 8) * 14;
  const y2 = 76 + Math.sin(t / 6) * 10;
  return (
    <AbsoluteFill
      style={{
        backgroundColor: theme.ink,
        backgroundImage: `radial-gradient(circle at ${x1}% ${y1}%, ${theme.saffron}38 0%, transparent 46%), radial-gradient(circle at ${x2}% ${y2}%, ${theme.rose}2e 0%, transparent 50%), radial-gradient(circle at 50% 50%, ${theme.inkSoft} 0%, ${theme.ink} 78%)`,
      }}
    />
  );
};

// Fades a scene in and out over its own timeline, and centres it in the safe area.
export const Stage: React.FC<{
  frames: number;
  children: React.ReactNode;
}> = ({ frames, children }) => {
  const frame = useCurrentFrame();
  const layout = useLayout();
  return (
    <AbsoluteFill
      style={{
        paddingTop: layout.top,
        paddingBottom: layout.bottom,
        paddingLeft: layout.side,
        paddingRight: layout.side,
        justifyContent: "center",
        opacity: interpolate(
          frame,
          [0, 7, Math.max(8, frames - 7), Math.max(9, frames)],
          [0, 1, 1, 0],
          clamp,
        ),
      }}
    >
      {children}
    </AbsoluteFill>
  );
};

// Rises into place at frame `at` of the scene.
export const Rise: React.FC<{
  at: number;
  distance?: number;
  style?: React.CSSProperties;
  children: React.ReactNode;
}> = ({ at, distance = 34, style, children }) => {
  const frame = useCurrentFrame();
  const { unit } = useLayout();
  return (
    <div
      style={{
        ...style,
        opacity: interpolate(frame, [at, at + 10], [0, 1], clamp),
        translate: `0px ${interpolate(frame, [at, at + 16], [distance * unit, 0], { ...clamp, easing: OUT })}px`,
      }}
    >
      {children}
    </div>
  );
};

const emphasised = (word: string, emphasis: string[]) => {
  const clean = word.replace(/[^\p{L}\p{N}'’-]/gu, "").toLowerCase();
  return emphasis.some((phrase) =>
    phrase.toLowerCase().split(/\s+/).includes(clean),
  );
};

// Text that arrives word by word; `perWord` is the frames between words.
export const Words: React.FC<{
  text: string;
  emphasis?: string[];
  perWord: number;
  start?: number;
  style?: React.CSSProperties;
}> = ({ text, emphasis = [], perWord, start = 0, style }) => {
  const frame = useCurrentFrame();
  const { unit } = useLayout();
  return (
    <div style={style}>
      {text.split(/\s+/).map((word, index) => {
        const at = start + index * perWord;
        return (
          <span
            key={`${word}-${index}`}
            style={{
              display: "inline-block",
              marginRight: "0.26em",
              color: emphasised(word, emphasis) ? theme.saffron : undefined,
              opacity: interpolate(frame, [at, at + 7], [0, 1], clamp),
              translate: `0px ${interpolate(frame, [at, at + 12], [20 * unit, 0], { ...clamp, easing: OUT })}px`,
            }}
          >
            {word}
          </span>
        );
      })}
    </div>
  );
};

// When each item of a list appears: at the time given in the storyboard, or spread evenly over the first 70%.
export const revealFrames = (
  items: { at?: number }[],
  sceneFrom: number,
  frames: number,
  fps: number,
) =>
  items.map((item, index) =>
    item.at !== undefined
      ? Math.round((item.at - sceneFrom) * fps)
      : Math.round(10 + (index / Math.max(1, items.length)) * frames * 0.7),
  );

export const Captions: React.FC<{
  pages: CaptionPage[];
  opaque: boolean;
}> = ({ pages, opaque }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const layout = useLayout();
  const now = frame / fps;
  const page = pages.find((p) => now >= p.from && now < p.to);
  if (!page) return null;
  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: layout.captionBottom,
        paddingLeft: layout.side * 0.55,
        paddingRight: layout.side * 0.55,
      }}
    >
      <div
        style={{
          fontFamily: theme.body,
          fontWeight: 600,
          fontSize: (layout.vertical ? 50 : 42) * layout.unit,
          lineHeight: 1.3,
          color: theme.cream,
          textAlign: "center",
          padding: `${14 * layout.unit}px ${28 * layout.unit}px`,
          borderRadius: 20 * layout.unit,
          backgroundColor: opaque ? "rgba(10,7,18,0.78)" : "rgba(10,7,18,0.5)",
        }}
      >
        {page.text}
      </div>
    </AbsoluteFill>
  );
};

export const Brand: React.FC = () => {
  const layout = useLayout();
  return (
    <div
      style={{
        position: "absolute",
        top: (layout.vertical ? 150 : 56) * layout.unit,
        left: layout.side,
        fontFamily: theme.display,
        fontWeight: 800,
        letterSpacing: "0.18em",
        fontSize: 28 * layout.unit,
        color: theme.cream,
        opacity: 0.8,
      }}
    >
      {theme.brand}
    </div>
  );
};
