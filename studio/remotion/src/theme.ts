import { loadFont as loadDisplay } from "@remotion/google-fonts/BricolageGrotesque";
import { loadFont as loadBody } from "@remotion/google-fonts/Inter";
import { useVideoConfig } from "remotion";

const display = loadDisplay("normal", {
  weights: ["700", "800"],
  subsets: ["latin"],
});
const body = loadBody("normal", {
  weights: ["500", "600"],
  subsets: ["latin"],
});

// The look of every video. Change the channel's colours and name here.
export const theme = {
  brand: "HARISENA",
  ink: "#120d1f",
  inkSoft: "#241a38",
  cream: "#fff6e9",
  muted: "#c9bfd6",
  saffron: "#ff9f1c",
  rose: "#ff5c7a",
  display: display.fontFamily,
  body: body.fontFamily,
};

// Sizes scale with the short side, so one design works at 1080x1920 and 1920x1080. The safe areas keep text clear of
// the buttons and captions that Shorts and Reels draw on top of the video.
export const useLayout = () => {
  const { width, height } = useVideoConfig();
  const vertical = height > width;
  return {
    vertical,
    unit: Math.min(width, height) / 1080,
    top: vertical ? 250 : 110,
    bottom: vertical ? 640 : 250,
    side: vertical ? 90 : 170,
    captionBottom: vertical ? 390 : 70,
  };
};

// Longer text gets a smaller size, so it fills the frame without overflowing.
export const sizeFor = (text: string, big: number, medium: number, small: number) =>
  text.length <= 26 ? big : text.length <= 56 ? medium : small;
