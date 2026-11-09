# Harisena studio

This folder turns one talk into finished videos. The web tool has already transcribed and translated it; from here,
Claude Code does the rest on the user's Claude subscription. Never use a paid API for this work.

From each talk come three things: **the whole talk** (the user uploads it with the English SRT), **short clips** for
YouTube Shorts and Instagram Reels, and **long clips** for normal YouTube uploads.

`tools/studio.py` does everything exact: names, limits, cuts, checks and rendering. You do the judgement: which clips,
and what story each video tells. Never hand-write times, cut media with your own ffmpeg commands, or skip its checks.

Run it from this folder as `python3 tools/studio.py <command>`. `status` shows every talk and how far it has got.

When the user says "start", or drops files and asks you to go, work through these steps. Only step 3 stops for them.

## 1. Take in the talk

`inbox/` holds the English `.srt`, and usually the summary `.md`, the audio and the video. Run `new`. It moves them to
`talks/<YouTube title>/source/`, named after the YouTube title in the summary. With no summary, write a title from the
SRT and pass it with `--title`. Show the user any warning it prints.

## 2. Find the clips

Read all of these: `rules/clip_rules.md`, `../data/channel.md`, the talk's summary if it has one, and every line of the
talk's `lines.txt`. Read the whole talk before proposing anything, because the best passages often need the context
around them to judge where they really start and end.

Write `candidates.json` in the talk's folder:

```json
{"clips": [{
  "start_line": 12, "end_line": 31, "format": "short", "type": "analogy", "score": 9,
  "title": "Who cures the illness of the soul?",
  "hook": "What in the first seconds makes a teenager stay",
  "why": "How it meets the rules, naming them",
  "concerns": "Anything to check, such as names that look wrong. Empty if none"
}]}
```

- Line numbers come from `lines.txt`. Each line is a whole sentence. Check a clip's length from its line times first.
- **short** is 0:30 to 3:00. **long** is over 3:00, up to 10:00. These limits are hard.
- Be honest with scores. Propose what truly fits the rules: some talks have 15 clips and some have 3.
- The same passage can be proposed in both formats if it works in both. Within one format, overlapping clips are
  merged and the higher score is kept, so propose only the better cut and score it accordingly.
- Recordings sometimes contain a section twice. If `lines.txt` repeats itself, propose from one copy only and tell the
  user where the repeat is.

Run `check`. It rejects anything over the limits, merges duplicates, moves each cut onto a real pause, numbers the clips
and writes `clips.md`. If good clips came back rejected, fix them and run it again.

## 3. Let the user choose. Stop here.

Send the list from `clips.md`: number, times, length, type, score and one line on why. Ask which to make. Rendering takes
minutes per clip, so never make videos they didn't pick. When they answer, carry on without further questions.

## 4. Storyboard each chosen clip

Run `prepare <talk> <numbers>`. Then for each clip read its `clip.json` and `captions.json`, and plan before writing:

1. What is the one idea? What should the viewer remember?
2. Break it into beats: hook, build, turn, payoff. Use caption times to see when each beat is spoken.
3. Choose a scene for each beat, so the picture changes when the idea changes.

Write `storyboard.json` in the clip's folder. Scene times are in seconds from the start of the clip, and scenes run back
to back from 0 to exactly the clip's `duration`.

```json
{"scenes": [
  {"type": "hook", "from": 0, "to": 5.2, "kicker": "Why satsang?", "headline": "Who cures the illness of the soul?", "emphasis": ["soul"]},
  {"type": "compare", "from": 5.2, "to": 19, "title": "Every illness has its healer", "rows": [
    {"left": "Body", "right": "Doctor", "at": 6.5},
    {"left": "Mind", "right": "Psychiatrist", "at": 10.1},
    {"left": "Soul", "right": "A true saint", "at": 15.2}]},
  {"type": "speaker", "from": 19, "to": 27, "label": "Guruhari Prabodh Swamiji", "focus": 0.5},
  {"type": "end", "from": 27, "to": 31.4, "text": "Find your doctor", "sub": "Harisena"}
],
"post": {
  "youtube_title": "Who cures the illness of the soul? #Shorts",
  "youtube_description": "...",
  "instagram_caption": "..."
}}
```

| Scene | Use it for | Fields |
| --- | --- | --- |
| `hook` | The opening question or claim. Always the first scene | `headline`, optional `kicker`, `emphasis` |
| `quote` | One line worth reading | `text`, optional `emphasis` |
| `compare` | An analogy, side by side. The last row is highlighted, so end on the point | `title`, 2-5 `rows` of `left`, `right` |
| `list` | Several points or commands | `title`, 2-5 `items` of `text` |
| `agna` | One direct command, stamped on screen | `text`, optional `label`, `emphasis` |
| `story` | The beats of a story, told as a timeline | optional `place`, 1-5 `lines` of `text` |
| `speaker` | Guruhari Prabodh Swamiji talking, from the video | optional `label`, `focus` (0 left to 1 right) |
| `end` | The closing line | `text`, optional `sub` |

Rows, items and lines can take `at`: the clip time when they appear. Set it from `captions.json`, so each appears as it
is spoken.

**Mix graphics with the speaker.** Show him talking wherever a graphic would only repeat itself: when the idea is being
restated, when his delivery or a pause carries the moment, or when the last graphic has done its work. Use graphics
where they add something, such as the first statement of an idea, the structure of an analogy or the beats of a story.
Don't leave the same graphic up longer than the idea lasts. Open on a graphic hook. With no video, every scene is a
graphic.

**Stay true to the talk.** Every word on screen must come from what is said in the clip. Never add a teaching, a claim
or a fact he didn't state. Keep names and terms as `../data/channel.md` spells them, including in captions you describe.
Follow its upasana section: never present the guru parampara differently from it. Keep "Swamiji" and "Guruhari" as
said: never expand them into a full name on screen or in a title, because the audience knows who is meant. This needs
no flag.

**No generated people.** The only images of people are from the talk's own video. Never generate or describe images of
Guruhari Prabodh Swamiji, Guruhari Hariprasad Swamiji or anyone else.

Run `storyboard <talk> <numbers>` until it passes. For the `post` section: keep the title true to the clip, and for
shorts add `#Shorts` to the title. Explain any term a newcomer wouldn't know in the description.

## 5. Look before rendering

Run `preview <talk> <number>`, then open every frame in the clip's `preview/` folder and look at it properly. Check for
overflowing or cramped text, clashes with the captions, and a speaker scene cropped so his face is out of frame (fix with
`focus`). Fix the storyboard and preview again until it looks right. A long clip's full render is costly, so fix things
here. If a scene can't be made to work with the storyboard alone, change the code in `remotion/src/`.

## 6. Render and report

Run `render <talk> <numbers>`. It checks each finished video's size, length and sound, then writes `post.md` with the
title, description and caption. Tell the user where each video is. Be plain about what you couldn't check: you can see
frames but can't hear audio or watch motion, so they should watch each video before posting.

## If the user corrects a choice

If they say a kind of clip is wrong for Harisena, or a kind you missed is good, add that to `rules/clip_rules.md` so
every later talk benefits, and tell them you did.

## Things to know

- Cuts land on real pauses, but they are found from audio levels, so a cut could still clip a word. `clips.md` says so
  when a cut didn't land on a clear pause.
- The SRT only has timing for chunks of speech, so caption and sentence times inside a chunk are estimates. Captions can
  run slightly ahead of or behind the speech.
- The English is machine translated from machine transcribed speech. Names can be wrong, so flag anything that looks off
  under `concerns`.
- Renders use Chrome for Testing, set in `remotion/remotion.config.ts`. The default headless shell hangs on
  macOS 27 at "Waiting for root component to load".
