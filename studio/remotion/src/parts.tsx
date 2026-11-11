import * as Phosphor from "@phosphor-icons/react";
import React from "react";
import {
  AbsoluteFill,
  Easing,
  Img,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { fontStyle } from "./looks";
import { PicturesContext, useLayout, useLook } from "./theme";
import type { CaptionPage } from "./types";

export const OUT = Easing.bezier(0.16, 1, 0.3, 1);
export const clamp = {
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
} as const;

// A springy 0 → 1 (with a little overshoot) starting at frame `at`.
export const usePop = (at: number, stiffness = 170) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  return spring({
    frame: frame - at,
    fps,
    config: { damping: 13, stiffness, mass: 0.6 },
  });
};

// A graphic's frame: padded into its safe area and centred in it, sliding up into place and out again quickly.
export const Stage: React.FC<{
  frames: number;
  align?: "center" | "flex-start";
  aside?: boolean; // keep the right third of a wide frame free for the scene's stickers
  children: React.ReactNode;
}> = ({ frames, align = "flex-start", aside = false, children }) => {
  const frame = useCurrentFrame();
  const { pad, unit, vertical, split, full } = useLayout();
  const room = aside && !vertical && !split ? full.w * 0.34 : 0;
  const enter = interpolate(frame, [0, 10], [1, 0], { ...clamp, easing: OUT });
  const leave = interpolate(frame, [frames - 6, frames], [0, 1], clamp);
  return (
    <AbsoluteFill
      style={{
        paddingTop: pad.top,
        paddingBottom: pad.bottom,
        paddingLeft: pad.left,
        paddingRight: pad.right + room,
        justifyContent: "center",
        alignItems: align,
        opacity: 1 - Math.max(enter * 0.9, leave),
        translate: `0px ${(enter * 70 - leave * 40) * unit}px`,
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
}> = ({ at, distance = 50, style, children }) => {
  const frame = useCurrentFrame();
  const { unit } = useLayout();
  return (
    <div
      style={{
        ...style,
        opacity: interpolate(frame, [at, at + 7], [0, 1], clamp),
        translate: `0px ${interpolate(frame, [at, at + 14], [distance * unit, 0], { ...clamp, easing: OUT })}px`,
      }}
    >
      {children}
    </div>
  );
};

// Springs into place from small at frame `at`, for icons and badges.
export const Pop: React.FC<{
  at: number;
  from?: number;
  turn?: number;
  style?: React.CSSProperties;
  children: React.ReactNode;
}> = ({ at, from = 0.5, turn = 0, style, children }) => {
  const p = usePop(at);
  return (
    <div
      style={{
        ...style,
        opacity: Math.min(1, Math.max(0, p * 3)),
        scale: `${from + (1 - from) * p}`,
        rotate: `${turn * (1 - p)}deg`,
      }}
    >
      {children}
    </div>
  );
};

const clean = (word: string) =>
  word.replace(/[^\p{L}\p{N}'’-]/gu, "").toLowerCase();

// Which words of `text` belong to an emphasised phrase, matching whole phrases in order.
export const emphasisMask = (text: string, emphasis: string[] = []) => {
  const words = text.split(/\s+/).map(clean);
  const mask = words.map(() => false);
  for (const phrase of emphasis) {
    const parts = phrase.split(/\s+/).map(clean).filter(Boolean);
    for (let i = 0; i + parts.length <= words.length; i++) {
      if (parts.every((part, j) => words[i + j] === part)) {
        parts.forEach((_, j) => (mask[i + j] = true));
      }
    }
  }
  return mask;
};

// Kinetic type: words land one at a time with a little scale and lift; emphasised words switch to the look's accent
// face and colour.
export const Words: React.FC<{
  text: string;
  emphasis?: string[];
  perWord: number;
  start?: number;
  size: number;
  colour?: string;
  align?: "left" | "center";
  lineHeight?: number;
  strip?: boolean; // set each line on a cut-paper highlight strip, in the paper and editorial looks
}> = ({
  text,
  emphasis,
  perWord,
  start = 0,
  size,
  colour,
  align = "left",
  lineHeight = 1.04,
  strip = false,
}) => {
  const frame = useCurrentFrame();
  const look = useLook();
  const { full, pad } = useLayout();
  const mask = emphasisMask(text, emphasis);
  // Shrink if the longest word wouldn't fit across the room the graphic has.
  const room = full.w - pad.left - pad.right;
  const longest = Math.max(...text.split(/\s+/).map((word) => word.length));
  const fitted = Math.min(
    size,
    (room * 0.95) / (longest * (look.display.wide ?? 0.7)),
  );
  const strips = strip && (look.name === "paper" || look.name === "editorial");
  const stripFill = look.name === "paper" ? look.accent3 : look.ink;
  const stripInk = look.name === "paper" ? look.ink : look.ground;
  const hotColour = look.accent;
  const wipe = interpolate(frame, [start, start + 10], [0, 100], {
    ...clamp,
    easing: OUT,
  });
  const words = text.split(/\s+/).map((word, index) => {
    const at = start + index * perWord;
    const hot = mask[index];
    const land = interpolate(frame, [at, at + 8], [0, 1], {
      ...clamp,
      easing: OUT,
    });
    return (
      <span
        key={`${word}-${index}`}
        style={{
          display: "inline-block",
          marginRight: "0.22em",
          opacity: interpolate(frame, [at, at + 4], [0, 1], clamp),
          scale: `${1.3 - 0.3 * land}`,
          translate: `0px ${(1 - land) * 0.3 * fitted}px`,
          ...(hot
            ? {
                ...fontStyle(look.flair),
                color: hotColour,
                fontSize: `${look.flair.scale ?? 1}em`,
                lineHeight: 0.9,
                textShadow: look.glow?.(look.accent2),
              }
            : {}),
        }}
      >
        {word}
      </span>
    );
  });
  return (
    <div
      style={{
        ...fontStyle(look.display),
        fontSize: strips ? fitted * 0.86 : fitted,
        lineHeight: strips ? 1.32 : lineHeight,
        color: strips ? stripInk : (colour ?? look.ink),
        textAlign: align,
        textWrap: "balance", // even lines, never one word left alone on the last
        textShadow: strips ? undefined : look.glow?.(look.accent),
        rotate: strips ? "-1.5deg" : undefined,
      }}
    >
      {strips ? (
        // One strip per line: the background is cloned onto every line the words wrap to, and wipes in.
        <span
          style={{
            backgroundImage: `linear-gradient(${stripFill}, ${stripFill})`,
            backgroundRepeat: "no-repeat",
            backgroundSize: `${wipe}% 100%`,
            boxDecorationBreak: "clone",
            WebkitBoxDecorationBreak: "clone",
            padding: "0.02em 0.16em 0.06em",
            filter:
              look.name === "paper"
                ? "drop-shadow(0 6px 6px rgba(42,34,24,0.25))"
                : "drop-shadow(6px 6px 0 rgba(0,0,0,0.25))",
          }}
        >
          {words}
        </span>
      ) : (
        words
      )}
    </div>
  );
};

const pascal = (name: string) =>
  name
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join("");

// A Phosphor icon by its kebab-case name, such as "hands-praying". tools/studio.py checks the names.
export const Icon: React.FC<{
  name: string;
  size: number;
  colour: string;
  weight?: Phosphor.IconWeight;
}> = ({ name, size, colour, weight }) => {
  const look = useLook();
  const Component = (Phosphor as unknown as Record<string, Phosphor.Icon>)[
    pascal(name)
  ];
  if (!Component) {
    throw new Error(`There is no icon called "${name}".`);
  }
  return <Component size={size} color={colour} weight={weight ?? look.icon} />;
};

// A generated illustration (already a die-cut sticker with its own white edge), with a soft shadow in the look's manner.
const Illustration: React.FC<{ name: string; size: number }> = ({
  name,
  size,
}) => {
  const look = useLook();
  const shadow =
    look.name === "editorial"
      ? `drop-shadow(${size * 0.03}px ${size * 0.04}px 0 rgba(0,0,0,0.2))`
      : `drop-shadow(0 ${size * 0.03}px ${size * 0.05}px rgba(0,0,0,0.3))`;
  return (
    <Img
      src={staticFile(`pictures/${name}.png`)}
      style={{
        width: size * 1.15,
        height: size * 1.15,
        margin: -size * 0.075,
        objectFit: "contain",
        filter: shadow,
        display: "block",
      }}
    />
  );
};

// One icon drawn as an illustration in the look's manner: a die-cut sticker with a thick edge and a shadow on paper and
// editorial, and an inked print with a hard shadow on engraving.
export const Sticker: React.FC<{
  name: string;
  size: number;
  colour: string;
}> = ({ name, size, colour }) => {
  const look = useLook();
  const pictures = React.useContext(PicturesContext);
  if (pictures.includes(name)) {
    return <Illustration name={name} size={size} />;
  }
  const edge = Math.max(3, size * 0.035);
  const outline = (c: string) =>
    [
      [edge, 0],
      [-edge, 0],
      [0, edge],
      [0, -edge],
      [edge * 0.7, edge * 0.7],
      [-edge * 0.7, edge * 0.7],
      [edge * 0.7, -edge * 0.7],
      [-edge * 0.7, -edge * 0.7],
    ]
      .map(([x, y]) => `drop-shadow(${x}px ${y}px 0 ${c})`)
      .join(" ");
  const styles: Record<string, [Phosphor.IconWeight, string, string]> = {
    paper: [
      "fill",
      colour,
      `${outline("#fffaf1")} drop-shadow(0 ${size * 0.04}px ${size * 0.05}px rgba(42,34,24,0.35))`,
    ],
    editorial: [
      "fill",
      colour,
      `${outline(look.ink)} drop-shadow(${size * 0.04}px ${size * 0.05}px 0 rgba(0,0,0,0.3))`,
    ],
    engraving: [
      "duotone",
      look.ink,
      `${outline(look.ground)} drop-shadow(${size * 0.035}px ${size * 0.035}px 0 ${colour})`,
    ],
  };
  const [weight, ink, filter] = styles[look.name];
  return (
    <div style={{ filter, lineHeight: 0 }}>
      <Icon name={name} size={size} colour={ink} weight={weight} />
    </div>
  );
};

type Spot = { x: number; y: number; scale: number; turn: number };

// A Short's cluster spreads sideways across the frame; a wide frame's stands taller, to fill the narrow, tall third
// it has beside the text.
const artSpots = (count: number, wide = false): Spot[] =>
  (wide
    ? [
        { x: count > 1 ? -0.12 : 0, y: count > 1 ? 0.2 : 0, scale: 1, turn: -7 },
        { x: 0.42, y: -0.55, scale: 0.6, turn: 11 },
        { x: -0.52, y: -0.5, scale: 0.5, turn: -13 },
      ]
    : [
        { x: count > 1 ? -0.36 : 0, y: 0.04, scale: 1, turn: -7 },
        { x: 0.74, y: -0.36, scale: 0.6, turn: 11 },
        { x: 0.7, y: 0.5, scale: 0.5, turn: -13 },
      ]
  ).slice(0, Math.max(1, Math.min(3, count)));

const artBounds = (spots: Spot[]) => ({
  left: Math.min(...spots.map((s) => s.x - s.scale / 2)),
  right: Math.max(...spots.map((s) => s.x + s.scale / 2)),
  top: Math.min(...spots.map((s) => s.y - s.scale / 2)),
  bottom: Math.max(...spots.map((s) => s.y + s.scale / 2)),
});

const artCentre = (spots: Spot[]) => {
  const b = artBounds(spots);
  return [-(b.left + b.right) / 2, -(b.top + b.bottom) / 2];
};

// How wide and tall a cluster of `count` stickers is, in sticker sizes, with a margin for floating and tilting.
export const artSpan = (count: number, wide = false) => {
  const b = artBounds(artSpots(count, wide));
  return [(b.right - b.left) * 1.15, (b.bottom - b.top) * 1.2];
};

// One to three stickers in a loose, tilted cluster that pops in and then floats, filling the space a scene leaves.
export const Art: React.FC<{
  names: string[];
  start?: number;
  size: number;
}> = ({ names, start = 4, size }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const colours = [look.accent, look.accent3, look.accent2];
  // Spaced so that floating and tilting, the stickers never touch each other, and shifted so the cluster as a whole
  // is centred in its place (see artSpan for how much room it needs).
  const { vertical } = useLayout();
  const spots = artSpots(names.length, !vertical);
  const [shiftX, shiftY] = artCentre(spots);
  return (
    <div
      style={{
        position: "relative",
        width: "100%",
        height: size * artSpan(names.length, !vertical)[1],
        display: "flex",
        justifyContent: "center",
        alignItems: "center",
      }}
    >
      {names.slice(0, 3).map((name, i) => {
        const spot = spots[i];
        const float = Math.sin(frame / 22 + i * 1.7) * size * 0.025;
        const sway = Math.sin(frame / 30 + i) * 2.5;
        return (
          <div
            key={`${name}-${i}`}
            style={{
              position: "absolute",
              left: `calc(50% + ${(spot.x + shiftX) * size}px - ${(size * spot.scale) / 2}px)`,
              top: `calc(50% + ${(spot.y + shiftY) * size + float}px - ${(size * spot.scale) / 2}px)`,
              rotate: `${sway}deg`,
              zIndex: 3 - i,
            }}
          >
            <Pop at={start + i * 5} from={0.2} turn={spot.turn * 3}>
              <div style={{ rotate: `${spot.turn}deg` }}>
                <Sticker
                  name={name}
                  size={size * spot.scale}
                  colour={colours[i % 3]}
                />
              </div>
            </Pop>
          </div>
        );
      })}
    </div>
  );
};

// An icon on a round badge, drawn in the look's manner. `hot` marks the one that matters most.
export const Badge: React.FC<{
  name: string;
  size: number;
  hot?: boolean;
}> = ({ name, size, hot }) => {
  const look = useLook();
  const pictures = React.useContext(PicturesContext);
  if (pictures.includes(name)) {
    return (
      <div style={{ width: size, height: size, flexShrink: 0 }}>
        <Illustration name={name} size={size} />
      </div>
    );
  }
  const styles: Record<string, [React.CSSProperties, string]> = {
    paper: [
      {
        backgroundColor: hot ? look.accent : "#fffaf1",
        boxShadow: `0 ${size * 0.06}px ${size * 0.16}px rgba(42,34,24,0.22)`,
      },
      hot ? look.onAccent : look.accent,
    ],
    engraving: [
      {
        backgroundColor: hot ? look.accent : "transparent",
        border: `${Math.max(2, size * 0.025)}px solid ${hot ? look.accent : look.ink}`,
        outline: `${Math.max(1, size * 0.012)}px solid ${hot ? look.accent : look.ink}`,
        outlineOffset: size * 0.05,
      },
      hot ? look.onAccent : look.ink,
    ],
    editorial: [
      { backgroundColor: hot ? look.accent : look.accent3 },
      look.onAccent,
    ],
  };
  const [style, ink] = styles[look.name];
  return (
    <div
      style={{
        width: size,
        height: size,
        flexShrink: 0,
        borderRadius: "50%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        ...style,
      }}
    >
      <Icon name={name} size={size * 0.54} colour={ink} />
    </div>
  );
};

// A small label: a kicker over a headline, a place over a story.
export const Tag: React.FC<{ text: string; size: number }> = ({
  text,
  size,
}) => {
  const look = useLook();
  const rule = (
    <span
      style={{
        display: "inline-block",
        width: size * 1.6,
        height: Math.max(2, size * 0.08),
        backgroundColor: look.accent,
        boxShadow: look.glow?.(look.accent2),
      }}
    />
  );
  return (
    <span
      style={{
        ...fontStyle(look.label),
        display: "inline-flex",
        alignItems: "center",
        gap: size * 0.5,
        fontSize: size,
        lineHeight: 1,
        color: look.accent,
        textShadow: look.glow?.(look.accent2),
      }}
    >
      {rule}
      {text}
    </span>
  );
};

// When each item of a list appears: at the time given in the storyboard, or spread evenly over the first 60%.
export const revealFrames = (
  items: { at?: number }[],
  sceneFrom: number,
  frames: number,
  fps: number,
) =>
  items.map((item, index) =>
    item.at !== undefined
      ? Math.max(4, Math.round((item.at - sceneFrom) * fps))
      : Math.round(8 + (index / Math.max(1, items.length)) * frames * 0.6),
  );

// The English captions: always in the same place low in the frame, centred, with no box. Clean bold type with a soft
// shadow. No word-by-word highlight: the timing inside a subtitle chunk is only estimated, so a lit word drifts off the
// speech.
export const Captions: React.FC<{
  pages: CaptionPage[];
  onVideo: boolean;
  underVideo?: boolean; // a wide split scene: keep the captions under the video half, clear of the graphic
}> = ({ pages, onVideo, underVideo = false }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const look = useLook();
  const layout = useLayout();
  const now = frame / fps;
  const page = pages.find((p) => now >= p.from && now < p.to);
  if (!page) return null;
  const pageFrame = Math.round((now - page.from) * fps);
  const size = (layout.vertical ? 56 : 46) * layout.unit;
  const light = onVideo || look.dark;
  const ink = onVideo ? "#ffffff" : look.ink;
  const shadow = light
    ? `0 ${size * 0.05}px ${size * 0.28}px rgba(0,0,0,0.55), 0 0 ${size * 0.06}px rgba(0,0,0,0.5)`
    : "none";
  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: layout.captionBottom,
        paddingRight: underVideo ? layout.full.w / 2 : 0,
      }}
    >
      <div
        style={{
          ...fontStyle(look.caption),
          maxWidth: underVideo
            ? layout.half.w - 120 * layout.unit
            : layout.captionWidth,
          fontSize: size,
          lineHeight: 1.22,
          textAlign: "center",
          textWrap: "balance",
          color: ink,
          textShadow: shadow,
          opacity: interpolate(pageFrame, [0, 4], [0, 1], clamp),
          translate: `0px ${interpolate(pageFrame, [0, 6], [14, 0], { ...clamp, easing: OUT }) * layout.unit}px`,
        }}
      >
        {page.text}
      </div>
    </AbsoluteFill>
  );
};
