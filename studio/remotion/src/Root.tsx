import React from "react";
import { CalculateMetadataFunction, Composition } from "remotion";
import { Clip } from "./Clip";
import type { ClipProps } from "./types";

// Every clip is this one composition: its size and length come from the props tools/studio.py writes.
const metadata: CalculateMetadataFunction<ClipProps> = ({ props }) => ({
  fps: props.fps,
  durationInFrames: Math.round(props.duration * props.fps),
  width: props.format === "short" ? 1080 : 1920,
  height: props.format === "short" ? 1920 : 1080,
});

// A sample of each graphic scene, for working on the look in Remotion Studio.
const sample: ClipProps = {
  format: "short",
  fps: 30,
  duration: 30,
  hasAudio: false,
  hasVideo: false,
  style: "paper",
  captions: [
    { text: "This is where the English captions appear.", from: 0, to: 30 },
  ],
  scenes: [
    {
      type: "hook",
      from: 0,
      to: 5,
      kicker: "Why satsang?",
      headline: "Who cures the illness of the soul?",
      emphasis: ["soul"],
    },
    {
      type: "compare",
      from: 5,
      to: 13,
      title: "Every illness has its healer",
      rows: [
        { left: "Body", right: "Doctor" },
        { left: "Mind", right: "Psychiatrist" },
        { left: "Soul", right: "A true saint" },
      ],
    },
    {
      type: "punch",
      from: 13,
      to: 15,
      text: "Ego. Jealousy. Worry.",
    },
    {
      type: "quote",
      from: 15,
      to: 18,
      text: "You cannot cure ego or jealousy with medicine.",
      emphasis: ["ego", "jealousy"],
    },
    {
      type: "agna",
      from: 18,
      to: 22,
      text: "Listen to your parents.",
      emphasis: ["parents"],
    },
    {
      type: "story",
      from: 22,
      to: 27,
      place: "A sample story",
      lines: [{ text: "First this happened." }, { text: "Then that." }],
    },
    {
      type: "end",
      from: 27,
      to: 30,
      text: "Find your doctor",
      sub: "Follow Harisena",
    },
  ],
};

export const RemotionRoot: React.FC = () => (
  <Composition
    id="Clip"
    component={Clip}
    durationInFrames={900}
    fps={30}
    width={1080}
    height={1920}
    defaultProps={sample}
    calculateMetadata={metadata}
  />
);
