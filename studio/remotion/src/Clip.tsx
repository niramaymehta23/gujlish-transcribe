import { Audio } from "@remotion/media";
import React from "react";
import {
  AbsoluteFill,
  Sequence,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { Backdrop, Brand, Captions } from "./parts";
import { SceneView } from "./scenes";
import type { ClipProps } from "./types";

// One finished video: the backdrop, one scene at a time, captions on top, and one unbroken audio track.
export const Clip: React.FC<ClipProps> = ({ scenes, captions, hasAudio }) => {
  const { fps } = useVideoConfig();
  const frame = useCurrentFrame();
  const now = frame / fps;
  const active = scenes.find((scene) => now >= scene.from && now < scene.to);
  const onSpeaker = active?.type === "speaker";
  return (
    <AbsoluteFill>
      <Backdrop />
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
            <SceneView scene={scene} frames={frames} />
          </Sequence>
        );
      })}
      {onSpeaker ? null : <Brand />}
      <Captions pages={captions} opaque={onSpeaker} />
      {hasAudio ? <Audio src={staticFile("audio.m4a")} /> : null}
    </AbsoluteFill>
  );
};
