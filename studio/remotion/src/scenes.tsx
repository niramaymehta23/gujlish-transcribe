import React from "react";
import {
  AbsoluteFill,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { fontStyle } from "./looks";
import { Picture as PictureMoment } from "./picture";
import {
  Art,
  artSpan,
  Badge,
  clamp,
  OUT,
  Pop,
  revealFrames,
  Rise,
  Stage,
  Tag,
  usePop,
  Words,
} from "./parts";
import { PicturesContext, sizeFor, useLayout, useLook } from "./theme";
import type {
  AgnaScene,
  CompareScene,
  EndScene,
  GraphicScene,
  HookScene,
  ListScene,
  PunchScene,
  QuoteScene,
  StoryScene,
} from "./types";

type Props<S> = { scene: S; frames: number };

// The scene's picture: big stickers when it has `art` and fills the frame, else nothing.
const Picture: React.FC<{
  art?: string[];
  artSize: number;
  gap: number;
}> = ({ art, artSize, gap }) => {
  const { unit, split, vertical, full, pad } = useLayout();
  if (art && art.length && !split) {
    // The cluster never grows past the room it has, so no sticker is cut off by the frame's edge.
    const [spanX, spanY] = artSpan(art.length, !vertical);
    if (!vertical) {
      // In a wide frame the stickers stand in the right third, beside the text (see Stage's `aside`).
      return (
        <div
          style={{
            position: "absolute",
            top: 0,
            bottom: 0,
            right: pad.right * 0.6,
            width: full.w * 0.34,
            display: "flex",
            alignItems: "center",
          }}
        >
          <Art
            names={art}
            size={Math.min(
              artSize * 1.15 * unit,
              (full.w * 0.34) / spanX,
              (full.h * 0.7) / spanY,
            )}
          />
        </div>
      );
    }
    return (
      <div style={{ width: "100%", marginBottom: gap * unit }}>
        <Art
          names={art}
          size={Math.min(
            artSize * unit,
            (full.w - pad.left - pad.right) / spanX,
          )}
        />
      </div>
    );
  }
  // No small icon instead: a lone badge beside a headline read as a stray mark.
  return null;
};

// A thin rule between rows, drawn across as its row appears.
const Hairline: React.FC<{ at: number }> = ({ at }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { unit } = useLayout();
  return (
    <div
      style={{
        height: Math.max(1, 2 * unit),
        width: "100%",
        backgroundColor: look.soft,
        opacity: 0.3,
        transformOrigin: "left",
        scale: `${interpolate(frame, [at, at + 14], [0, 1], { ...clamp, easing: OUT })} 1`,
      }}
    />
  );
};

const Heading: React.FC<{ text: string; size: number }> = ({ text, size }) => {
  const look = useLook();
  return (
    <div
      style={{
        ...fontStyle(look.display),
        fontSize: size,
        lineHeight: 1.05,
        textWrap: "balance",
        color: look.ink,
        textShadow: look.glow?.(look.accent),
      }}
    >
      {text}
    </div>
  );
};

const Hook: React.FC<Props<HookScene>> = ({ scene, frames }) => {
  const { unit } = useLayout();
  return (
    <Stage frames={frames} aside={!!scene.art?.length}>
      <Picture
        art={scene.art}
        artSize={340}
        gap={44}
      />
      {scene.kicker ? (
        <Rise at={0} style={{ marginBottom: 30 * unit }}>
          <Tag text={scene.kicker} size={30 * unit} />
        </Rise>
      ) : null}
      <Words
        text={scene.headline}
        emphasis={scene.emphasis}
        perWord={3}
        start={5}
        size={sizeFor(scene.headline, 136, 112, 92) * unit}
        strip
      />
    </Stage>
  );
};

// One idea in a few words, as big as the frame allows: the quick beats between longer scenes.
const Punch: React.FC<Props<PunchScene>> = ({ scene, frames }) => {
  const look = useLook();
  const { unit } = useLayout();
  return (
    <Stage frames={frames} align="center" aside={!!scene.art?.length}>
      <Picture
        art={scene.art}
        artSize={420}
        gap={46}
      />
      <Words
        text={scene.text}
        perWord={5}
        start={3}
        size={170 * unit}
        align="center"
        lineHeight={0.98}
        strip
      />
      {scene.sub ? (
        <Rise at={12} style={{ marginTop: 36 * unit }}>
          <div
            style={{
              ...fontStyle(look.flair),
              fontSize: 58 * unit * (look.flair.scale ?? 1),
              color: look.accent,
              textAlign: "center",
              textShadow: look.glow?.(look.accent),
            }}
          >
            {scene.sub}
          </div>
        </Rise>
      ) : null}
    </Stage>
  );
};

const Quote: React.FC<Props<QuoteScene>> = ({ scene, frames }) => {
  const look = useLook();
  const { unit, split, vertical } = useLayout();
  // With no picture beside it, a quote in a wide frame is set in the middle, not hugging the left with the right empty.
  const centred = !vertical && !split && !scene.art?.length;
  const words = scene.text.split(/\s+/).length;
  const perWord = Math.max(2, Math.min(5, (frames * 0.4) / words));
  const mark = usePop(0, 120);
  return (
    <Stage
      frames={frames}
      aside={!!scene.art?.length}
      align={centred ? "center" : "flex-start"}
    >
      {scene.art && scene.art.length && !split ? (
        <Picture art={scene.art} artSize={380} gap={30} />
      ) : null}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 28 * unit,
          marginBottom: 26 * unit,
        }}
      >
        <div
          style={{
            fontFamily: "Georgia, serif",
            fontWeight: 700,
            fontSize: 220 * unit,
            lineHeight: 1,
            height: 110 * unit,
            color: look.accent,
            textShadow: look.glow?.(look.accent),
            scale: `${mark}`,
            transformOrigin: "left center",
          }}
        >
          “
        </div>
      </div>
      <Words
        text={scene.text}
        emphasis={scene.emphasis}
        perWord={perWord}
        start={4}
        size={sizeFor(scene.text, 112, 96, 80) * unit}
        lineHeight={1.1}
        align={centred ? "center" : "left"}
      />
    </Stage>
  );
};

// An analogy as a clean table: what is on the left leads to what is on the right, row by row, and the last row — the
// point — is set in the accent colour.
const Compare: React.FC<Props<CompareScene>> = ({ scene, frames }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { unit, split, full, pad } = useLayout();
  const pictures = React.useContext(PicturesContext);
  const reveals = revealFrames(scene.rows, scene.from, frames, fps);
  const widest = Math.max(
    ...scene.rows.flatMap((row) => [row.left.length, row.right.length]),
  );
  // Fewer rows get bigger type; only long phrases make it smaller.
  const rows = scene.rows.length;
  // Generated pictures need room to be recognised; plain icon badges can be smaller.
  const marks = scene.rows.flatMap((row) => [row.left_icon, row.icon]);
  const drawn = marks.some((name) => name && pictures.includes(name));
  const iconShare = drawn ? 1.6 : 1.3;
  const iconColumns =
    (scene.rows.some((row) => row.left_icon) ? 1 : 0) +
    (scene.rows.some((row) => row.icon) ? 1 : 0);
  // The type also has to fit the width there is: both sides keep room for about nine characters a line beside their
  // icons and the arrow, so words don't stack one per line in a narrow split.
  const room = full.w - pad.left - pad.right - 4 * 22 * unit;
  const fits = room / (iconColumns * iconShare + 1.1 + 9.5);
  const font = Math.min(
    (rows <= 2 ? 66 : rows === 3 ? 58 : 50) * (widest > 22 ? 0.88 : 1) * unit,
    fits,
  );
  const icon = font * iconShare;
  const hot = look.accent;
  // One grid for the whole table (icon, words, arrow, icon, words), so every column lines up down the rows. A side
  // with an icon on any row keeps the icon column on every row.
  const leftIcons = scene.rows.some((row) => row.left_icon);
  const rightIcons = scene.rows.some((row) => row.icon);
  // The two word columns share the width by how long their longest entries are (within 30-60% for the left), so
  // short words on one side give the other side room. Columns the table doesn't use are left out, so their gaps
  // don't squeeze the words.
  const longest = (side: "left" | "right") =>
    Math.max(...scene.rows.map((row) => row[side].length));
  const share = Math.min(
    0.6,
    Math.max(0.3, longest("left") / (longest("left") + longest("right"))),
  );
  const columns = [
    leftIcons ? `${icon}px` : null,
    `minmax(0, ${share.toFixed(3)}fr)`,
    `${font * 1.1}px`,
    rightIcons ? `${icon}px` : null,
    `minmax(0, ${(1 - share).toFixed(3)}fr)`,
  ]
    .filter(Boolean)
    .join(" ");
  // The body face for both sides: even, narrow enough to wrap well, and the same on every row.
  const words: React.CSSProperties = {
    ...fontStyle(look.body),
    fontSize: font,
    lineHeight: 1.1,
    textWrap: "balance",
  };
  return (
    <Stage frames={frames}>
      <Rise at={0} style={{ marginBottom: (split ? 20 : 34) * unit }}>
        <Heading
          text={scene.title}
          size={sizeFor(scene.title, 84, 70, 58) * (split ? 0.86 : 1) * unit}
        />
      </Rise>
      <div
        style={{
          width: "100%",
          display: "grid",
          gridTemplateColumns: columns,
          columnGap: 22 * unit,
          alignItems: "center",
        }}
      >
        {scene.rows.map((row, index) => {
          const last = index === scene.rows.length - 1;
          const at = reveals[index];
          const arrow = interpolate(frame, [at + 4, at + 12], [0, 1], {
            ...clamp,
            easing: OUT,
          });
          const cell: React.CSSProperties = {
            paddingTop: (split ? 16 : 22) * unit,
            paddingBottom: (split ? 16 : 22) * unit,
          };
          return (
            <React.Fragment key={`${row.left}-${index}`}>
              <div style={{ gridColumn: "1 / -1" }}>
                <Hairline at={at} />
              </div>
              {leftIcons ? (
                <Rise
                  at={at}
                  distance={30}
                  style={{ ...cell, display: "flex" }}
                >
                  {row.left_icon ? (
                    <Badge name={row.left_icon} size={icon} />
                  ) : null}
                </Rise>
              ) : null}
              <Rise
                at={at}
                distance={30}
                style={{ ...cell, ...words, color: look.soft }}
              >
                {row.left}
              </Rise>
              <svg
                width={font * 1.1}
                height={font * 0.6}
                viewBox="0 0 66 36"
                style={{ overflow: "visible" }}
              >
                <path
                  d="M 2 18 L 58 18 M 44 5 L 60 18 L 44 31"
                  fill="none"
                  stroke={last ? hot : look.soft}
                  strokeWidth={5}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  pathLength={1}
                  strokeDasharray={1}
                  strokeDashoffset={1 - arrow}
                />
              </svg>
              {rightIcons ? (
                <Pop
                  at={at + 8}
                  from={0.7}
                  style={{ ...cell, display: "flex" }}
                >
                  {row.icon ? (
                    <Badge name={row.icon} size={icon} hot={last} />
                  ) : null}
                </Pop>
              ) : null}
              <Pop
                at={at + 8}
                from={last ? 0.85 : 0.92}
                style={{
                  ...cell,
                  ...words,
                  transformOrigin: "left center",
                  color: last ? hot : look.ink,
                  textShadow: look.glow?.(last ? look.accent : look.accent2),
                }}
              >
                {row.right}
              </Pop>
            </React.Fragment>
          );
        })}
        <div style={{ gridColumn: "1 / -1" }}>
          <Hairline at={reveals[reveals.length - 1]} />
        </div>
      </div>
    </Stage>
  );
};

const List: React.FC<Props<ListScene>> = ({ scene, frames }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { unit } = useLayout();
  const reveals = revealFrames(scene.items, scene.from, frames, fps);
  const latest = reveals.filter((r) => r <= frame).length - 1;
  // One kind of marker for the whole list: icons only when every item has one, numbers otherwise.
  const icons = scene.items.every((item) => item.icon);
  // Fewer items get bigger type, so a short list fills its space instead of sitting small in it.
  const count = scene.items.length;
  const text = (count <= 2 ? 70 : count === 3 ? 62 : 54) * unit;
  const marker = text * 1.6;
  return (
    <Stage frames={frames}>
      <Rise at={0} style={{ marginBottom: 34 * unit }}>
        <Heading
          text={scene.title}
          size={sizeFor(scene.title, 84, 70, 58) * unit}
        />
      </Rise>
      <div style={{ width: "100%" }}>
        {scene.items.map((item, index) => (
          <div key={`${item.text}-${index}`}>
            <Hairline at={reveals[index]} />
            <Rise
              at={reveals[index]}
              distance={30}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 30 * unit,
                padding: `${22 * unit}px 0`,
              }}
            >
              {icons && item.icon ? (
                <Badge
                  name={item.icon}
                  size={marker}
                  hot={index === latest}
                />
              ) : (
                <div
                  style={{
                    ...fontStyle(look.display),
                    width: marker,
                    flexShrink: 0,
                    fontSize: text * 1.15,
                    color: index === latest ? look.accent : look.soft,
                    textShadow: look.glow?.(look.accent),
                  }}
                >
                  {String(index + 1).padStart(2, "0")}
                </div>
              )}
              <div
                style={{
                  ...fontStyle(look.body),
                  fontSize: text,
                  lineHeight: 1.12,
                  textWrap: "balance",
                  color: index === latest ? look.ink : look.soft,
                }}
              >
                {item.text}
              </div>
            </Rise>
          </div>
        ))}
        <Hairline at={reveals[reveals.length - 1]} />
      </div>
    </Stage>
  );
};

const Agna: React.FC<Props<AgnaScene>> = ({ scene, frames }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { unit } = useLayout();
  const stamp = interpolate(frame, [0, 8], [1.6, 1], { ...clamp, easing: OUT });
  const colour = look.accent;
  return (
    <Stage frames={frames} aside={!!scene.art?.length}>
      {scene.art && scene.art.length ? (
        <Picture art={scene.art} artSize={380} gap={30} />
      ) : null}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 26 * unit,
          marginBottom: 40 * unit,
        }}
      >
        <div
          style={{
            ...fontStyle(look.label),
            fontSize: 34 * unit,
            color: colour,
            border: `${4 * unit}px solid ${colour}`,
            borderRadius: 8 * unit,
            padding: `${12 * unit}px ${24 * unit}px`,
            rotate: "-3deg",
            scale: `${stamp}`,
            opacity: interpolate(frame, [0, 4], [0, 1], clamp),
            textShadow: look.glow?.(colour),
          }}
        >
          {(scene.label ?? "Agna").toUpperCase()}
        </div>
      </div>
      <Words
        text={scene.text}
        emphasis={scene.emphasis}
        perWord={3}
        start={10}
        size={sizeFor(scene.text, 120, 100, 82) * unit}
      />
    </Stage>
  );
};

const Story: React.FC<Props<StoryScene>> = ({ scene, frames }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { unit } = useLayout();
  const reveals = revealFrames(scene.lines, scene.from, frames, fps);
  const node = 92 * unit;
  const shown = reveals.filter((r) => r <= frame).length;
  // One kind of marker down the timeline: icons only when every line has one, dots otherwise.
  const icons = scene.lines.every((line) => line.icon);
  return (
    <Stage frames={frames}>
      {scene.place ? (
        <Rise at={0} style={{ marginBottom: 40 * unit }}>
          <Tag text={scene.place} size={30 * unit} />
        </Rise>
      ) : null}
      <div style={{ position: "relative", paddingLeft: node + 34 * unit }}>
        <div
          style={{
            position: "absolute",
            left: node / 2 - 2 * unit,
            top: node / 2,
            bottom: node / 2,
            width: 4 * unit,
            backgroundColor: look.soft,
            opacity: 0.6,
            transformOrigin: "top",
            scale: `1 ${interpolate(frame, [0, frames * 0.75], [0, 1], clamp)}`,
          }}
        />
        {scene.lines.map((line, index) => {
          const current = index === shown - 1;
          return (
            <Rise
              key={`${line.text}-${index}`}
              at={reveals[index]}
              style={{
                position: "relative",
                minHeight: node,
                display: "flex",
                alignItems: "center",
                marginBottom: 36 * unit,
              }}
            >
              <div style={{ position: "absolute", left: -node - 34 * unit }}>
                {icons && line.icon ? (
                  <Badge name={line.icon} size={node} hot={current} />
                ) : (
                  <div
                    style={{
                      width: node,
                      height: node,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}
                  >
                    <div
                      style={{
                        width: node * 0.32,
                        height: node * 0.32,
                        borderRadius: "50%",
                        backgroundColor: current ? look.accent : look.soft,
                      }}
                    />
                  </div>
                )}
              </div>
              <div
                style={{
                  ...fontStyle(look.display),
                  textTransform: "none",
                  letterSpacing: "-0.02em",
                  fontSize: (scene.lines.length > 3 ? 58 : 70) * unit,
                  lineHeight: 1.08,
                  textWrap: "balance",
                  color: current ? look.ink : look.soft,
                  textShadow: current ? look.glow?.(look.accent) : undefined,
                }}
              >
                {line.text}
              </div>
            </Rise>
          );
        })}
      </div>
    </Stage>
  );
};

// The closing card: the line to remember, with rings rippling out behind it.
const End: React.FC<Props<EndScene>> = ({ scene, frames }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { unit, vertical, full } = useLayout();
  const ring = look.accent;
  return (
    <>
      <AbsoluteFill
        style={{
          alignItems: "center",
          justifyContent: "center",
          top: vertical ? -180 * unit : 0,
        }}
      >
        {[0, 1, 2].map((i) => {
          const t = ((frame + i * 16) % 48) / 48;
          return (
            <div
              key={i}
              style={{
                position: "absolute",
                width: full.w * (0.3 + t * 0.9),
                height: full.w * (0.3 + t * 0.9),
                borderRadius: "50%",
                border: `${3 * unit}px solid ${ring}`,
                opacity: (1 - t) * 0.45,
                boxShadow: look.glow ? `0 0 24px ${ring}` : undefined,
              }}
            />
          );
        })}
      </AbsoluteFill>
      <Stage frames={frames + 6} align="center" aside={!!scene.art?.length}>
        <Picture
          art={scene.art}
          artSize={400}
          gap={40}
        />
        <Words
          text={scene.text}
          perWord={3}
          start={4}
          size={sizeFor(scene.text, 128, 104, 86) * unit}
          align="center"
        />
        {scene.sub ? (
          <Rise at={14} style={{ marginTop: 26 * unit }}>
            <div
              style={{
                ...fontStyle(look.flair),
                fontSize: 54 * unit * (look.flair.scale ?? 1),
                lineHeight: 1.1,
                color: look.accent,
                textAlign: "center",
                textShadow: look.glow?.(look.accent),
              }}
            >
              {scene.sub}
            </div>
          </Rise>
        ) : null}
      </Stage>
    </>
  );
};

export const GraphicView: React.FC<{ scene: GraphicScene; frames: number }> = ({
  scene,
  frames,
}) => {
  switch (scene.type) {
    case "picture":
      return <PictureMoment scene={scene} frames={frames} />;
    case "hook":
      return <Hook scene={scene} frames={frames} />;
    case "punch":
      return <Punch scene={scene} frames={frames} />;
    case "quote":
      return <Quote scene={scene} frames={frames} />;
    case "compare":
      return <Compare scene={scene} frames={frames} />;
    case "list":
      return <List scene={scene} frames={frames} />;
    case "agna":
      return <Agna scene={scene} frames={frames} />;
    case "story":
      return <Story scene={scene} frames={frames} />;
    case "end":
      return <End scene={scene} frames={frames} />;
  }
};
