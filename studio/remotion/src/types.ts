export type Format = "short" | "long";
export type LookName = "paper" | "engraving" | "editorial";

// full: the graphic fills the screen. split: the speaker's video takes the top half (the left half in a long clip)
// and the graphic sits in the other half.
export type Layout = "full" | "split";

type Timed = { at?: number; icon?: string };

type Base = { from: number; to: number };
// `art` is one to three icon names drawn as big sticker illustrations of the scene's idea.
type Graphic = Base & { layout?: Layout; icon?: string; art?: string[] };

export type HookScene = Graphic & {
  type: "hook";
  headline: string;
  kicker?: string;
  emphasis?: string[];
};
export type PunchScene = Graphic & {
  type: "punch";
  text: string;
  sub?: string;
};
export type QuoteScene = Graphic & {
  type: "quote";
  text: string;
  emphasis?: string[];
};
export type CompareScene = Graphic & {
  type: "compare";
  title: string;
  rows: (Timed & { left: string; right: string; left_icon?: string })[];
};
export type ListScene = Graphic & {
  type: "list";
  title: string;
  items: (Timed & { text: string })[];
};
export type AgnaScene = Graphic & {
  type: "agna";
  text: string;
  label?: string;
  emphasis?: string[];
};
export type StoryScene = Graphic & {
  type: "story";
  place?: string;
  lines: (Timed & { text: string })[];
};
export type SpeakerScene = Base & {
  type: "speaker";
  label?: string;
  focus?: number;
};
export type EndScene = Graphic & {
  type: "end";
  text: string;
  sub?: string;
};

// A full-screen illustrated moment: a setting drawn in layers, a hero object under light, and the moment's words.
export type Setting = "city" | "palace" | "water" | "hills" | "arch" | "sky";
export type PictureScene = Graphic & {
  type: "picture";
  setting: Setting;
  hero?: string;
  kicker?: string;
  title?: string;
  chips?: (Timed & { text: string })[];
  stamp?: string;
  effect?: "rays" | "fall";
};

export type GraphicScene =
  | PictureScene
  | HookScene
  | PunchScene
  | QuoteScene
  | CompareScene
  | ListScene
  | AgnaScene
  | StoryScene
  | EndScene;

export type Scene = GraphicScene | SpeakerScene;

export type CaptionPage = { text: string; from: number; to: number };

export type ClipProps = {
  format: Format;
  fps: number;
  duration: number;
  hasAudio: boolean;
  hasVideo: boolean;
  style: LookName;
  scenes: Scene[];
  captions: CaptionPage[];
  pictures?: string[]; // names of generated illustrations in public/pictures/<name>.png
};
