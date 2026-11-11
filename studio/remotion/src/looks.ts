import type { IconWeight } from "@phosphor-icons/react";
import { loadFont as loadBricolage } from "@remotion/google-fonts/BricolageGrotesque";
import { loadFont as loadInstrument } from "@remotion/google-fonts/InstrumentSerif";
import { loadFont as loadManrope } from "@remotion/google-fonts/Manrope";
import { loadFont as loadPlayfair } from "@remotion/google-fonts/PlayfairDisplay";
import { loadFont as loadJakarta } from "@remotion/google-fonts/PlusJakartaSans";
import type { LookName } from "./types";

const latin = { subsets: ["latin" as const] };
const jakarta = loadJakarta("normal", {
  weights: ["700", "800"],
  ...latin,
});
const instrument = loadInstrument("italic", { weights: ["400"], ...latin });
const playfair = loadPlayfair("normal", { weights: ["800"], ...latin });
loadPlayfair("italic", { weights: ["700"], ...latin });
const manrope = loadManrope("normal", { weights: ["700", "800"], ...latin });
const bricolage = loadBricolage("normal", { weights: ["800"], ...latin });

export type Font = {
  family: string;
  weight: number;
  style?: "italic";
  upper?: boolean;
  tracking?: string;
  wide?: number; // average width of a character in ems, so headlines can be kept inside the frame
  scale?: number; // how much bigger an emphasised word is drawn, so a lighter accent face matches the headline
};

// One art direction. Each takes the mood of a reference (palette, texture, energy) and draws motifs of its own, in
// backdrops.tsx. Type is restrained: one clean face for headlines, one contrasting accent, one face for captions.
export type Look = {
  name: LookName;
  dark: boolean;
  ground: string; // the backdrop's base colour
  ink: string; // main text on the backdrop
  soft: string; // secondary text and hairlines
  accent: string; // emphasis, the spoken word, the last row of a compare
  accent2: string;
  accent3: string;
  onAccent: string; // text drawn on an accent fill
  hotOnVideo: string; // the accent drawn over the speaker's video
  display: Font; // headlines
  flair: Font; // emphasised words
  body: Font; // rows, items, small text
  label: Font; // tags, the brand
  caption: Font;
  icon: IconWeight;
  glow?: (colour: string) => string; // an optional text-shadow; no current look uses one
};

export const LOOKS: Record<LookName, Look> = {
  // Cut paper, warm and handmade: torn sheets and paper petals on a warm paper ground.
  paper: {
    name: "paper",
    dark: false,
    ground: "#ece3d1",
    ink: "#23201b",
    soft: "#776d60",
    accent: "#d4502a",
    accent2: "#2c7a77",
    accent3: "#e7b04a",
    onAccent: "#fff8ee",
    hotOnVideo: "#ffc75a",
    display: {
      family: jakarta.fontFamily,
      weight: 800,
      tracking: "-0.035em",
      wide: 0.6,
    },
    flair: {
      family: instrument.fontFamily,
      weight: 400,
      style: "italic",
      tracking: "-0.01em",
      scale: 1.2,
    },
    body: { family: jakarta.fontFamily, weight: 700, tracking: "-0.015em" },
    label: {
      family: jakarta.fontFamily,
      weight: 800,
      upper: true,
      tracking: "0.18em",
    },
    caption: { family: jakarta.fontFamily, weight: 800, tracking: "-0.01em" },
    icon: "fill",
  },
  // An engraved print: ivory paper, fine guilloché lines and a drawn ornamental frame, a high-contrast serif.
  engraving: {
    name: "engraving",
    dark: false,
    ground: "#efe7d6",
    ink: "#1c1a17",
    soft: "#6d6352",
    accent: "#a3271f",
    accent2: "#1c1a17",
    accent3: "#b8893a",
    onAccent: "#f8f2e4",
    hotOnVideo: "#f1c46b",
    display: {
      family: playfair.fontFamily,
      weight: 800,
      tracking: "-0.02em",
      wide: 0.56,
    },
    flair: {
      family: playfair.fontFamily,
      weight: 700,
      style: "italic",
      scale: 1.02,
    },
    body: { family: manrope.fontFamily, weight: 700, tracking: "-0.01em" },
    label: {
      family: manrope.fontFamily,
      weight: 800,
      upper: true,
      tracking: "0.26em",
    },
    caption: { family: manrope.fontFamily, weight: 800 },
    icon: "regular",
  },
  // Flat, grainy editorial, in light colours: warm cream with Swiss-style blocks of orange, mustard and soft blue, deep
  // green type, a chunky grotesque and a fine italic.
  editorial: {
    name: "editorial",
    dark: false,
    ground: "#f4ecdc",
    ink: "#173f35",
    soft: "#6d877c",
    accent: "#e2603a",
    accent2: "#86b8d4",
    accent3: "#f0bf4c",
    onAccent: "#173f35",
    hotOnVideo: "#f5cf4a",
    display: {
      family: bricolage.fontFamily,
      weight: 800,
      tracking: "-0.035em",
      wide: 0.58,
    },
    flair: {
      family: instrument.fontFamily,
      weight: 400,
      style: "italic",
      scale: 1.18,
    },
    body: { family: jakarta.fontFamily, weight: 700, tracking: "-0.015em" },
    label: {
      family: jakarta.fontFamily,
      weight: 800,
      upper: true,
      tracking: "0.18em",
    },
    caption: { family: jakarta.fontFamily, weight: 800, tracking: "-0.01em" },
    icon: "duotone",
  },
};

export const fontStyle = (font: Font): React.CSSProperties => ({
  fontFamily: font.family,
  fontWeight: font.weight,
  fontStyle: font.style ?? "normal",
  textTransform: font.upper ? "uppercase" : "none",
  letterSpacing: font.tracking ?? "normal",
});
