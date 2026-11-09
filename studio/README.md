# Studio

Turns a translated talk into finished videos for YouTube and Instagram, using Claude Code on your Claude subscription.
There is no API key and no per-use cost. It runs on your own computer.

## Set up once

You need Node.js, Python 3 and ffmpeg. Then, in this folder:

```bash
cd remotion && npm install
```

Copy `rules/clip_rules.example.md` to `rules/clip_rules.md` and write your channel's rules. The first render downloads a
browser for Remotion, which takes a few minutes.

## For each talk

1. In the web tool, fix the words, then run **Translate & write summary**.
2. Download the **English SRT** from the English tab and the **summary MD** from the Summary & YouTube tab.
3. Drop them into `inbox/`, along with the audio and the video if you have it.
4. Open Claude Code in this `studio` folder and say **start**.
5. Claude reads the whole talk and sends you a list of clips. Tell it which numbers to make.
6. Watch the finished videos, then post them. Each clip's folder has its video and a `post.md` with the title,
   description and caption.

The whole talk is yours to upload as it is, with the English SRT. The summary has its title and description.

## What ends up where

```text
inbox/                          drop files here
rules/clip_rules.md             what makes a good clip; keeps improving
talks/<YouTube title>/
  source/                       your files, renamed after the title
  lines.txt                     the talk, sentence by sentence, with times
  clips.md                      the list of clips to choose from
  clips/<number>-<clip title>/
    <title>.mp4                 the finished video
    post.md                     title, description and caption
    storyboard.json             the plan for the video
    preview/                    a frame of each scene
remotion/                       the look of the videos (colours and name in src/theme.ts)
tools/studio.py                 names, limits, cuts, checks and rendering
```

Shorts and Reels are 1080x1920 and 0:30 to 3:00. Long clips are 1920x1080 and over 3:00, up to 10:00. The limits are
checked by the tool as well as being given to Claude.

Your talks, media and clip rules stay out of Git. `CLAUDE.md` holds the instructions Claude follows.
