export type Format = "short" | "long";

type Timed = { at?: number };

type Base = { from: number; to: number };

export type HookScene = Base & {
  type: "hook";
  headline: string;
  kicker?: string;
  emphasis?: string[];
};
export type QuoteScene = Base & {
  type: "quote";
  text: string;
  emphasis?: string[];
};
export type CompareScene = Base & {
  type: "compare";
  title: string;
  rows: (Timed & { left: string; right: string })[];
};
export type ListScene = Base & {
  type: "list";
  title: string;
  items: (Timed & { text: string })[];
};
export type AgnaScene = Base & {
  type: "agna";
  text: string;
  label?: string;
  emphasis?: string[];
};
export type StoryScene = Base & {
  type: "story";
  place?: string;
  lines: (Timed & { text: string })[];
};
export type SpeakerScene = Base & {
  type: "speaker";
  label?: string;
  focus?: number;
};
export type EndScene = Base & {
  type: "end";
  text: string;
  sub?: string;
};

export type Scene =
  | HookScene
  | QuoteScene
  | CompareScene
  | ListScene
  | AgnaScene
  | StoryScene
  | SpeakerScene
  | EndScene;

export type CaptionPage = { text: string; from: number; to: number };

export type ClipProps = {
  format: Format;
  fps: number;
  duration: number;
  hasAudio: boolean;
  hasVideo: boolean;
  scenes: Scene[];
  captions: CaptionPage[];
};
