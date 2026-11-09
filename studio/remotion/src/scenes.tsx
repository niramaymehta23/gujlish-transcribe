import { Video } from "@remotion/media";
import React from "react";
import {
  AbsoluteFill,
  Easing,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { revealFrames, Rise, Stage, Words } from "./parts";
import { sizeFor, theme, useLayout } from "./theme";
import type {
  AgnaScene,
  CompareScene,
  EndScene,
  HookScene,
  ListScene,
  QuoteScene,
  Scene,
  SpeakerScene,
  StoryScene,
} from "./types";

const clamp = {
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
} as const;

const Hook: React.FC<{ scene: HookScene; frames: number }> = ({
  scene,
  frames,
}) => {
  const { unit } = useLayout();
  const size = sizeFor(scene.headline, 118, 96, 80) * unit;
  return (
    <Stage frames={frames}>
      {scene.kicker ? (
        <Rise at={0} style={{ marginBottom: 34 * unit }}>
          <span
            style={{
              fontFamily: theme.body,
              fontWeight: 600,
              fontSize: 36 * unit,
              color: theme.ink,
              backgroundColor: theme.saffron,
              padding: `${10 * unit}px ${24 * unit}px`,
              borderRadius: 999,
            }}
          >
            {scene.kicker}
          </span>
        </Rise>
      ) : null}
      <Words
        text={scene.headline}
        emphasis={scene.emphasis}
        perWord={4}
        start={5}
        style={{
          fontFamily: theme.display,
          fontWeight: 800,
          fontSize: size,
          lineHeight: 1.06,
          color: theme.cream,
        }}
      />
    </Stage>
  );
};

const Quote: React.FC<{ scene: QuoteScene; frames: number }> = ({
  scene,
  frames,
}) => {
  const { unit } = useLayout();
  const words = scene.text.split(/\s+/).length;
  const perWord = Math.max(2, Math.min(7, (frames * 0.45) / words));
  return (
    <Stage frames={frames}>
      <div
        style={{
          fontFamily: theme.display,
          fontWeight: 800,
          fontSize: 190 * unit,
          lineHeight: 0.7,
          height: 96 * unit,
          color: theme.saffron,
        }}
      >
        “
      </div>
      <Words
        text={scene.text}
        emphasis={scene.emphasis}
        perWord={perWord}
        style={{
          fontFamily: theme.display,
          fontWeight: 700,
          fontSize: sizeFor(scene.text, 96, 82, 68) * unit,
          lineHeight: 1.14,
          color: theme.cream,
        }}
      />
    </Stage>
  );
};

const Compare: React.FC<{ scene: CompareScene; frames: number }> = ({
  scene,
  frames,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { unit } = useLayout();
  const reveals = revealFrames(scene.rows, scene.from, frames, fps);
  return (
    <Stage frames={frames}>
      <Rise
        at={0}
        style={{
          fontFamily: theme.display,
          fontWeight: 800,
          fontSize: 76 * unit,
          lineHeight: 1.08,
          color: theme.cream,
          marginBottom: 46 * unit,
        }}
      >
        {scene.title}
      </Rise>
      {scene.rows.map((row, index) => {
        const last = index === scene.rows.length - 1;
        const at = reveals[index];
        return (
          <Rise
            key={row.left}
            at={at}
            style={{
              display: "flex",
              alignItems: "center",
              marginBottom: 22 * unit,
              fontFamily: theme.body,
              fontWeight: 600,
              fontSize: 44 * unit,
            }}
          >
            <div
              style={{
                flex: 1,
                color: theme.cream,
                padding: `${22 * unit}px ${26 * unit}px`,
                borderRadius: 22 * unit,
                backgroundColor: "rgba(255,246,233,0.1)",
              }}
            >
              {row.left}
            </div>
            <div
              style={{
                width: 56 * unit,
                height: 6 * unit,
                backgroundColor: last ? theme.saffron : theme.muted,
                transformOrigin: "left",
                scale: `${interpolate(frame, [at + 6, at + 18], [0, 1], clamp)} 1`,
              }}
            />
            <div
              style={{
                flex: 1,
                color: last ? theme.ink : theme.cream,
                padding: `${22 * unit}px ${26 * unit}px`,
                borderRadius: 22 * unit,
                backgroundColor: last
                  ? theme.saffron
                  : "rgba(255,246,233,0.18)",
                opacity: interpolate(frame, [at + 12, at + 22], [0, 1], clamp),
              }}
            >
              {row.right}
            </div>
          </Rise>
        );
      })}
    </Stage>
  );
};

const List: React.FC<{ scene: ListScene; frames: number }> = ({
  scene,
  frames,
}) => {
  const { fps } = useVideoConfig();
  const { unit } = useLayout();
  const reveals = revealFrames(scene.items, scene.from, frames, fps);
  return (
    <Stage frames={frames}>
      <Rise
        at={0}
        style={{
          fontFamily: theme.display,
          fontWeight: 800,
          fontSize: 76 * unit,
          lineHeight: 1.08,
          color: theme.cream,
          marginBottom: 46 * unit,
        }}
      >
        {scene.title}
      </Rise>
      {scene.items.map((item, index) => (
        <Rise
          key={item.text}
          at={reveals[index]}
          style={{
            display: "flex",
            alignItems: "center",
            marginBottom: 28 * unit,
          }}
        >
          <div
            style={{
              flexShrink: 0,
              width: 76 * unit,
              height: 76 * unit,
              marginRight: 28 * unit,
              borderRadius: 999,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontFamily: theme.display,
              fontWeight: 800,
              fontSize: 42 * unit,
              color: theme.ink,
              backgroundColor: theme.saffron,
            }}
          >
            {index + 1}
          </div>
          <div
            style={{
              fontFamily: theme.body,
              fontWeight: 600,
              fontSize: 50 * unit,
              lineHeight: 1.2,
              color: theme.cream,
            }}
          >
            {item.text}
          </div>
        </Rise>
      ))}
    </Stage>
  );
};

const Agna: React.FC<{ scene: AgnaScene; frames: number }> = ({
  scene,
  frames,
}) => {
  const frame = useCurrentFrame();
  const { unit } = useLayout();
  return (
    <Stage frames={frames}>
      <div
        style={{
          alignSelf: "flex-start",
          fontFamily: theme.display,
          fontWeight: 800,
          fontSize: 40 * unit,
          letterSpacing: "0.14em",
          color: theme.rose,
          border: `${5 * unit}px solid ${theme.rose}`,
          borderRadius: 16 * unit,
          padding: `${10 * unit}px ${24 * unit}px`,
          marginBottom: 40 * unit,
          rotate: "-3deg",
          scale: interpolate(frame, [0, 10], [1.7, 1], {
            ...clamp,
            easing: Easing.bezier(0.16, 1, 0.3, 1),
          }),
          opacity: interpolate(frame, [0, 6], [0, 1], clamp),
        }}
      >
        {(scene.label ?? "AGNA").toUpperCase()}
      </div>
      <Words
        text={scene.text}
        emphasis={scene.emphasis}
        perWord={4}
        start={10}
        style={{
          fontFamily: theme.display,
          fontWeight: 800,
          fontSize: sizeFor(scene.text, 112, 92, 76) * unit,
          lineHeight: 1.08,
          color: theme.cream,
        }}
      />
    </Stage>
  );
};

const Story: React.FC<{ scene: StoryScene; frames: number }> = ({
  scene,
  frames,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { unit } = useLayout();
  const reveals = revealFrames(scene.lines, scene.from, frames, fps);
  return (
    <Stage frames={frames}>
      {scene.place ? (
        <Rise
          at={0}
          style={{
            fontFamily: theme.body,
            fontWeight: 600,
            fontSize: 38 * unit,
            color: theme.saffron,
            marginBottom: 40 * unit,
          }}
        >
          {scene.place}
        </Rise>
      ) : null}
      <div style={{ position: "relative", paddingLeft: 56 * unit }}>
        <div
          style={{
            position: "absolute",
            left: 10 * unit,
            top: 10 * unit,
            bottom: 26 * unit,
            width: 5 * unit,
            backgroundColor: theme.saffron,
            transformOrigin: "top",
            scale: `1 ${interpolate(frame, [0, frames * 0.8], [0, 1], clamp)}`,
          }}
        />
        {scene.lines.map((line, index) => (
          <Rise
            key={line.text}
            at={reveals[index]}
            style={{
              position: "relative",
              fontFamily: theme.display,
              fontWeight: 700,
              fontSize: (scene.lines.length > 3 ? 54 : 66) * unit,
              lineHeight: 1.14,
              color: theme.cream,
              marginBottom: 40 * unit,
            }}
          >
            <div
              style={{
                position: "absolute",
                left: -56 * unit,
                top: 16 * unit,
                width: 25 * unit,
                height: 25 * unit,
                borderRadius: 999,
                backgroundColor: theme.saffron,
              }}
            />
            {line.text}
          </Rise>
        ))}
      </div>
    </Stage>
  );
};

// The speaker on screen. The video has no sound of its own: the clip's one audio track carries on underneath, so the
// sound can't jump or drift when the picture changes.
const Speaker: React.FC<{ scene: SpeakerScene; frames: number }> = ({
  scene,
  frames,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const { vertical, unit, side, captionBottom } = useLayout();
  const focus = scene.focus ?? 0.5;
  return (
    <AbsoluteFill
      style={{
        opacity: interpolate(
          frame,
          [0, 5, Math.max(6, frames - 5), Math.max(7, frames)],
          [0, 1, 1, 0],
          clamp,
        ),
      }}
    >
      <Video
        src={staticFile("video.mp4")}
        trimBefore={Math.round(scene.from * fps)}
        muted
        objectFit="cover"
        style={{
          width: "100%",
          height: "100%",
          objectPosition: `${focus * 100}% 50%`,
        }}
      />
      {scene.label ? (
        <div
          style={{
            position: "absolute",
            left: side * 0.55,
            bottom: captionBottom + (vertical ? 190 : 130) * unit,
            fontFamily: theme.body,
            fontWeight: 600,
            fontSize: 36 * unit,
            color: theme.ink,
            backgroundColor: theme.saffron,
            padding: `${10 * unit}px ${22 * unit}px`,
            borderRadius: 12 * unit,
            opacity: interpolate(frame, [8, 16], [0, 1], clamp),
          }}
        >
          {scene.label}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};

const End: React.FC<{ scene: EndScene; frames: number }> = ({
  scene,
  frames,
}) => {
  const { unit } = useLayout();
  return (
    <Stage frames={frames}>
      <Rise
        at={0}
        style={{
          fontFamily: theme.display,
          fontWeight: 800,
          fontSize: sizeFor(scene.text, 108, 90, 76) * unit,
          lineHeight: 1.08,
          color: theme.cream,
          textAlign: "center",
        }}
      >
        {scene.text}
      </Rise>
      {scene.sub ? (
        <Rise
          at={10}
          style={{
            fontFamily: theme.body,
            fontWeight: 600,
            fontSize: 44 * unit,
            lineHeight: 1.25,
            color: theme.saffron,
            textAlign: "center",
            marginTop: 34 * unit,
          }}
        >
          {scene.sub}
        </Rise>
      ) : null}
    </Stage>
  );
};

export const SceneView: React.FC<{ scene: Scene; frames: number }> = ({
  scene,
  frames,
}) => {
  switch (scene.type) {
    case "hook":
      return <Hook scene={scene} frames={frames} />;
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
    case "speaker":
      return <Speaker scene={scene} frames={frames} />;
    case "end":
      return <End scene={scene} frames={frames} />;
  }
};
