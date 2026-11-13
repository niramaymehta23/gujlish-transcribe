# HariSena Vaani Studio

Turn Gujarati and Gujarati–English talks into text on your own computer, then into everything you publish: Gujlish, natural spoken English, timed subtitles, a summary, YouTube details and short clips. Transcription runs on your computer; the English, summary and YouTube steps use Google's Gemini (needs your own Gemini API key; see [step 8](#translate-to-english-and-summarize-optional)).

- **Mixed-language transcripts.** Gujarati words come out in Gujarati script and English words in English letters, the way people actually speak.
- **Gujlish too.** The same transcript in Latin letters ("kem cho"), word for word, made on your computer with AI4Bharat's IndicXlit.
- **Search every talk.** Find words in Gujarati, Gujlish or English, however they are spelled, and jump to the moment they are said; with a Gemini key, also find passages about the same thing and ask questions answered from the talks.
- **Fix words, and it learns.** Double-click a misheard word, type the right one in Gujarati or Gujlish, and the fix is saved to your dictionary, which corrects every later transcript automatically.
- **Runs locally.** Transcription happens on your computer and your audio never leaves it. Only if you translate is the transcript *text* sent to Gemini.
- **Browser app and command line.** Drop a file into the web page, or script it from the terminal.
- **Handles long recordings.** Audio is cut at natural pauses, and an interrupted run picks up where it stopped.
- **Exports.** Copy the text or download TXT, DOCX, or SRT subtitles with timestamps, for both the transcript and the English translation.

## Contents

1. [What you need](#what-you-need)
2. [Install](#install)
3. [Set up Hugging Face (one time)](#set-up-hugging-face-one-time)
4. [Run the app](#run-the-app)
5. [Fix words and build your dictionary](#fix-words-and-build-your-dictionary)
6. [Search every talk](#search-every-talk)
7. [Use the command line](#use-the-command-line)
8. [Translate to English and summarize (optional)](#translate-to-english-and-summarize-optional)
9. [Troubleshooting](#troubleshooting)
10. [Project layout](#project-layout)
11. [For developers](#for-developers)
12. [License and credits](#license-and-credits)

## What you need

- **A computer:** a Mac with Apple Silicon (M1 or later) works best and is what this is mainly built for. Linux and Intel Macs work too, more slowly without a GPU; an NVIDIA GPU on Linux is used automatically. Windows is untested.
- **About 10 GB of free disk space** (the main model is about 4.6 GB, plus Python packages) and **16 GB of memory** recommended.
- **Python 3.10 or newer** and **FFmpeg** (installation below).
- **A free Hugging Face account** to download the speech model.

## Install

### 1. Install the tools

**macOS** (with [Homebrew](https://brew.sh)):

```bash
brew install python@3.12 ffmpeg git
```

**Ubuntu / Debian:**

```bash
sudo apt update && sudo apt install python3 python3-venv ffmpeg git
```

### 2. Download this project

```bash
git clone https://github.com/niramaymehta23/harisena-vaani-studio.git
cd harisena-vaani-studio
```

### 3. Create a Python environment and install the packages

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Linux, use `python3 -m venv .venv` if `python3.12` is not available. The install downloads PyTorch and can take several minutes.

You need to run `source .venv/bin/activate` again in every new terminal window before using the app. Your prompt shows `(.venv)` when it is active.

## Set up Hugging Face (one time)

The default speech model, **Indic-Transcribe Flex**, is free but *gated*: you must accept its terms on Hugging Face and sign in once from your computer before it can download.

1. **Create an account** at [huggingface.co/join](https://huggingface.co/join) (free), and confirm your email address.
2. **Accept the model's terms.** Open the [Indic-Transcribe Flex model page](https://huggingface.co/bodhan-ai/indic-transcribe-flex), read the license, and click the button to agree and get access. Access is usually granted straight away; the page shows when you have it.
3. **Create an access token.** Go to [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens), click **Create new token**, choose the **Read** token type, give it a name (for example `gujarati-transcriber`), and click **Create**. Copy the token; it starts with `hf_`. Treat it like a password: don't share it or put it in any file in this project.
4. **Sign in from your computer.** With the Python environment active, run:

   ```bash
   hf auth login
   ```

   Paste the token when asked (nothing shows as you paste; that is normal) and press Enter. When it asks about adding the token as a git credential, answer `n`.

The model downloads automatically the first time you transcribe (about 4.6 GB), so the first run takes a while. After that it loads from your computer.

The comparison model, **Gujlish**, is not gated and needs no extra steps.

## Run the app

```bash
source .venv/bin/activate      # if not already active
python app.py
```

Open **[http://localhost:8765](http://localhost:8765)** in your browser. To stop the app, press `Ctrl+C` in the terminal. To use another port: `python app.py --port 8766`.

Then:

1. **Add a recording.** Drop it anywhere on the page or click **Choose a file**; it starts straight away. Most audio formats work (M4A, MP3, WAV, OPUS, OGG, FLAC, AAC, WEBM, AIFF, WMA), as well as the audio in video files (MP4, MOV, MKV). If it is a recording you have already transcribed (even renamed), the app asks whether to open that one instead. The app recognises recordings by a fingerprint of the audio, keeps one copy of each, and never uploads audio it already has.
2. **Optional: change the model** first with **Change** under the drop area. Keep **Flex** for the best results. Its **output script** can keep English words in English letters (the default) or write everything in Gujarati script. **Gujlish** is a smaller comparison model.
3. **Watch it work.** The talk appears under **Your talks** with its progress, and you can open other talks while it works. The first run also waits for the model to download and load. Your dictionary's fixes are applied as soon as the Gujarati is ready, and the Gujlish fills in straight after.
4. **Fix any misheard words** on the talk's page. Switch the script between **ગુજરાતી**, **Gujlish** and **Both** (see [below](#fix-words-and-build-your-dictionary)).
5. **Read, copy or download** each version from **Export** as TXT, DOCX or SRT (subtitles with timestamps, for video players and editors).
6. **Optional:** once the words are right, click **Translate & summarise** in the bar at the bottom. The English appears under each Gujarati line, and **See summary** opens the summary and YouTube text (see [below](#translate-to-english-and-summarize-optional)).

**Your talks** on the home page lists every run and what it is doing, even after restarting the app. While a recording is being transcribed you can:

- **Pause** it: finished chunks are kept, and **Resume** continues from there.
- **Cancel** it: after asking, the run and its uploaded recording are deleted. A finished run has **Delete** instead.
- **Stop** the English and summary while they run. Starting again continues where it left off.

Transcription and the Gemini steps have separate queues, so a translation never waits for a transcription. If the app stops in the middle of a run (or a hosted server restarts), it picks the work up again by itself the next time it starts.

Your files are saved in the `data/` folder: recordings in `data/recordings/` and results in `data/transcripts/<recording name>/<model>/<date-time>/`, including SRT subtitle files for the transcript and, once translated, the English. Nothing in `data/` is ever uploaded to GitHub.

**Upgrading from an older copy?** Earlier versions kept these files in the top folder. Move `recordings/`, `transcripts/`, `plots/`, `channel.md`, `youtube_footer.txt`, `dictionary.json` and `dictionary-clips/` (whichever you have) into `data/`, or the app won't see your earlier runs, word fixes or channel profile:

```bash
for d in recordings transcripts plots dictionary-clips; do [ -d "$d" ] && mkdir -p "data/$d" && find "$d" -mindepth 1 -maxdepth 1 -exec mv -n {} "data/$d"/ \; ; rmdir "$d" 2>/dev/null; done
for f in channel.md youtube_footer.txt dictionary.json; do [ -e "$f" ] && mv -n "$f" data/; done
ls recordings transcripts plots dictionary-clips channel.md youtube_footer.txt dictionary.json 2>/dev/null   # anything listed already exists in data/: compare and move it by hand
```

Nothing is overwritten: a file or folder that already exists in `data/` is left where it was.

**Fully offline:** once the model has downloaded, start the app with `HF_HUB_OFFLINE=1 python app.py` and it never contacts Hugging Face.

## Fix words and build your dictionary

Speech recognition mishears some words, often the same ones every time: names, places, terms. Fix them once and the app fixes them for you from then on.

1. **Double-click a word** (or select it and press Enter). Drag across words, or Shift + double-click, to pick several; **More** in the fixer has **+ word before / after** for a phone and the Gujarati spellings to tap.
2. **Type the right word** in either box. Typing Gujlish (English letters) fills in the Gujarati and offers spellings under **More**, so you don't need a Gujarati keyboard. Typing Gujarati fills in the Gujlish. If you only respell the Gujlish, the Gujarati is kept and just the spelling is saved.
3. **Save** (or press Enter). *All N in this talk* changes every match in this recording. *Remember* saves the fix to your dictionary (both are on by default).

A word that was misheard once can be the right word somewhere else, so a saved fix **asks** by default. In new transcripts the words it would change are highlighted; double-click one to **Accept** the fix there or choose **Not here, keep as heard**. Press **▶** beside the heard words to hear just that moment of the recording (it works for any words you pick). Both answers are remembered as evidence. For a fix that can never be wrong (a garbled name, say), tick *Fix without asking* or switch it to **Always** in the Dictionary list: those words are replaced straight away and underlined with dots; hover to see what was heard. Fixes made after translating mark the English and summary as out of date; **Update English & summary** in the bottom bar re-translates only the lines you changed.

The **Dictionary** page (top right) shows what you've taught it: **Asks** / **Always** switches a fix between the two, and **×** forgets it. It is stored in `data/dictionary.json` (on a hosted site, in its storage bucket), never uploaded to GitHub. Each fix keeps the words as heard and corrected in Gujarati and Gujlish and, for the latest five times you made or accepted it and the latest five times you rejected it, the sentence, its time and a short audio clip of that stretch in `data/dictionary-clips/`. The app doesn't judge fixes from the clips: in a test on 21 real fixes, the sound alone picked the right word only twice, because most fixes come from knowing the talk, not from how it sounds. That's why you decide, by ear, with ▶. Fixes saved before this version ask until you switch them to Always.

## Search every talk

Click the magnifier at the top (or press `/` anywhere) to search all your talks at once.

- **Find words** searches every talk's name, Gujarati, Gujlish, English and summary as you type. Spelling needn't be exact: *vaat*, *vat* and *વાત* find the same word, and a typo in a longer word still finds it. Talks with all your words come first, each with its best moments and the words marked.
- **Open a moment.** Click a moment's time to open the talk at that line, lit up, with **▶** to hear 30 seconds from there; **▶** in the results plays it without leaving them. The browser's Back button returns to your results. A moment's link (`#talk=…&t=…`) opens the same place on any device signed in to the same site. The times in a summary open their moments the same way.
- **Similar meaning** (with a Gemini key): when you stop typing, passages about the same thing are found too, so *service* also finds *seva*. These are marked **Similar meaning**.
- **Ask a question** (with a Gemini key): Gemini reads the passages that best match your question (at most ten) and answers from them alone, in the language you asked in. Each point is numbered, and the numbers open the moments they came from. The answer model is `gemini-4-hydrogen`, or `gemini-3.8-flash` where that is not offered; set `VAANI_ANSWER_MODEL` to choose another.

**What goes to Google, and what it costs.** Finding words never leaves your computer. Meaning search sends each talk's text once, in passages of about 40 seconds (the Gujarati with its English) and the summary's points, to Google's `gemini-embedding-2`; a passage is sent again only when you change it. What you search for is sent too, once you stop typing. A question sends the question and the passages it is answered from. Audio is never sent. At Google's prices in November 2026, meaning search costs about $0.006 per hour of talk, once, and a question about $0.01. There is no daily limit, so set a spending limit on your key.

**Where it is kept.** The search index is held in memory (about 1.5 to 2 MB per hour of talk) and saved to `data/.search/index.sqlite` a minute after the last change, so a restart opens it without reading every talk again. Each change (a word fix, a new name, a new talk, a finished step, a delete) updates only that talk, and talks changed from the command line are picked up before your next search. Deleting the file is safe: it is rebuilt from the talks, and meaning search sends their text to Google again.

## Use the command line

Useful for batches or for comparing settings. Put your files in `data/recordings/` (or anywhere) and run:

```bash
python scripts/transcribe.py data/recordings/talk.m4a flex                  # Gujarati + English (mixed script)
python scripts/transcribe.py data/recordings/talk.m4a flex --mode native     # everything in Gujarati script
python scripts/transcribe.py data/recordings/talk.m4a gujlish                # comparison model
python scripts/transcribe.py data/recordings/talk.m4a gujlish --plot         # plus a confidence chart
```

The transcript is saved as `data/transcripts/talk/flex-mixed/<date-time>/talk.flex.txt`, with `talk.flex.srt` (subtitles) and a `.json` that has timings. Running the same command again after an interruption resumes it; add `--reset` to start fresh.

| Option | What it does |
| --- | --- |
| `--mode mixed` / `native` | Flex output script (default `mixed`). |
| `--device auto` / `mps` / `cuda` / `cpu` | Where the model runs. `auto` picks the Apple GPU, then NVIDIA, then the CPU. |
| `--chunking pause` / `fixed` | `pause` (default) cuts the audio at the speaker's pauses and skips silence; `fixed` cuts at fixed lengths. |
| `--denoise` | Applies a light noise filter before transcribing. |
| `--vocab FILE` | Fixes spellings you list, for example names. See `examples/custom_words.json`. |
| `--dictionary data/dictionary.json` | Applies the fixes you saved in the web app. |
| `--gujlish` | Also writes the Gujlish version (`talk.flex.gujlish.txt`, `.srt`, `.json`). |
| `--plot` | Saves a confidence chart (Gujlish only). |
| `--translate`, `--summarize` | Also translate and summarize afterwards (next section). |
| `--reset` | Ignore an unfinished earlier run and start again. |
| `--output-dir PATH` | Save results in a folder you choose. |

`python scripts/transcribe.py --help` lists every option. For an existing run, `python scripts/transliterate.py data/transcripts/talk/flex-mixed/<date-time>/` writes the Gujlish (add `--engine gemini` to have Gemini spell it instead).

## Translate to English and summarize (optional)

After transcribing, the app can translate into **natural spoken English that keeps the speaker's tone**, and summarize the English into an overview, main points by theme, key stories, quotes and names. This uses **Google's Gemini** and needs your own Gemini API key. Without a key, transcription still works; only the English and summary stay off.

**Privacy and cost:** only the transcript *text* is sent to Google, never the audio. Gemini charges per use; at Google's published prices a long recording usually costs well under a dollar. Check Google's data-use terms for your key's tier before sending private recordings.

### 1. Get a Gemini API key (one time)

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and sign in with your Google account.
2. Click **Create API key** and copy it. Treat it like a password: don't share it, and never put it in any file in this project.
3. Optional but recommended: in Google AI Studio or Google Cloud Billing, set a spending limit or budget alert for the key.

### 2. Give the key to the app

The key is read only from your terminal's environment, never typed into the web page. In the terminal where you start the app:

```bash
read -rs GEMINI_API_KEY && export GEMINI_API_KEY     # paste the key and press Enter (nothing shows)
python app.py
```

This lasts until you close that terminal. To avoid repeating it, add `export GEMINI_API_KEY="your-key"` to your shell's startup file (`~/.zshrc` on a Mac), knowing that the key is then stored there in plain text.

### 3. Translate and summarize

Fix the words first, then click **Translate & summarise** in the bar at the bottom of the talk. The corrected Gujarati (not the Gujlish, which is less precise) is translated, then summarized, with a YouTube title and description. The English can be downloaded as TXT, DOCX or SRT, and the summary as Markdown or DOCX.

### From the command line

```bash
python scripts/transcribe.py data/recordings/talk.m4a flex --translate --summarize     # all in one go
python scripts/translate.py data/transcripts/talk/flex-mixed/<date-time>/             # translate an existing run
python scripts/summarize.py data/transcripts/talk/flex-mixed/<date-time>/             # then summarize it
python scripts/captions.py data/transcripts/talk/flex-mixed/<date-time>/              # re-time older English subtitles
```

Results are saved next to the transcript: `talk.flex.en.txt` (English), `talk.flex.en.srt` (English subtitles), `talk.flex.bilingual.md` (Gujarati and English side by side) and `talk.flex.summary.md`.

**English subtitles timed to the speech.** Right after translating, the English subtitles are re-broken into whole phrases and timed to when each phrase is said; the English words themselves never change. The recording is lined up word by word with the transcript, on your computer, using the Gujlish model (downloaded the first time, about 1.2 GB; roughly a minute or two for an hour of audio), so a caption no longer appears during a pause or before it is said. Gemini then reads a few minutes of English at a time and decides which words belong in the same caption, so a phrase is no longer split across two captions, and long sentences are split at a natural break (two lines of at most 42 characters). The result is `talk.flex.en.srt`, with each caption's times and words in `talk.flex.en.captions.json`; the older chunk-by-chunk subtitles stay in `talk.flex.en.unsplit.srt`. Add `--no-captions` to skip this, or run `scripts/captions.py` on an existing run (`--plain` breaks by punctuation only, without Gemini; `--audio FILE` if the recording is not in `data/recordings/`). In the app, a translation made before this step shows **Time the subtitles**.

Useful options: `--tone faithful` for a closer, plainer translation; `--style-note "a teacher speaking to students"` to describe the setting; `--glossary examples/glossary.json` to fix how names and terms are translated; `--llm-model <gemini-model>` to use a different Gemini model; `--limit 15 --label test` to try settings on the first 15 segments without touching the full output.

### YouTube title and description

The **Summary & YouTube** step also writes a YouTube title (plus alternatives), a description ready to paste, chapters and tags. They sit below the summary under **For YouTube**, with buttons to copy the title and the description. YouTube's limits are enforced by the program: titles of at most 100 characters, no `<` or `>`, a description of at most 5,000 characters, tags of at most 500, and chapters only when there are at least three, starting at 0:00 and 10 seconds apart.

Tell it about your channel with plain-text files in `data/` (kept out of Git); copy the templates from `examples/` to start:

| File | What goes in it |
| --- | --- |
| `data/channel.md` | The channel, its audience, speakers, title style, and how names and terms are spelled. The translation uses it for spelling too, and changing it means the next translation starts again from the beginning. |
| `data/youtube_footer.txt` | Links and standard lines added word for word to every description. |

### Clips and motion graphics

Finding clips, storyboards and Remotion videos run in Claude Code on your own computer, not in this app. See [studio/README.md](studio/README.md).

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `401` / `403` / "gated repo" error when the model downloads | Accept the terms on the [Flex model page](https://huggingface.co/bodhan-ai/indic-transcribe-flex) with the same account, then run `hf auth login` again with a **Read** token. |
| `hf: command not found` | Activate the environment first (`source .venv/bin/activate`); the `hf` command is installed with the packages. |
| `ffmpeg` or `ffprobe` not found | Install FFmpeg (`brew install ffmpeg` or `sudo apt install ffmpeg`), then open a new terminal. |
| `ModuleNotFoundError` | The environment isn't active: run `source .venv/bin/activate`, then `python -m pip install -r requirements.txt`. |
| The page doesn't open | Check that `python app.py` is still running in a terminal, and use `http://localhost:8765`. |
| "Address already in use" | Another copy is running. Stop it, or use `python app.py --port 8766`. |
| Very slow | The first run downloads and loads the model. Without an Apple or NVIDIA GPU, transcription runs on the CPU and takes much longer. |
| A quiet recording comes out empty | Try `--chunking fixed` on the command line, which doesn't rely on detecting speech. |
| The bottom bar says to set `GEMINI_API_KEY` | Stop the app, set the key in that terminal (see [step 2](#2-give-the-key-to-the-app)), and start it again. |

## Project layout

```text
harisena-vaani-studio/
├── app.py              # start the web app: python app.py
├── transcriber/        # all the Python code
│   ├── speech/         #   recognising Gujarati: models, audio chunking, guards, word fixes, Gujlish
│   ├── english/        #   Gemini: translation, summary, YouTube title and description
│   └── files/          #   run folders and paths, subtitles, shared text helpers
├── web/                # the browser app (Flask server, page, styles, script)
├── scripts/            # command-line tools: transcribe, translate, summarize, youtube, transliterate, evaluate
├── studio/             # clips and motion graphics, run from Claude Code
├── examples/           # templates: channel.md, youtube_footer.txt, glossary.json, custom_words.json
├── deploy/             # Google Cloud Run setup and deploy scripts
├── docs/               # design notes and development history
├── tests/              # automated tests (no model download needed)
└── data/               # everything private, kept out of Git (the hosted site keeps the same files in its bucket)
    ├── recordings/     #   your recordings
    ├── transcripts/    #   your results
    ├── channel.md      #   your channel profile (copy from examples/)
    ├── youtube_footer.txt
    └── dictionary.json #   your word fixes, made in the web app
```

## For developers

Run the checks (no model download or GPU needed):

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
python -m ruff check .
python -m ruff format --check .
```

To measure accuracy against a reference transcript, run `python scripts/evaluate.py reference.txt data/transcripts/.../talk.flex.txt --normalize`, which reports word and character error rates.

Design decisions, what has and hasn't been verified, and open questions are in [docs/design-notes.md](docs/design-notes.md) and [docs/process-report.md](docs/process-report.md). See also [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License and credits

The code in this repository is [MIT licensed](LICENSE). The speech models are not part of this repository; each is downloaded from Hugging Face under its own license.

- [**Indic-Transcribe Flex**](https://huggingface.co/bodhan-ai/indic-transcribe-flex) by Bodhan AI, under the Bodhan AI Open Model License. Running it on your own computer is permitted; hosting it as a service for other people requires Bodhan AI's approval, so read its license before doing that.
- [**Gujlish Wav2Vec2**](https://huggingface.co/WhissleAI/speech-tagger_gujlish_wav2vec2_meta) by WhissleAI, under Apache 2.0.
- [**IndicXlit**](https://github.com/AI4Bharat/IndicXlit) by AI4Bharat (MIT), for Gujlish, run through a [CTranslate2 conversion](https://huggingface.co/Singla0009/all-indic-transliteration) (MIT).
- Built on FFmpeg, PyTorch, CTranslate2, Hugging Face Transformers, Silero VAD, Flask and python-docx.
- AI-assisted coding used.

Transcripts, translations and summaries are machine-generated. Check names, numbers and quotes before sharing them.
