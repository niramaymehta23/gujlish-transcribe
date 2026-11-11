import { createContext, useContext } from "react";
import { useVideoConfig } from "remotion";
import { LOOKS, type Look } from "./looks";

export const LookContext = createContext<Look>(LOOKS.paper);
export const useLook = () => useContext(LookContext);

// Whether the graphic being drawn shares the screen with the speaker's video, and so has less room.
export const SplitContext = createContext(false);
// Names of the clip's generated illustrations; a sticker with one of these names draws the picture, not an icon.
export const PicturesContext = createContext<string[]>([]);

export type Rect = { x: number; y: number; w: number; h: number };

// Where things go. Sizes scale with the short side, so one design works at 1080x1920 and 1920x1080. The safe areas
// keep text clear of the buttons and titles that Shorts and Reels draw over the bottom and right of the video.
export const frameGeometry = (width: number, height: number) => {
  const vertical = height > width;
  // A long clip's wide frame gets slightly bigger type, so graphics don't look lost in it.
  const unit = (Math.min(width, height) / 1080) * (vertical ? 1 : 1.12);
  const full: Rect = { x: 0, y: 0, w: width, h: height };
  // The speaker's part of a split scene: the top of a Short, the left half of a long clip.
  const half: Rect = vertical
    ? { x: 0, y: 0, w: width, h: 760 }
    : { x: 0, y: 0, w: width / 2, h: height };
  return {
    vertical,
    unit,
    full,
    half,
    // The speaker full screen. In a Short the wide video would have to be cropped to a third of its width to fill the
    // frame, which is far too close, so it sits in a tall band instead, with a blurred copy of itself around it.
    stage: vertical ? { x: 46, y: 200, w: width - 92, h: 1170 } : full,
    // The video's place when no video is on screen: the half, moved out of the frame.
    hidden: vertical
      ? { ...half, y: -half.h - 40 }
      : { ...half, x: -half.w - 40 },
    // Padding around a full-screen graphic. Its bottom stops above the captions.
    fullPad: vertical
      ? { top: 250, bottom: 600, left: 90, right: 100 }
      : { top: 120, bottom: 240, left: 170, right: 170 },
    // Padding around a graphic in the other part of a split scene, between the video and the captions.
    splitPad: vertical
      ? { top: 810, bottom: 590, left: 80, right: 90 }
      : { top: 120, bottom: 240, left: width / 2 + 80, right: 120 },
    // The captions always sit in the same place: low in the frame, above the platform's own buttons and title.
    captionBottom: vertical ? 400 : 70,
    captionWidth: vertical ? 920 : 1500,
  };
};

export const useLayout = () => {
  const { width, height } = useVideoConfig();
  const split = useContext(SplitContext);
  const geometry = frameGeometry(width, height);
  return {
    ...geometry,
    split,
    // Graphics in a split scene are drawn a little smaller so they fit their part.
    unit: geometry.unit * (split ? (geometry.vertical ? 0.9 : 0.95) : 1),
    pad: split ? geometry.splitPad : geometry.fullPad,
  };
};

// Longer text gets a smaller size, so it fills the frame without overflowing.
export const sizeFor = (
  text: string,
  big: number,
  medium: number,
  small: number,
) => (text.length <= 26 ? big : text.length <= 56 ? medium : small);
