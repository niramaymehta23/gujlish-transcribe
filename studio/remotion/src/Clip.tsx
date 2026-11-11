import { Audio, Video } from "@remotion/media";
import React from "react";
import {
  AbsoluteFill,
  interpolate,
  Sequence,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { Accents } from "./accents";
import { Backdrop } from "./backdrops";
import { fontStyle, LOOKS } from "./looks";
import { Captions, clamp, OUT } from "./parts";
import { GraphicView } from "./scenes";
import {
  frameGeometry,
  LookContext,
  PicturesContext,
  SplitContext,
  useLook,
  type Rect,
} from "./theme";
import type { CaptionPage, ClipProps, Scene } from "./types";

const MOVE_FRAMES = 12; // how long the video takes to move between full screen, its part of a split and hidden

type Place = "full" | "half" | "hidden";

const placeOf = (scene: Scene | undefined, hasVideo: boolean): Place => {
  if (!scene || !hasVideo) return "hidden";
  if (scene.type === "speaker") return "full";
  return scene.layout === "split" ? "half" : "hidden";
};

const mix = (a: Rect, b: Rect, t: number): Rect => ({
  x: a.x + (b.x - a.x) * t,
  y: a.y + (b.y - a.y) * t,
  w: a.w + (b.w - a.w) * t,
  h: a.h + (b.h - a.h) * t,
});

// The speaker's video: one unbroken stream for the whole clip that glides between full screen, its part of a split
// scene and out of frame as the scenes change. In a split scene each look frames it in its own way. It is muted: the
// clip's one audio track carries the sound, so it can't drift.
const SpeakerVideo: React.FC<{
  scenes: Scene[];
  index: number;
  sceneFrame: number;
}> = ({ scenes, index, sceneFrame }) => {
  const look = useLook();
  const { width, height, fps } = useVideoConfig();
  const g = frameGeometry(width, height);
  const rects = { full: g.stage, half: g.half, hidden: g.hidden };
  const scene = scenes[index];
  const now = placeOf(scene, true);
  const before = index > 0 ? placeOf(scenes[index - 1], true) : now;
  if (before === "hidden" && now === "hidden") return null;
  const t = interpolate(sceneFrame, [0, MOVE_FRAMES], [0, 1], {
    ...clamp,
    easing: OUT,
  });
  const rect = mix(rects[before], rects[now], t);
  // How much of the split-scene framing shows, and how full-screen the video is (for the caption scrim).
  const framed = (before === "half" ? 1 - t : 0) + (now === "half" ? t : 0);
  const fullness = (before === "full" ? 1 - t : 0) + (now === "full" ? t : 0);
  // Where his face sits across the frame: the nearest speaker scene's setting.
  const nearest =
    [...scenes.slice(0, index + 1)]
      .reverse()
      .find((s) => s.type === "speaker") ??
    scenes.find((s) => s.type === "speaker");
  const focus =
    nearest && nearest.type === "speaker" ? (nearest.focus ?? 0.5) : 0.5;
  // A slow push-in while he speaks full screen.
  const seconds = scene.to - scene.from;
  const zoom =
    now === "full"
      ? interpolate(sceneFrame, [0, seconds * fps], [1.0, 1.03], clamp)
      : 1.0;
  const u = g.unit;
  const inset =
    {
      paper: 44,
      engraving: 64,
      editorial: 0,
    }[look.name] *
    u *
    framed;
  const top = g.vertical
    ? inset +
      (look.name === "paper" || look.name === "engraving"
        ? 150 * u * framed
        : 0)
    : inset;
  const frameStyle: React.CSSProperties = {
    paper: {
      border: `${12 * u * framed}px solid #fffaf1`,
      boxShadow: `0 ${18 * u}px ${40 * u}px rgba(42,34,24,${0.3 * framed})`,
      rotate: `${-1.5 * framed}deg`,
    },
    engraving: {
      border: `${3 * u * framed}px solid ${look.ink}`,
      outline: `${1.5 * u * framed}px solid ${look.ink}`,
      outlineOffset: 10 * u * framed,
      filter: `sepia(${0.18 * framed})`,
    },
    editorial: {
      borderRadius: g.vertical
        ? `0 0 ${80 * u * framed}px ${80 * u * framed}px`
        : `0 ${80 * u * framed}px ${80 * u * framed}px 0`,
    },
  }[look.name];
  // In a Short, the band's top and bottom edges fade into the blurred copy while he is full screen.
  const fade = g.vertical ? fullness : 0;

  return (
    <>
      {g.vertical && fullness > 0 ? (
        <AbsoluteFill style={{ opacity: fullness, overflow: "hidden" }}>
          <Video
            src={staticFile("video.mp4")}
            muted
            objectFit="cover"
            style={{
              width: "100%",
              height: "100%",
              objectPosition: `${focus * 100}% 30%`,
              scale: "1.2",
              filter: "blur(36px) brightness(0.5) saturate(1.15)",
            }}
          />
        </AbsoluteFill>
      ) : null}
      <div
        style={{
          position: "absolute",
          left: rect.x + inset,
          top: rect.y + top,
          width: rect.w - inset * 2,
          height: rect.h - top - inset,
          overflow: "hidden",
          backgroundColor: fade > 0 ? "transparent" : "#000",
          borderRadius: 34 * u * fade,
          boxShadow:
            fade > 0
              ? `0 ${24 * u}px ${70 * u}px rgba(0,0,0,${0.5 * fade})`
              : undefined,
          ...frameStyle,
        }}
      >
        <Video
          src={staticFile("video.mp4")}
          muted
          objectFit="cover"
          style={{
            width: "100%",
            height: "100%",
            objectPosition: `${focus * 100}% 30%`,
            scale: `${zoom}`,
            transformOrigin: `${focus * 100}% 30%`,
          }}
        />
        {/* darkens the bottom of the full-screen video, so the captions read without a box */}
        <AbsoluteFill
          style={{
            background:
              "linear-gradient(180deg, rgba(0,0,0,0.25) 0%, transparent 16%, transparent 52%, rgba(0,0,0,0.7) 100%)",
            opacity: fullness,
          }}
        />
        {look.name === "paper" && framed > 0
          ? [-1, 1].map((side) => (
              <div
                key={side}
                style={{
                  position: "absolute",
                  top: -6 * u,
                  [side < 0 ? "left" : "right"]: 50 * u,
                  width: 140 * u,
                  height: 42 * u,
                  backgroundColor: "rgba(231,176,74,0.8)",
                  rotate: `${side * 7}deg`,
                  opacity: framed,
                }}
              />
            ))
          : null}
      </div>
    </>
  );
};

// The speaker's name over the full-screen video.
const SpeakerLabel: React.FC<{ label: string }> = ({ label }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const g = frameGeometry(width, height);
  const show = interpolate(frame, [8, 20], [0, 1], { ...clamp, easing: OUT });
  return (
    <div
      style={{
        position: "absolute",
        left: 0,
        right: 0,
        top: (g.vertical ? 140 : 110) * g.unit,
        display: "flex",
        justifyContent: "center",
        opacity: show,
        translate: `0px ${(1 - show) * 20 * g.unit}px`,
      }}
    >
      <div
        style={{
          ...fontStyle(look.body),
          fontSize: 32 * g.unit,
          color: "#ffffff",
          textShadow: "0 2px 12px rgba(0,0,0,0.6)",
          display: "flex",
          alignItems: "center",
          gap: 14 * g.unit,
        }}
      >
        <span
          style={{
            width: 34 * g.unit,
            height: 3 * g.unit,
            backgroundColor: look.hotOnVideo,
          }}
        />
        {label}
        <span
          style={{
            width: 34 * g.unit,
            height: 3 * g.unit,
            backgroundColor: look.hotOnVideo,
          }}
        />
      </div>
    </div>
  );
};

const wordsOf = (text: string) =>
  text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s'’-]/gu, " ")
    .split(/\s+/)
    .filter(Boolean);

// Whether the graphic on screen already shows the words being said, so the captions would only repeat it.
const repeats = (scene: Scene, captions: CaptionPage[], now: number) => {
  if (scene.type === "speaker") return false;
  const page = captions.find((p) => now >= p.from && now < p.to);
  if (!page) return false;
  const shown = new Set(
    wordsOf(
      [
        "headline" in scene ? scene.headline : "",
        "text" in scene ? scene.text : "",
        "title" in scene ? scene.title : "",
        "sub" in scene ? (scene.sub ?? "") : "",
        ...("rows" in scene
          ? scene.rows.flatMap((r) => [r.left, r.right])
          : []),
        ...("items" in scene ? scene.items.map((item) => item.text) : []),
        ...("lines" in scene ? scene.lines.map((line) => line.text) : []),
        ...("chips" in scene
          ? (scene.chips ?? []).map((chip) => chip.text)
          : []),
        "kicker" in scene ? (scene.kicker ?? "") : "",
        "stamp" in scene ? (scene.stamp ?? "") : "",
      ].join(" "),
    ),
  );
  const said = wordsOf(page.text);
  return (
    said.length > 0 &&
    said.filter((w) => shown.has(w)).length / said.length >= 0.75
  );
};

const ClipBody: React.FC<ClipProps> = ({
  scenes,
  captions,
  hasAudio,
  hasVideo,
}) => {
  const { fps, width, height } = useVideoConfig();
  const vertical = height > width;
  const frame = useCurrentFrame();
  const now = frame / fps;
  let index = scenes.findIndex((scene) => now >= scene.from && now < scene.to);
  if (index < 0) index = scenes.length - 1;
  const active = scenes[index];
  const sceneFrame = frame - Math.round(active.from * fps);
  const place = placeOf(active, hasVideo);
  const index_of = (scene: Scene) => scenes.indexOf(scene);
  return (
    <AbsoluteFill>
      <Backdrop beat={index} beatFrame={sceneFrame} />
      {hasVideo ? (
        <SpeakerVideo scenes={scenes} index={index} sceneFrame={sceneFrame} />
      ) : null}
      {scenes.map((scene) => {
        const from = Math.round(scene.from * fps);
        const frames = Math.round(scene.to * fps) - from;
        return (
          <Sequence
            key={`${scene.type}-${scene.from}`}
            from={from}
            durationInFrames={frames}
            name={`${scene.type} ${scene.from}s`}
          >
            {scene.type === "speaker" ? (
              scene.label ? (
                <SpeakerLabel label={scene.label} />
              ) : null
            ) : (
              <SplitContext.Provider
                value={hasVideo && scene.layout === "split"}
              >
                {scene.type === "picture" ? null : (
                  <Accents
                    beat={index_of(scene)}
                    frames={frames}
                    split={hasVideo && scene.layout === "split"}
                  />
                )}
                <GraphicView scene={scene} frames={frames} />
              </SplitContext.Provider>
            )}
          </Sequence>
        );
      })}
      {repeats(active, captions, now) ? null : (
        <Captions
          pages={captions}
          onVideo={
            place === "full" ||
            (place === "half" && !vertical)
          }
          underVideo={place === "half" && !vertical}
        />
      )}
      {hasAudio ? <Audio src={staticFile("audio.m4a")} /> : null}
    </AbsoluteFill>
  );
};

// One finished video: the look's backdrop, the speaker's video where the storyboard uses it, one graphic at a time,
// captions in one place low in the frame, and one unbroken audio track.
export const Clip: React.FC<ClipProps> = (props) => (
  <LookContext.Provider value={LOOKS[props.style] ?? LOOKS.paper}>
    <PicturesContext.Provider value={props.pictures ?? []}>
      <ClipBody {...props} />
    </PicturesContext.Provider>
  </LookContext.Provider>
);
