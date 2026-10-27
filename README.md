# Gujarati Transcriber

Local speech-to-text (automatic speech recognition, ASR) for **Gujarati and Gujarati–English recordings**, with a browser workspace and a CLI for repeatable experiments. After transcribing, it can **translate into natural spoken English that keeps the speaker's tone** and **summarize** the result into main points, key stories and quotes.

Two speech models are available:

- **Flex** ([Bodhan Indic-Transcribe Flex](https://huggingface.co/bodhan-ai/indic-transcribe-flex)) is the default. In its *mixed* mode, Gujarati words come out in Gujarati script and English words in Latin script; *native* mode writes everything in Gujarati script.
- **Gujlish** ([WhissleAI Wav2Vec2](https://huggingface.co/WhissleAI/speech-tagger_gujlish_wav2vec2_meta)) is a smaller model of a different design, kept for comparison. It also reports word timings and how confident the model was in each word.

## Features

- Upload audio, see processing progress, and read the completed transcript.
- Copy text or download TXT/DOCX; reopen previous completed runs.
- Decode common audio/video containers through FFmpeg without extracting a full WAV.
- Separate recordings, dated transcript runs, and optional CLI confidence plots.
- Resume interrupted transcription from one hidden JSON checkpoint per run.
- Select MPS automatically on compatible Apple hardware; CUDA and CPU are also supported.
- Cut audio at the speaker's pauses (Silero VAD when installed, an energy detector otherwise), trim silence, skip stretches with no speech, and flag suspicious output (loops, implausible density) for a second decode.
- **Translate to English** in a spoken, direct register using a local model (Ollama, LM Studio or `mlx_lm.server`) or, optionally, the Claude or Gemini API. Gujarati and English are kept side by side in a bilingual Markdown file.
- **Summarize** the English into an overview, themes with timestamped points, key stories, quotes (checked against the text), asks and names.
- Keep denoising, vocabulary correction, and plotting opt-in in the CLI.

## Quick start

Requirements: Python **3.10+**, FFmpeg/FFprobe, and sufficient memory/disk space for the selected model. Apple Silicon is the primary target. The Flex model card lists a checkpoint of approximately 4.6 GB; initial downloads can take time.

From the repository directory, on macOS:

```bash
brew install ffmpeg
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Ubuntu/Debian, install FFmpeg with `sudo apt install ffmpeg`, then use the same Python setup commands.

Flex is gated: accept its terms on the [model page](https://huggingface.co/bodhan-ai/indic-transcribe-flex) and authenticate once:

```bash
hf auth login
```

Start the web app:

```bash
python app.py
```

Open [http://localhost:8765](http://localhost:8765). Stop the server with `Ctrl+C`. For another port, use `python app.py --port 8766`.

`requirements.txt` includes `silero-vad` for the speech detector. Without it the app falls back to a simpler energy detector and says so.

Model files download on first use and remain in Hugging Face's shared cache. Once the required files are cached, `HF_HUB_OFFLINE=1 python app.py` runs without contacting the model hub.

## Browser workflow

1. Drop or choose a recording.
2. Choose Flex or Gujlish. Flex offers mixed or native script output.
3. Select **Transcribe recording** and follow upload, preparation, and audio-processing progress.
4. Read the result, copy it, or download TXT/DOCX.
5. Open the **English** tab, choose a local model, Claude or Gemini, and select **Translate to English**. Then open **Summary** and select **Summarize**. Each tab has its own copy and download (TXT/DOCX for English, MD/DOCX for the summary).

Progress is based on completed audio chunks, not an estimated time to completion. Model preparation and inputs without known duration use an indeterminate indicator. One transcription runs at a time; other jobs wait to keep model memory bounded.

The web app uses pause-aware chunking with the default detector, and no denoising, vocabulary correction, or plotting. TXT and JSON are saved automatically. DOCX is generated in memory when requested, preserving the transcript text in plain A4 paragraphs. Gujarati font rendering depends on the application opening the document.

Failed jobs offer **Retry & resume**. Refreshing the browser reconnects to its active run; **Previous runs** reopens saved transcripts after a server restart. If the server is stopped mid-job, resume the checkpoint from the CLI with `--output-dir` pointing to that run directory.

### Supported input formats

Common formats include **M4A, MP3, WAV, OPUS, OGG, FLAC, AAC, WEBM, AIFF, and WMA**, plus audio tracks in **MP4, MOV, MKV**, and other FFmpeg-compatible containers. The browser validates an actual audio stream using FFprobe rather than trusting the extension. The upload limit is **4 GB**.

## CLI and repeatable experiments

Place your input files in `recordings/`. Recordings and generated output are excluded from Git; private samples are not shipped with the repository.

```bash
python transcribe.py recordings/meeting.m4a flex
python transcribe.py recordings/meeting.m4a flex --mode native
python transcribe.py recordings/meeting.m4a gujlish
python transcribe.py recordings/meeting.m4a gujlish --plot
```

Optional controls:

| Flag | Behavior |
| --- | --- |
| `--mode mixed\|native` | Flex output script; defaults to `mixed`. |
| `--device auto\|mps\|cuda\|cpu` | Defaults to MPS when available, then CUDA, then CPU. Explicit unavailable devices fail. |
| `--chunking pause\|fixed` | `pause` (default) cuts at pauses and skips silence; `fixed` is the original 27.5 s cuts with 0.4 s overlap, for comparison. |
| `--detector auto\|silero\|energy` | Speech detector for pause chunking; `auto` uses Silero VAD if installed, else the energy gate. |
| `--translate`, `--summarize` | After transcribing, run the English stages (see below). |
| `--denoise` | Apply FFmpeg's `afftdn` filter to the decoded stream. |
| `--vocab FILE` | Correct only explicit spelling variants supplied in a JSON vocabulary. |
| `--plot` | Create one Gujlish confidence PNG after transcription; CLI only. |
| `--reset` | Start fresh; earlier dated run folders remain available. |
| `--output-dir PATH` | Select a transcript run folder explicitly, including for checkpoint resume. |

Use `custom_words.example.json` as a vocabulary template:

```bash
python transcribe.py recordings/meeting.m4a flex --vocab custom_words.example.json
```

### Folder layout and automatic naming

```text
gujarati-transcriber/
├── app.py                         # local web entry point
├── transcribe.py                  # CLI entry point
├── evaluate.py                    # WER / CER evaluation
├── translate.py                   # translate a transcript to spoken English
├── summarize.py                   # summarize the English translation
├── asr/                           # backends, chunking, merge, storage, LLM stages
├── web/                           # Flask server, HTML, CSS, JavaScript
├── tests/                         # synthetic regression tests
├── recordings/                    # local inputs; ignored by Git
├── transcripts/                   # dated runs; ignored by Git
├── plots/                         # optional CLI plots; ignored by Git
├── docs/process-report.md         # development history and validation limits
├── docs/design-notes.md           # why chunking, translation and summaries work the way they do
├── custom_words.example.json      # template for --vocab spelling corrections
├── glossary.example.json          # template for --glossary names and terms in translation
├── requirements.txt               # application dependencies
└── requirements-test.txt          # lightweight CI / regression dependencies
```

For `meeting.m4a`, paths are derived automatically from the basename, backend/mode, and run timestamp:

```text
transcripts/meeting/flex-mixed/20261014-173000-123456/meeting.flex.txt
transcripts/meeting/flex-mixed/20261014-173000-123456/meeting.flex.json
transcripts/meeting/gujlish/20261014-174000-654321/meeting.gujlish.txt
plots/meeting/gujlish/20261014-174000-654321/meeting.gujlish.confidence.png
```

Successful reruns create independent dated folders. Interrupted CLI runs resume the latest checkpoint matching the input and inference settings. Uploading an existing filename adds a numbered suffix instead of overwriting it. Use distinct recording basenames when you want separate groups; CLI inputs sharing a basename share a group, but retain independent dated runs. A non-default `--output-dir` places transcripts/checkpoints there; optional plots remain under `plots/`.

## English translation and summary

```bash
# Local model (Ollama shown; LM Studio and mlx_lm.server also work)
ollama pull <model>                      # choose one; see "Choosing a local model" below
python transcribe.py recordings/talk.m4a flex --translate --summarize --llm-model <model>

# Or run the stages on an existing run folder
python translate.py transcripts/talk/flex-mixed/RUN/ --llm local --llm-model <model>
export ANTHROPIC_API_KEY=...             # only if you use Claude
python summarize.py transcripts/talk/flex-mixed/RUN/ --summary-llm claude
```

Outputs sit next to the transcript: `talk.flex.en.txt` (English, paragraphs follow pauses), `talk.flex.en.json` (per-segment Gujarati/English pairs and flags), `talk.flex.bilingual.md` (read the English against the Gujarati), `talk.flex.summary.md` and `.json`.

**How the tone is kept.** Dedicated translation models take one instruction and offer no tone control, so tone comes from an instruction-following LLM. Each segment is translated with a brief describing the spoken register, the previous three segments with the English already produced (consistent names and phrasing), a short preview of the next segment, and your glossary. The model is told to translate meaning rather than words, carry over emotion and emphasis, use `[unclear]` instead of guessing, and treat transcript text as content, never instructions. `--tone faithful` gives a closer, plainer rendering; `--style-note "a village elder addressing a gathering"` adds context; `--glossary glossary.example.json` fixes names and terms.

**Summary.** The English is summarized in sections and merged, so long talks work with small local context windows. Times are snapped to real segment starts, and every quote is checked against the translation: a quote the text does not contain is shown as a paraphrase. The summary uses the same provider and model as the translation unless you choose another (`--summary-llm`, `--summary-model`).

**Privacy.** With `--llm local` nothing leaves the computer. With Claude (`ANTHROPIC_API_KEY`) or Gemini (`GEMINI_API_KEY`, or `GOOGLE_API_KEY`), only transcript *text* (never audio) is sent to Anthropic or Google. Keys are read from environment variables only; the web app never accepts or stores a key. Check the data-use terms for your Gemini key's tier before sending private recordings.

**Using Gemini.** `export GEMINI_API_KEY=...`, then add `--llm gemini` (and/or `--summary-llm gemini`), or pick Gemini in the web app after starting it from a shell where the variable is set. The default model is `gemini-3.8-flash` (the stable model code on Google's model page). Translation is a language task, not a reasoning task, so requests ask for the lowest thinking level the model allows (`low`) and send no `temperature`, which Google's 3.8 notes say to drop. Choose another model with `--llm-model` or `GUJLISH_GEMINI_MODEL`; a Flash-Lite tier is cheaper but its Gujarati quality is untested here, so compare it with `--limit` first. At Google's published prices ($0.75 per million input and $3.75 per million output tokens until 31 December 2026, double after that), a long recording should cost well under a dollar. That figure is an estimate, not a measurement. Gemini is reached through Google's OpenAI-compatible endpoint, which has not been tested against the live service here.

### Choosing a local model

No benchmark covers spoken Gujarati to English in a particular tone, so the right local model has to be chosen on your own audio. Candidates worth trying on an 18 GB Apple Silicon machine (about 4–14B parameters at 4-bit): Sarvam-Translate (a 4B Gemma 3 fine-tune for 22 Indian languages; its tone claim comes from its authors), TranslateGemma 12B or 4B, and a general instruction model of 12–14B. Their Gujarati→English quality for spoken text is **not verified here**. Compare them like this:

```bash
python translate.py RUN/ --llm-model modelA --limit 15      # writes talk.flex.en-sample.txt
python translate.py RUN/ --llm-model modelB --limit 15 --label modelB
```

`--limit N` translates the first N segments into a labeled sample so full outputs are untouched. Read the bilingual files side by side; if no local model reads as natural speech, use `--llm claude` for translation.

## Challenges, decisions, and trade-offs

The project began as a single-file CLI. Stabilizing it required checking real upstream APIs and balancing recognition quality against runtime cost, generated-text risk, and operational simplicity.

| Challenge | Decision and outcome | Remaining trade-off |
| --- | --- | --- |
| Gujarati–English code-switching | Keep Flex as the primary backend and pass `lang="gu", mode="mixed"` explicitly. Drop Whisper, which my own informal comparisons ranked below Flex. | Quality preference reflects practical observations, not a published benchmark from this project. |
| Unsupported generated text | Add Gujlish CTC for comparison; keep transcripts free of generative grammar rewriting. | Neither architecture guarantees an error-free or hallucination-free transcript. |
| Long recordings and memory | Stream FFmpeg PCM into bounded windows (27.5 s for Flex, 15.5 s for Gujlish) and cut each at the latest clean pause (at least 0.25 s). A forced cut with 0.4 s overlap is only the last resort. | Speech detectors are imperfect and a continuous speaker still forces cuts; full hour-long model inference remains unverified. |
| Duplicate boundary text | Pause cuts need no de-duplication. Only forced cuts (which overlap) remove exact word suffix/prefix matches, ignoring edge punctuation and case. | This is a simple heuristic; it may miss partial-word duplicates or remove real repetition. |
| Recognizer loops and silence | Flag repeated phrases and implausibly dense text, then re-decode that chunk in two halves; skip chunks with no speech. Text is never edited by the guards. | Thresholds are unmeasured heuristics; sparse output is flagged but not retried. |
| Broken Gujlish loading example | Small-file checks showed missing processor configuration. Follow the author's raw-waveform and `vocab.json` inference path instead of `AutoProcessor`. | Upstream packaging changes can require loader updates. |
| Meaningful confidence | Extract CTC softmax probabilities and greedy frame spans; expose Flex confidence as unavailable. | Model probability is not accuracy; timestamps are approximate, without forced alignment. |
| Apple acceleration | Preserve the PyTorch backend and choose MPS when available. | CoreML/MLX conversion and a controlled M3 Pro throughput benchmark remain open work. |
| Optional preprocessing | Default to plain decode → ASR; apply filters/corrections only on request. | Denoising can lose speech cues. CTC vocabulary correction requires low confidence; Flex supports exact supplied variants without a confidence gate. |
| Progress after interruption | Atomically update one partial JSON and resume completed chunks. | Resume re-decodes earlier audio, and rewriting checkpoint JSON grows with the transcript. |
| File clutter and comparisons | Replace per-chunk work folders with dated transcript runs and matching plot folders. | Run folders intentionally accumulate; you decide when to delete old ones. |
| Unicode uploads and exports | Preserve Gujarati combining marks in filenames, number duplicates, and retain Unicode text in TXT/DOCX. | Display fonts vary across systems. |
| Plotting in restricted environments | Use a headless Matplotlib backend and temporary writable cache. | Plotting is optional and deliberately excluded from the web UI. |

Implementation testing also caught and corrected a NumPy view retaining a resizable audio buffer. The decoder now releases that view before removing consumed samples. These details, development stages, and verification evidence are recorded in the [process report](docs/process-report.md).

## Validation and limitations

Completed development checks include real Flex inference in mixed/native modes, a short Flex CLI run, a 50-second Gujlish CLI run, a browser upload-to-transcript run, and one confidence PNG. Synthetic regression tests cover resume, overlapping text, file collisions, invalid audio, Unicode exports, run separation, history reconstruction, and WER/CER calculations.

For the pause-aware chunking, boundaries were measured against a whole-file Silero VAD on real English speech: cuts landing inside speech fell from 8 of 8 (fixed) to 1 of 8, with no speech frames lost. That measures boundary quality, **not word error rate**: the Flex model could not be run in that test environment, so WER with and without pause chunking, the guard thresholds and the translation models on Gujarati are untested. The translation and summary stages were verified end to end with scripted stand-in servers, not with real Gujarati audio.

These checks establish basic functionality. They do **not** establish dataset-level accuracy, hallucination rates, hour-long reliability, or fastest Apple GPU execution. No labeled corpus benchmark or controlled CoreML/MLX/MPS comparison has been completed. The web tool is intended for a single local user, not public multi-user deployment.

## Roadmap

The current release runs locally: follow the Python/FFmpeg installation steps above, download the required model, and launch the browser interface on your own computer. The capabilities below are planned extensions, not features available in this release. The aim is to make Gujarati recordings useful to more readers while keeping the original transcript available as the source of truth.

### Done: English translation and summaries

English translation (local model, Claude or Gemini) and structured summaries are implemented; see [English translation and summary](#english-translation-and-summary). Remaining work is evaluation: a labeled set of Gujarati speech with reference English to compare local models, and a measured WER comparison of pause-aware and fixed chunking.

### Near term: transliteration

Gujarati → Latin transliteration for readers who understand Gujarati but prefer English letters: a script conversion that preserves the spoken language, not a translation. It would be a separately labeled output that leaves the source transcript intact. No provider or model has been selected.

### Provider access

Claude and Gemini are supported through the `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` environment variables. A key-entry flow in the browser is deliberately not provided: any future one must keep keys out of logs and browser storage, say which provider receives what, and treat transcript content as untrusted input to the model. A key does not make this a secure multi-user service.

### Longer term: a hosted website

Make the workflow accessible through a public website so users can transcribe without installing Python, FFmpeg, or model weights. This requires a model-serving environment with suitable compute, managed job execution, concurrency limits, and an operating-cost model. Containerization, potentially with Docker, can help package dependencies, but a deployment approach has not been chosen.

Public hosting also requires authentication or appropriate access controls, isolation between users, upload validation, rate and resource limits, protected credentials, and explicit data-retention/deletion policies. Model-hosting and redistribution permissions must be reviewed against upstream terms before launch. The existing loopback Flask app is a local interface, not the proposed public service.

There are no release dates attached to these items. Public hosting requires further security and operational design.

## Evaluation and development

Evaluate a prediction against a UTF-8 reference:

```bash
python evaluate.py reference.txt transcripts/meeting/flex-mixed/RUN/meeting.flex.txt
```

Add `--normalize` to ignore case, punctuation (including the danda) and Unicode composition, and use the same setting for every system you compare. Without it, WER uses whitespace-separated words. CER uses Unicode code points, including internal whitespace; otherwise neither metric normalizes text. Results are rates (`0.1250` means 12.5%). Empty reference and prediction yield zero; an empty reference with non-empty prediction yields one by convention. Evaluation downloads no datasets.

Run checks without downloading model weights:

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
python -m ruff check .
python -m ruff format --check .
```

GitHub Actions runs the regression, lint, formatting, and JavaScript syntax checks on Python 3.10/3.12. GPU/model inference remains a separate manual check. See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidance and [SECURITY.md](SECURITY.md) for local-use privacy and security boundaries.

## Licensing and acknowledgements

Application code and documentation are licensed under [MIT](LICENSE). Model weights and downloaded upstream inference code retain their own licenses; this repository does not redistribute them.

- [Bodhan Indic-Transcribe Flex](https://huggingface.co/bodhan-ai/indic-transcribe-flex): gated model; review and accept its upstream terms.
- [WhissleAI Gujlish Wav2Vec2](https://huggingface.co/WhissleAI/speech-tagger_gujlish_wav2vec2_meta): the model card identifies Apache 2.0 licensing.
- FFmpeg, PyTorch, Transformers, Flask, python-docx, and Matplotlib provide the decoding, inference, UI, export, and optional plotting components.
