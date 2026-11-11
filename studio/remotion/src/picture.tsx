import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { rand } from "./backdrops";
import { fontStyle, type Look } from "./looks";
import { clamp, OUT, revealFrames, Sticker } from "./parts";
import { useLayout, useLook } from "./theme";
import type { PictureScene, Setting } from "./types";

// A picture scene is an illustrated moment from the story, the way a motion designer would stage it: a setting drawn
// in layers of one colour (lighter far away, darker up close, each layer casting a shadow), a hero object in the
// middle under light, and around it the words of the moment — a title, speech chips, a stamp. Everything is drawn here
// in code; no images are generated.

const mixHex = (a: string, b: string, t: number) => {
  const p = (h: string) =>
    [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const [x, y] = [p(a), p(b)];
  return `#${x
    .map((v, i) =>
      Math.round(v + (y[i] - v) * t)
        .toString(16)
        .padStart(2, "0"),
    )
    .join("")}`;
};

// [far, near] colours for land and buildings, and for water, in each look.
const LAND: Record<string, [string, string]> = {
  paper: ["#b5d2c4", "#1f5d5a"],
  engraving: ["#d3c6a6", "#4a4134"],
  editorial: ["#cfe0cf", "#4f8a74"],
};
const WATER: Record<string, [string, string]> = {
  paper: ["#a6dad6", "#256f6c"],
  engraving: ["#d8d9cc", "#8a9c9a"],
  editorial: ["#c4dfe9", "#4f8fa8"],
};

const shade = (look: Look, depth: number, water = false) => {
  const [far, near] = (water ? WATER : LAND)[look.name];
  return mixHex(far, near, depth);
};

// A row of buildings: flat roofs and the odd tower, with lit windows.
const Skyline: React.FC<{
  seed: number;
  base: number;
  tall: number;
  colour: string;
  windows: string | null;
}> = ({ seed, base, tall, colour, windows }) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const blocks: React.ReactNode[] = [];
  let x = -20;
  let i = 0;
  while (x < width + 20) {
    const w = (70 + rand(seed + i) * 110) * (width / 1080);
    const h = tall * (0.45 + rand(seed + i + 50) * 0.55);
    blocks.push(
      <rect
        key={`b${i}`}
        x={x}
        y={base - h}
        width={w + 2}
        height={height - base + h}
        fill={colour}
      />,
    );
    if (rand(seed + i + 9) > 0.7) {
      blocks.push(
        <rect
          key={`t${i}`}
          x={x + w * 0.35}
          y={base - h - tall * 0.18}
          width={w * 0.3}
          height={tall * 0.2}
          fill={colour}
        />,
      );
    }
    if (windows) {
      for (let wx = x + 12; wx < x + w - 14; wx += 26) {
        for (let wy = base - h + 16; wy < base - 10; wy += 30) {
          const key = seed + wx * 0.13 + wy * 0.07;
          if (rand(key) > 0.58) {
            const flicker = rand(key + Math.floor(frame / 40)) > 0.08 ? 1 : 0.3;
            blocks.push(
              <rect
                key={`w${wx}-${wy}`}
                x={wx}
                y={wy}
                width={10}
                height={14}
                rx={2}
                fill={windows}
                opacity={0.85 * flicker}
              />,
            );
          }
        }
      }
    }
    x += w;
    i += 1;
  }
  return <g>{blocks}</g>;
};

// A palace front: a wall, arched gateways and onion domes on towers.
const Palace: React.FC<{ base: number; colour: string; light: string }> = ({
  base,
  colour,
  light,
}) => {
  const { width, height } = useVideoConfig();
  const u = Math.min(width, height) / 1080;
  const towers = width > height ? [0.3, 0.5, 0.7] : [0.12, 0.5, 0.88];
  return (
    <g>
      <rect
        x={-10}
        y={base - 260 * u}
        width={width + 20}
        height={800 * u}
        fill={colour}
      />
      {towers.map((t, i) => {
        const cx = t * width;
        const tw = (i === 1 ? 220 : 150) * u;
        const top = base - (i === 1 ? 470 : 380) * u;
        return (
          <g key={t}>
            <rect
              x={cx - tw / 2}
              y={top}
              width={tw}
              height={base - top + 10}
              fill={colour}
            />
            <path
              d={`M ${cx - tw * 0.55} ${top} Q ${cx - tw * 0.55} ${top - tw * 0.7} ${cx} ${top - tw * 1.05} Q ${cx + tw * 0.55} ${top - tw * 0.7} ${cx + tw * 0.55} ${top} Z`}
              fill={colour}
            />
            <rect
              x={cx - 3 * u}
              y={top - tw * 1.3}
              width={6 * u}
              height={tw * 0.3}
              fill={colour}
            />
            <path
              d={`M ${cx - tw * 0.22} ${base} L ${cx - tw * 0.22} ${base - tw * 0.55} Q ${cx} ${base - tw * 0.9} ${cx + tw * 0.22} ${base - tw * 0.55} L ${cx + tw * 0.22} ${base} Z`}
              fill={light}
              opacity={0.75}
            />
          </g>
        );
      })}
      {Array.from({ length: 9 }, (_, i) => {
        const cx = ((i + 0.5) / 9) * width;
        return (
          <path
            key={i}
            d={`M ${cx - 16 * u} ${base - 150 * u} L ${cx - 16 * u} ${base - 200 * u} Q ${cx} ${base - 226 * u} ${cx + 16 * u} ${base - 200 * u} L ${cx + 16 * u} ${base - 150 * u} Z`}
            fill={light}
            opacity={0.45}
          />
        );
      })}
    </g>
  );
};

// Rolling hills, or waves when `water` is set: one smooth band across the frame.
const band = (
  seed: number,
  width: number,
  height: number,
  y: number,
  amp: number,
  shift: number,
  bumps: number,
) => {
  let d = `M -40 ${height + 20} L -40 ${y}`;
  const steps = bumps * 2;
  for (let i = 0; i <= steps; i++) {
    const x = (i / steps) * (width + 80) - 40 + shift;
    const up = i % 2 === 0 ? -amp * (0.5 + rand(seed + i) * 0.5) : amp * 0.15;
    const px = x - (width + 80) / steps / 2;
    d += ` Q ${px.toFixed(1)} ${(y + up).toFixed(1)} ${x.toFixed(1)} ${y.toFixed(1)}`;
  }
  return `${d} L ${width + 40} ${height + 20} Z`;
};

const Scenery: React.FC<{ setting: Setting }> = ({ setting }) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const u = Math.min(width, height) / 1080;
  const vertical = height > width;
  const floor = height * (vertical ? 0.74 : 0.86);
  // Shadows and glows are SVG filters, not CSS drop-shadow: Chrome rasterises CSS filters on SVG groups unreliably
  // while rendering, so whole layers vanished for single frames.
  const shadow = "url(#pc-shadow)";
  const layers = (
    n: number,
    draw: (k: number, depth: number) => React.ReactNode,
  ) =>
    Array.from({ length: n }, (_, k) => (
      <g key={k} filter={shadow}>
        {draw(k, k / Math.max(1, n - 1))}
      </g>
    ));
  let art: React.ReactNode = null;
  if (setting === "city") {
    art = layers(3, (k, depth) => (
      <Skyline
        seed={k * 40 + 3}
        base={floor + k * 70 * u}
        tall={(330 - k * 80) * u}
        colour={shade(look, 0.25 + depth * 0.75)}
        windows={k === 0 ? null : look.accent3}
      />
    ));
  } else if (setting === "palace") {
    art = (
      <>
        {layers(2, (k, depth) => (
          <Skyline
            seed={k * 11 + 70}
            base={floor - 40 * u + k * 30 * u}
            tall={(260 - k * 60) * u}
            colour={shade(look, depth * 0.4)}
            windows={null}
          />
        ))}
        <g filter={shadow}>
          <Palace
            base={floor + 120 * u}
            colour={shade(look, 0.85)}
            light={look.accent3}
          />
        </g>
      </>
    );
  } else if (setting === "water" || setting === "hills") {
    const water = setting === "water";
    const n = 4;
    art = (
      <>
        {setting === "hills"
          ? [0, 1].map((k) => (
              <path
                key={`c${k}`}
                d={clouds(
                  width,
                  (90 + k * 60) * u,
                  (70 - k * 14) * u,
                  k * 30 + ((frame * (k + 1) * 0.3) % 160),
                )}
                fill={mixHex(
                  look.dark ? "#ffffff" : "#ffffff",
                  look.ground,
                  0.15 + k * 0.25,
                )}
                opacity={look.dark ? 0.18 + k * 0.08 : 0.95}
                filter={shadow}
              />
            ))
          : null}
        {layers(n, (k, depth) => (
          <path
            d={band(
              k * 13 + (water ? 5 : 1),
              width,
              height,
              floor - 140 * u + k * 95 * u,
              (water ? 34 : 120 - k * 18) * u,
              Math.sin(frame / (water ? 14 : 70) + k) * (water ? 28 : 12) * u,
              water ? 7 + k : 3 + k,
            )}
            fill={shade(look, 0.1 + depth * 0.9, water)}
          />
        ))}
      </>
    );
  } else if (setting === "arch") {
    const cx = width / 2;
    const top = height * (vertical ? 0.3 : 0.16);
    const w = width * (vertical ? 0.62 : 0.34);
    const archPath = (inset: number) =>
      `M ${cx - w / 2 + inset} ${floor} L ${cx - w / 2 + inset} ${top + w / 2} A ${w / 2 - inset} ${w / 2 - inset} 0 0 1 ${cx + w / 2 - inset} ${top + w / 2} L ${cx + w / 2 - inset} ${floor}`;
    const glow = look.accent3;
    art = (
      <>
        <rect
          x={0}
          y={floor}
          width={width}
          height={height - floor}
          fill={shade(look, 0.9)}
        />
        {[0, 1, 2].map((k) => (
          <path
            key={k}
            d={archPath(k * 26 * u)}
            fill="none"
            stroke={k === 0 ? glow : shade(look, 0.3 + k * 0.25)}
            strokeWidth={(k === 0 ? 6 : 14) * u}
            opacity={k === 0 ? 0.9 : 0.7}
            filter={k === 0 ? "url(#pc-glow-arch)" : undefined}
          />
        ))}
        {[-1, 1].map((side) => {
          const x = cx + side * (w / 2 + 90 * u);
          const flick = 1 + Math.sin(frame / 3 + side) * 0.08;
          return (
            <g key={side} transform={`translate(${x} ${floor - 10 * u})`}>
              <ellipse
                cx={0}
                cy={0}
                rx={46 * u}
                ry={14 * u}
                fill={look.accent}
              />
              <path
                d={`M 0 ${-70 * u * flick} Q ${18 * u} ${-30 * u} 0 ${-10 * u} Q ${-18 * u} ${-30 * u} 0 ${-70 * u * flick} Z`}
                fill={look.accent3}
                filter="url(#pc-glow-flame)"
              />
            </g>
          );
        })}
      </>
    );
  } else if (setting === "sky") {
    art = (
      <>
        {Array.from({ length: 60 }, (_, i) => (
          <circle
            key={i}
            cx={rand(i + 3) * width}
            cy={rand(i + 90) * floor * 0.9}
            r={(1.5 + rand(i + 7) * 3) * u}
            fill={look.dark ? "#ffffff" : look.ink}
            opacity={0.25 + 0.6 * Math.abs(Math.sin(frame / 14 + i))}
          />
        ))}
        {layers(2, (k, depth) => (
          <path
            d={band(
              k * 7 + 40,
              width,
              height,
              floor + k * 90 * u,
              (90 - k * 20) * u,
              0,
              2 + k,
            )}
            fill={shade(look, 0.5 + depth * 0.5)}
          />
        ))}
      </>
    );
  }
  return (
    <svg
      width={width}
      height={height}
      style={{ position: "absolute", inset: 0 }}
    >
      <defs>
        <filter id="pc-shadow" x="-10%" y="-20%" width="120%" height="140%">
          <feDropShadow
            dx={0}
            dy={-8 * u}
            stdDeviation={6 * u}
            floodColor="#000000"
            floodOpacity={0.28}
          />
        </filter>
        <filter id="pc-glow-arch" x="-20%" y="-20%" width="140%" height="140%">
          <feDropShadow
            dx={0}
            dy={0}
            stdDeviation={7 * u}
            floodColor={look.accent3}
          />
        </filter>
        <filter id="pc-glow-flame" x="-200%" y="-100%" width="500%" height="300%">
          <feDropShadow dx={0} dy={0} stdDeviation={9 * u} floodColor={look.accent3} />
        </filter>
      </defs>
      {art}
    </svg>
  );
};

// A scalloped cloud bank along the top of the frame, bulging downward.
const clouds = (width: number, y: number, r: number, shift: number) => {
  let d = `M -200 -10 L -200 ${y}`;
  for (let x = -200 + shift; x < width + 200; x += r * 1.6) {
    d += ` A ${r} ${r} 0 0 0 ${x + r * 1.6} ${y}`;
  }
  return `${d} L ${width + 200} -10 Z`;
};

export const Picture: React.FC<{ scene: PictureScene; frames: number }> = ({
  scene,
  frames,
}) => {
  const look = useLook();
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const { unit, vertical, pad, captionBottom } = useLayout();
  const enter = interpolate(frame, [0, 12], [0, 1], { ...clamp, easing: OUT });
  const leave = interpolate(frame, [frames - 6, frames], [1, 0], clamp);
  const heroPop = spring({
    frame: frame - 6,
    fps,
    config: { damping: 11, stiffness: 120, mass: 0.8 },
  });
  const heroSize = (vertical ? 380 : 330) * unit;
  const heroY = height * (vertical ? 0.47 : 0.5);
  const bob = Math.sin(frame / 20) * 10 * unit;
  const light = look.accent3;
  const chipTimes = revealFrames(scene.chips ?? [], scene.from, frames, fps);
  const chipSpots = vertical
    ? [
        [0.3, 0.34],
        [0.7, 0.31],
        [0.28, 0.6],
        [0.72, 0.62],
        [0.36, 0.69],
        [0.64, 0.71],
      ]
    : [
        [0.2, 0.3],
        [0.8, 0.28],
        [0.17, 0.62],
        [0.83, 0.64],
        [0.33, 0.78],
        [0.67, 0.8],
      ];
  const stampAt = 16;
  const stampIn = interpolate(frame, [stampAt, stampAt + 7], [2.2, 1], {
    ...clamp,
    easing: OUT,
  });
  return (
    <AbsoluteFill style={{ opacity: leave }}>
      <AbsoluteFill
        style={{
          translate: `0px ${(1 - enter) * 60 * unit}px`,
          opacity: enter,
        }}
      >
        <Scenery setting={scene.setting} />
        {/* a soft wash of the ground colour where the captions sit, so their dark type reads cleanly over busy
            scenery such as lit windows */}
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: captionBottom - 80 * unit,
            height: 340 * unit,
            background: `linear-gradient(transparent, ${look.ground}d9 32%, ${look.ground}d9 68%, transparent)`,
          }}
        />
      </AbsoluteFill>
      {/* light on the hero: a turning fan of rays, or a soft halo */}
      {scene.hero ? (
        <div
          style={{
            position: "absolute",
            left: width / 2 - heroSize * 1.4,
            top: heroY - heroSize * 1.4,
            width: heroSize * 2.8,
            height: heroSize * 2.8,
            borderRadius: "50%",
            background:
              scene.effect === "rays"
                ? `repeating-conic-gradient(from ${frame * 0.5}deg, ${light}38 0deg 7deg, transparent 7deg 20deg)`
                : `radial-gradient(circle, ${light}55 0%, ${light}18 38%, transparent 66%)`,
            maskImage:
              "radial-gradient(circle, black 0%, black 30%, transparent 70%)",
            opacity: heroPop,
          }}
        />
      ) : null}
      {scene.effect === "fall"
        ? Array.from({ length: 26 }, (_, i) => {
            const speed = 2 + rand(i + 5) * 3;
            const y =
              ((frame * speed * unit + rand(i) * height) % (height + 100)) - 50;
            const x =
              rand(i + 30) * width + Math.sin(frame / 25 + i) * 20 * unit;
            const size = (14 + rand(i + 2) * 22) * unit;
            return (
              <div
                key={i}
                style={{
                  position: "absolute",
                  left: x,
                  top: y,
                  width: size,
                  height: size,
                  rotate: `${frame * (rand(i) - 0.5) * 6}deg`,
                  background: [look.accent3, light, "#ffffff"][i % 3],
                  clipPath:
                    "polygon(50% 0, 62% 38%, 100% 50%, 62% 62%, 50% 100%, 38% 62%, 0 50%, 38% 38%)",
                  opacity: 0.85,
                }}
              />
            );
          })
        : null}
      {scene.hero ? (
        <div
          style={{
            position: "absolute",
            left: width / 2 - heroSize / 2,
            top: heroY - heroSize / 2 + bob,
            scale: `${heroPop}`,
            rotate: `${(1 - heroPop) * -25 + Math.sin(frame / 30) * 2}deg`,
          }}
        >
          <Sticker
            name={scene.hero}
            size={heroSize}
            colour={look.accent}
          />
        </div>
      ) : null}
      {/* the words of the moment: a kicker and title up top, speech chips around the hero, a stamp across it */}
      <div
        style={{
          position: "absolute",
          top: pad.top - 20 * unit,
          left: pad.left,
          right: pad.right,
          textAlign: "center",
          opacity: enter,
          translate: `0px ${(1 - enter) * -30 * unit}px`,
        }}
      >
        {scene.kicker ? (
          <div
            style={{
              ...fontStyle(look.label),
              fontSize: 28 * unit,
              color: look.dark ? look.accent3 : look.accent,
              marginBottom: 14 * unit,
            }}
          >
            {scene.kicker}
          </div>
        ) : null}
        {scene.title ? (
          <div
            style={{
              ...fontStyle(look.display),
              fontSize: (scene.title.length > 26 ? 70 : 92) * unit,
              lineHeight: 1.05,
              textWrap: "balance",
              color: look.ink,
              textShadow:
                look.glow?.(look.accent) ??
                (look.dark ? "0 4px 18px rgba(0,0,0,0.5)" : undefined),
            }}
          >
            {scene.title}
          </div>
        ) : null}
      </div>
      {(scene.chips ?? []).map((chip, i) => {
        const [sx, sy] = chipSpots[i % chipSpots.length];
        const pop = spring({
          frame: frame - chipTimes[i],
          fps,
          config: { damping: 10, stiffness: 180, mass: 0.5 },
        });
        return (
          <div
            key={`${chip.text}-${i}`}
            style={{
              position: "absolute",
              left: sx * width,
              top: sy * height + Math.sin(frame / 16 + i) * 8 * unit,
              translate: "-50% -50%",
              scale: `${pop}`,
              rotate: `${(rand(i + 4) - 0.5) * 12}deg`,
              ...fontStyle(look.body),
              fontSize: 34 * unit,
              whiteSpace: "nowrap",
              padding: `${12 * unit}px ${24 * unit}px`,
              borderRadius: look.name === "engraving" ? 4 * unit : 999,
              color: look.name === "engraving" ? look.ink : "#23201b",
              backgroundColor:
                look.name === "engraving" ? look.ground : "#fffaf1",
              border:
                look.name === "engraving"
                  ? `${3 * unit}px solid ${look.ink}`
                  : undefined,
              boxShadow: `0 ${8 * unit}px ${18 * unit}px rgba(0,0,0,0.25)`,
            }}
          >
            {chip.text}
          </div>
        );
      })}
      {scene.stamp ? (
        <div
          style={{
            position: "absolute",
            left: width / 2,
            top: heroY + heroSize * 0.42,
            translate: "-50% -50%",
            rotate: "-9deg",
            scale: `${stampIn}`,
            opacity: interpolate(frame, [stampAt, stampAt + 3], [0, 1], clamp),
            ...fontStyle(look.display),
            textTransform: "uppercase",
            fontSize: 96 * unit,
            lineHeight: 1,
            color: look.accent,
            border: `${8 * unit}px solid ${look.accent}`,
            borderRadius: 14 * unit,
            padding: `${8 * unit}px ${30 * unit}px`,
            backgroundColor: look.dark
              ? "rgba(0,0,0,0.25)"
              : "rgba(255,250,241,0.6)",
            textShadow: look.glow?.(look.accent),
            boxShadow: look.glow ? `0 0 30px ${look.accent}88` : undefined,
            whiteSpace: "nowrap",
          }}
        >
          {scene.stamp}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};
