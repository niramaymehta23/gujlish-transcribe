# Harisena studio

This folder turns one talk into finished videos. The web tool has already transcribed and translated it; from here,
Claude Code does the rest on the user's Claude subscription. Never use a paid API for this work, with one exception
the user chose: the clips' illustrations are generated with Gemini (see Pictures below), a few cents each.

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
- A clip can leave lines out from inside it with `"skip": [[first, last], ...]`, such as a crude joke in the middle of a
  good passage. Each piece is cut on a real pause and the pieces are joined; the length is what remains. Use it to keep
  a passage's best parts, but only where the joined speech still flows. `prepare` prints where the joins fall: cover
  each one with a full-screen graphic, so the jump in his video doesn't show.
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
{"style": "paper",
 "pictures": {"first-aid-kit": "a red first-aid kit box with a white cross"},
 "scenes": [
  {"type": "hook", "from": 0, "to": 3.6, "kicker": "Why satsang?", "headline": "Who cures the illness of the soul?", "emphasis": ["soul"], "art": ["first-aid-kit"]},
  {"type": "speaker", "from": 3.6, "to": 8.2, "label": "Guruhari Prabodh Swamiji", "focus": 0.5},
  {"type": "compare", "layout": "split", "from": 8.2, "to": 19, "title": "Every illness has its healer", "rows": [
    {"left": "Body", "right": "Doctor", "left_icon": "person-simple", "icon": "stethoscope", "at": 8.5},
    {"left": "Mind", "right": "Psychiatrist", "left_icon": "brain", "icon": "chats-circle", "at": 11.1},
    {"left": "Soul", "right": "A true saint", "left_icon": "heart", "icon": "hands-praying", "at": 15.2}]},
  {"type": "punch", "from": 19, "to": 21.5, "text": "Ego. Jealousy. Worry."},
  {"type": "speaker", "from": 21.5, "to": 27},
  {"type": "end", "from": 27, "to": 31.4, "text": "Find your doctor", "sub": "Follow for more"}
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
| `punch` | A beat of two or three words, as big as the frame: fast pacing between longer scenes | `text` (up to 32 characters), optional `sub` |
| `quote` | One line worth reading | `text`, optional `emphasis` |
| `compare` | An analogy, side by side. The last row is highlighted, so end on the point | `title`, 2-5 `rows` of `left`, `right` |
| `list` | Several points or commands | `title`, 2-5 `items` of `text` |
| `agna` | One direct command, stamped on screen | `text`, optional `label`, `emphasis` |
| `story` | The beats of a story, told as a timeline | optional `place`, 1-5 `lines` of `text` |
| `picture` | An illustrated moment of a story: a setting, a hero object under light, the moment's words | `setting`, optional `hero`, `kicker`, `title`, 1-6 `chips` of `text`, `stamp`, `effect` |
| `speaker` | Guruhari Prabodh Swamiji talking, from the video | optional `label`, `focus` (0 left to 1 right) |
| `end` | The closing line | `text`, optional `sub` |

Rows, items and lines can take `at`: the clip time when they appear. Set it from `captions.json`, so each appears as it
is spoken. Caption times follow the real pauses in his speech (`prepare` measures them from the audio, and writes when
each word is said to `words.json`). **A graphic appears when he says its words, never before**: the team saw graphics
come up ahead of the speech as the video being out of sync. After writing a storyboard, always run `align` (below),
which moves each graphic and each row to its words; the storyboard check warns about any graphic still more than a
second early. The English is translated from Gujarati, so word order within a sentence can't be matched more closely
than that.

**Style.** Every storyboard names one `style`, the clip's art direction. Vary it across a talk's clips, matching the
clip's mood:

| Style | Mood |
| --- | --- |
| `paper` | Warm and handmade: torn paper sheets and cut-paper petals. Friendly, for stories and analogies |
| `engraving` | Classic print: ivory, fine guilloché lines, a drawn ornamental frame, a serif. Weighty, for scripture and quotes |
| `editorial` | Flat and bold: warm cream with Swiss blocks of orange, mustard and soft blue, deep green type. Clear, for lists and explanations |

All looks are light. The user found dark looks, and a neon one most of all, wrong for a spiritual talk: keep to greens,
pastels, creams, beige, oranges and soft blues. The user likes the engraving look best so far (rich paper texture, fine
line work, engraved pictures), so lean on it.

**Layouts.** There are three pictures, and a good clip moves between all of them: the `speaker` scene shows him full
screen with the captions over the video; any graphic scene with `"layout": "split"` puts his video in the top part of
the frame (the left half of a long clip) and the graphic below it; any other graphic fills the frame. Use split when
his face carries the moment and a graphic still adds something, such as a compare built up while he explains it.

**Pictures: don't wait to be asked.** Whenever a line names something easy to picture (a clock, money, a chart going
up, a mango, a gold chain, a well of water, a TV, a phone, a crown, the sun), put it on screen as `art`: one to three
names drawn as big sticker illustrations that pop in and float, filling the space a full-screen graphic leaves. Use it
on most full-screen hooks, punches, quotes, agnas and end cards; pick the objects the words actually name, the main one
first. (Split scenes have the video as their picture, so `art` is skipped there.)

Pictures are generated, not icons. Describe each one once in the storyboard's top-level `pictures`, by a kebab-case
name, and use that name in `art`, a picture scene's `hero`, or a compare row's `icon`/`left_icon`:

```json
"pictures": {"mango": "a ripe Alphonso mango, golden yellow with a soft red blush, with a short stem and one green leaf"}
```

Describe the object exactly (a mango is not an orange), alone, with no text. Each look draws them in its own manner
(cut paper, flat editorial, engraved print), as die-cut stickers. Don't use them for ideas with no object:
a sparkle, a question mark or a tick says nothing, so leave such a scene without art instead. Then run
`pictures <talk> <numbers>`: it makes any picture not made yet for that look (kept in the talk's `pictures/<style>/`, so
clips share them), and writes `pictures.jpg` in the clip's folder. Look at it closely, and remake a bad one with
`--redo <name>` after changing its description. The Gemini key comes from `GEMINI_API_KEY` or the macOS Keychain item
`harisena-gemini`; never put it in a file. Small compare-row badges may still use Phosphor icons, but keep one kind
per table.

**Picture scenes.** For the moment a story is set (a king's court, a well of water, a mango offered under an arch, a
summer sky, a city at night), stage it as a `picture`. `setting` is one of `city`, `palace`, `water`, `hills`, `arch`
or `sky`, drawn in layers of one colour for depth; `hero` is the object at its centre (one of the pictures); `chips` are short
phrases from the speech that float around it like speech bubbles, appearing at their `at`; `stamp` slams a short
exclamation across it; `effect` adds turning light `rays` or falling sparkles (`fall`). Use them for a few key story
beats per clip, not every scene. They may stay up to 12s.

**Keep him on screen.** The real video of Guruhari Prabodh Swamiji is the heart of every clip: keep it on screen (full
screen or split) for well over half of each clip, around 60-75%. Pictures, stickers and graphics support his words;
they never replace him for long stretches. Every full-screen graphic also gets a few small collage shapes around it,
and hook and punch lines in the paper and editorial looks sit on cut-paper strips; both happen automatically.

**Using inspiration.** References the user shares are a toolbox, not a checklist: keep what already works, and pick
the few ideas that suit this clip. Notes on liked references so far: Vox-style cut-out collage with highlight-strip
headlines; layered paper-cut depth (shades of one colour, each layer shadowed); scrapbook stickers with tape and
halftone; and an earlier "diamond" story video where every beat was its own illustrated set (skyline at night, floating
speech chips, a stamped word, a spotlight on the object, falling sparkles).

**No channel name on screen.** The videos carry no "Harisena" text: no label, no sign-off. Don't put it in any scene.
A kicker or label that is only a name ("Bhagwan Swaminarayan", "Akbar and Birbal") adds nothing either: the team
found it unnecessary. A kicker sets up the line ("Arjuna asked Lord Krishna"), or is left out.

**Icons.** Small icons only mark the rows of a compare, the items of a list and the lines of a story (`icon`, and a
compare row's `left_icon`), and only when every row or item has one. Names are kebab-case
[Phosphor](https://phosphoricons.com) icons such as `hands-praying` or `book-open-text`, or one of the storyboard's
pictures; the check rejects unknown names. A scene itself takes no `icon`: a lone badge beside a headline reads as a
stray mark, so show the object as `art` instead, or nothing. Never an icon that depicts a person's face.

**Captions** show the subtitle's words plainly, with no word-by-word highlight: the SRT only times whole chunks, so a
lit word would drift off the speech.

**Pacing.** Keep the hook under 4.5s, a full graphic under 8s, a split under 15s and the speaker alone under 15s; the
check warns past these. Change the picture whenever the idea changes. Captions are added by the video itself, in one
place low in the frame, so leave that area to them.

**His emphasis is a big moment.** When he repeats a point (`prepare` lists the phrases he says more than once, with
their times, and keeps them in `clip.json` as `emphasis`), or leans hard on a word, mark it on screen: a `punch` or
`agna` on the repeat, big, so the viewer feels the weight he gives it. Don't let a point he says three times pass with
nothing on screen.

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
Guruhari Prabodh Swamiji, Guruhari Hariprasad Swamiji, God, or anyone else, not even hands; the check rejects picture
descriptions that name a person.

Run `align <talk> <numbers>`, then `storyboard <talk> <numbers>` until it passes with no early graphics and every
join covered (align can move a graphic off a join; then add or move one to cover it, and align again). For the `post` section: keep the title true to the clip, and for
shorts add `#Shorts` to the title. Explain any term a newcomer wouldn't know in the description.

## 5. Look before rendering

Run `pictures` first if the storyboard has any, and look at each `pictures.jpg`: is every picture exactly the object
named, clean, and in the clip's style? Then run `preview <talk> <number>` and open every frame in the clip's `preview/`
folder.

Review each frame the way a professional designer would, without being asked, every time. The user's feedback on one
frame is an example of a standard, not a one-off fix: when one thing is wrong, look for the same kind of fault in every
scene type, every clip and both formats, and fix its cause in `remotion/src/` so it can't come back. Go through all of
this for every frame:

- **Alignment and uniformity.** Things of the same kind line up and look the same: rows of a table share columns, so
  arrows, icons and words sit on the same lines; repeated items are the same size, weight, font and spacing; a row or
  item missing an icon still keeps its place. Nothing looks off-centre by accident.
- **Spacing.** Even gaps between items and even margins around the content; nothing crammed against an edge, the video
  or the captions; no lopsided empty area.
- **Sizing and hierarchy.** One clear main element; text big enough to read on a phone; words wrapping evenly (no
  single orphan word, no column three lines deep beside one line); pictures in proportion to the text.
- **Visibility.** Every word stands clearly off what is behind it; no backdrop shape, ring or glow shares the text's
  colour; captions readable over the video and over pictures.
- **Clutter.** Not too many things at once; collage shapes stay in the margins and never touch text or pictures.
- **Truth and fit.** Each picture shows exactly what is said (a mango, not an orange); no meaningless decoration.
- **The speaker.** His face is in frame (fix with `focus`), framed with breathing room, and on screen for 60-75%.

The captions hide themselves while a graphic shows the same words, or while they say most of the graphic's big words,
so a sentence is never on screen twice. Fix the storyboard,
or the code when it is the design, and preview again until every frame passes. A long clip's full render is costly, so
fix things here.

## 6. Render and report

Run `render <talk> <numbers>`. It checks each finished video's size, length and sound, then writes `post.md` with the
title, description and caption. Then pull every frame from a second either side of each scene change of the finished
video and look at them as strips: a frame that differs from both its neighbours is a glitch, and motion between scenes
should be smooth. Tell the user where each video is. Be plain about what you couldn't check: you can see
frames but can't hear audio or watch motion, so they should watch each video before posting.

## If the user corrects a choice

If they say a kind of clip is wrong for Harisena, or a kind you missed is good, add that to `rules/clip_rules.md` so
every later talk benefits, and tell them you did.

## Things to know

- `prepare` cuts any silence over 5s out of a clip (he drinks water, waits for the hall), keeping half a second each
  side; the cut is a join like any other, so cover it with a graphic. The sound comes from the video's own soundtrack
  when there is a video: a separate audio download can be offset from the picture.
- Cuts land on real pauses, but they are found from audio levels, so a cut could still clip a word. `clips.md` says so
  when a cut didn't land on a clear pause.
- The SRT only has timing for chunks of speech, so caption and sentence times inside a chunk are estimates. Captions can
  run slightly ahead of or behind the speech.
- The English is machine translated from machine transcribed speech. Names can be wrong, so flag anything that looks off
  under `concerns`.
- Renders run one Chrome tab at a time (`remotion/remotion.config.ts`). With several, Chrome sometimes returned a
  half-painted frame, seen as a one-frame glitch between scenes. Don't raise the concurrency.
- Renders use Chrome for Testing, set in `remotion/remotion.config.ts`. The default headless shell hangs on
  macOS 27 at "Waiting for root component to load".
