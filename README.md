# Gujarati Transcriber

Turn Gujarati and Gujarati–English recordings into text, on your own computer. Optionally, translate the transcript into natural spoken English and get a structured summary, using Google's Gemini (needs your own Gemini API key; see [step 6](#translate-to-english-and-summarize-optional)).

- **Mixed-language transcripts.** Gujarati words come out in Gujarati script and English words in English letters, the way people actually speak.
- **Runs locally.** Transcription happens on your computer and your audio never leaves it. Only if you translate is the transcript *text* sent to Gemini.
- **Browser app and command line.** Drop a file into the web page, or script it from the terminal.
- **Handles long recordings.** Audio is cut at natural pauses, and an interrupted run picks up where it stopped.
- **Exports.** Copy the text or download TXT, DOCX, or SRT subtitles with timestamps, for both the transcript and the English translation.

## Contents

1. [What you need](#what-you-need)
2. [Install](#install)
3. [Set up Hugging Face (one time)](#set-up-hugging-face-one-time)
4. [Run the app](#run-the-app)
5. [Use the command line](#use-the-command-line)
6. [Translate to English and summarize (optional)](#translate-to-english-and-summarize-optional)
7. [Troubleshooting](#troubleshooting)
8. [Project layout](#project-layout)
9. [For developers](#for-developers)
10. [License and credits](#license-and-credits)

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
git clone https://github.com/niramaymehta23/gujlish-transcribe.git
cd gujlish-transcribe
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

1. **Add a recording.** Drop it on the page or click to choose a file. Most audio formats work (M4A, MP3, WAV, OPUS, OGG, FLAC, AAC, WEBM, AIFF, WMA), as well as the audio in video files (MP4, MOV, MKV).
2. **Choose a model.** Keep **Flex** for the best results. Its **output script** can be *Mixed* (Gujarati script plus English in English letters, the default) or *Native* (everything in Gujarati script). **Gujlish** is a smaller comparison model.
3. **Click Transcribe recording.** The progress bar follows the audio processed so far. The first run also waits for the model to download and load.
4. **Read, copy or download** the transcript as TXT, DOCX or SRT (subtitles with timestamps, for video players and editors).
5. **Optional:** open the **English** and **Summary** tabs to translate and summarize (see [below](#translate-to-english-and-summarize-optional)).

**Previous runs** on the left reopens earlier transcripts, even after restarting the app. If a run fails or you stop it, **Retry & resume** continues from the last finished chunk instead of starting over.

Your files are saved in the project folder: recordings in `recordings/` and results in `transcripts/<recording name>/<model>/<date-time>/`, including SRT subtitle files for the transcript and, once translated, the English. Neither folder is ever uploaded to GitHub.

**Fully offline:** once the model has downloaded, start the app with `HF_HUB_OFFLINE=1 python app.py` and it never contacts Hugging Face.

## Use the command line

Useful for batches or for comparing settings. Put your files in `recordings/` (or anywhere) and run:

```bash
python transcribe.py recordings/talk.m4a flex                  # Gujarati + English (mixed script)
python transcribe.py recordings/talk.m4a flex --mode native     # everything in Gujarati script
python transcribe.py recordings/talk.m4a gujlish                # comparison model
python transcribe.py recordings/talk.m4a gujlish --plot         # plus a confidence chart
```

The transcript is saved as `transcripts/talk/flex-mixed/<date-time>/talk.flex.txt`, with `talk.flex.srt` (subtitles) and a `.json` that has timings. Running the same command again after an interruption resumes it; add `--reset` to start fresh.

| Option | What it does |
| --- | --- |
| `--mode mixed` / `native` | Flex output script (default `mixed`). |
| `--device auto` / `mps` / `cuda` / `cpu` | Where the model runs. `auto` picks the Apple GPU, then NVIDIA, then the CPU. |
| `--chunking pause` / `fixed` | `pause` (default) cuts the audio at the speaker's pauses and skips silence; `fixed` cuts at fixed lengths. |
| `--denoise` | Applies a light noise filter before transcribing. |
| `--vocab FILE` | Fixes spellings you list, for example names. See `custom_words.example.json`. |
| `--plot` | Saves a confidence chart (Gujlish only). |
| `--translate`, `--summarize` | Also translate and summarize afterwards (next section). |
| `--reset` | Ignore an unfinished earlier run and start again. |
| `--output-dir PATH` | Save results in a folder you choose. |

`python transcribe.py --help` lists every option.

## Translate to English and summarize (optional)

After transcribing, the app can translate into **natural spoken English that keeps the speaker's tone**, and summarize the English into an overview, main points by theme, key stories, quotes and names. This uses **Google's Gemini** and needs your own Gemini API key. Without a key, transcription still works; only the English and Summary tabs stay off.

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

Open a finished transcript, go to the **English** tab and click **Translate to English**. Then open **Summary** and click **Summarize**. The English can be downloaded as TXT, DOCX or SRT, and the summary as Markdown or DOCX.

### From the command line

```bash
python transcribe.py recordings/talk.m4a flex --translate --summarize     # all in one go
python translate.py transcripts/talk/flex-mixed/<date-time>/             # translate an existing run
python summarize.py transcripts/talk/flex-mixed/<date-time>/             # then summarize it
```

Results are saved next to the transcript: `talk.flex.en.txt` (English), `talk.flex.en.srt` (English subtitles), `talk.flex.bilingual.md` (Gujarati and English side by side) and `talk.flex.summary.md`.

Useful options: `--tone faithful` for a closer, plainer translation; `--style-note "a teacher speaking to students"` to describe the setting; `--glossary glossary.example.json` to fix how names and terms are translated; `--llm-model <gemini-model>` to use a different Gemini model; `--limit 15 --label test` to try settings on the first 15 segments without touching the full output.

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
| The English tab says to set `GEMINI_API_KEY` | Stop the app, set the key in that terminal (see [step 2](#2-give-the-key-to-the-app)), and start it again. |

## Project layout

```text
gujlish-transcribe/
├── app.py                      # start the web app
├── transcribe.py               # command-line transcription
├── translate.py                # translate a transcript to English
├── summarize.py                # summarize the English
├── evaluate.py                 # measure accuracy against a reference transcript
├── asr/                        # speech models, audio chunking, translation and summary code
├── web/                        # the browser app (Flask server, page, styles, script)
├── tests/                      # automated tests (no model download needed)
├── docs/                       # design notes and development history
├── custom_words.example.json   # template for --vocab
├── glossary.example.json       # template for --glossary
├── recordings/                 # your recordings (kept out of Git)
└── transcripts/                # your results (kept out of Git)
```

## For developers

Run the checks (no model download or GPU needed):

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
python -m ruff check .
python -m ruff format --check .
```

To measure accuracy against a reference transcript, run `python evaluate.py reference.txt transcripts/.../talk.flex.txt --normalize`, which reports word and character error rates.

Design decisions, what has and hasn't been verified, and open questions are in [docs/design-notes.md](docs/design-notes.md) and [docs/process-report.md](docs/process-report.md). See also [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License and credits

The code in this repository is [MIT licensed](LICENSE). The speech models are not part of this repository; each is downloaded from Hugging Face under its own license.

- [**Indic-Transcribe Flex**](https://huggingface.co/bodhan-ai/indic-transcribe-flex) by Bodhan AI, under the Bodhan AI Open Model License. Running it on your own computer is permitted; hosting it as a service for other people requires Bodhan AI's approval, so read its license before doing that.
- [**Gujlish Wav2Vec2**](https://huggingface.co/WhissleAI/speech-tagger_gujlish_wav2vec2_meta) by WhissleAI, under Apache 2.0.
- Built on FFmpeg, PyTorch, Hugging Face Transformers, Silero VAD, Flask and python-docx.

Transcripts, translations and summaries are machine-generated. Check names, numbers and quotes before sharing them.
