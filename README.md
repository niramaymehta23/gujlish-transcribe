# Gujarati ASR

Local speech-to-text for **Gujarati and Gujarati–English recordings**, with a browser workspace and a CLI for repeatable experiments. Designed around bounded audio decoding, mixed-script output, and a small Python codebase.

The web app defaults to **Indic-Transcribe Flex / mixed mode**: Gujarati words in Gujarati script and English words in Latin script. Gujlish Wav2Vec2 CTC is available as an architectural comparison with word timings and model probabilities.

## Features

- Upload audio, see processing progress, and read the completed transcript.
- Copy text or download TXT/DOCX; reopen previous completed runs.
- Decode common audio/video containers through FFmpeg without extracting a full WAV.
- Separate recordings, dated transcript runs, and optional CLI confidence plots.
- Resume interrupted transcription from one hidden JSON checkpoint per run.
- Select MPS automatically on compatible Apple hardware; CUDA and CPU are also supported.
- Keep VAD, denoising, vocabulary correction, and plotting opt-in in the CLI.

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

Model files download on first use and remain in Hugging Face's shared cache. Once the required files are cached, `HF_HUB_OFFLINE=1 python app.py` runs without contacting the model hub.

## Browser workflow

1. Drop or choose a recording.
2. Choose Flex or Gujlish. Flex offers mixed or native script output.
3. Select **Transcribe recording** and follow upload, preparation, and audio-processing progress.
4. Read the result, copy it, or download TXT/DOCX.

Progress is based on completed audio chunks, not an estimated time to completion. Model preparation and inputs without known duration use an indeterminate indicator. One transcription runs at a time; other jobs wait to keep model memory bounded.

The web app uses fixed chunks, with no VAD, denoising, vocabulary correction, or plotting. TXT and JSON are saved automatically. DOCX is generated in memory when requested, preserving the transcript text in plain A4 paragraphs. Gujarati font rendering depends on the application opening the document.

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
| `--vad` | Choose low-energy boundaries near the chunk target; this is an energy heuristic, not Silero VAD. |
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
├── asr/                           # backends, audio streaming, merge, storage
├── web/                           # Flask server, HTML, CSS, JavaScript
├── tests/                         # synthetic regression tests
├── recordings/                    # local inputs; ignored by Git
├── transcripts/                   # dated runs; ignored by Git
├── plots/                         # optional CLI plots; ignored by Git
├── docs/process-report.md         # development history and validation limits
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

## Challenges, decisions, and trade-offs

The project began as a single-file CLI. Stabilising it required checking real upstream APIs and balancing recognition quality against runtime cost, generated-text risk, and operational simplicity.

| Challenge | Decision and outcome | Remaining trade-off |
| --- | --- | --- |
| Gujarati–English code-switching | Keep Flex as the primary backend and pass `lang="gu", mode="mixed"` explicitly. Remove Whisper from active selection based on the user's earlier comparisons. | Quality preference reflects practical observations, not a published benchmark from this project. |
| Unsupported generated text | Add Gujlish CTC for comparison; keep transcripts free of generative grammar rewriting. | Neither architecture guarantees an error-free or hallucination-free transcript. |
| Long recordings and memory | Stream FFmpeg PCM into bounded chunks: 27.5 s for Flex, 15.5 s for Gujlish, with 0.4 s overlap. | Boundaries can still split words; full hour-long model inference remains unverified. |
| Duplicate boundary text | Remove exact word suffix/prefix matches, ignoring edge punctuation and case. | This is a simple heuristic; it may miss partial-word duplicates or remove real repetition. |
| Broken Gujlish loading example | Small-file checks showed missing processor configuration. Follow the author's raw-waveform and `vocab.json` inference path instead of `AutoProcessor`. | Upstream packaging changes can require loader updates. |
| Meaningful confidence | Extract CTC softmax probabilities and greedy frame spans; expose Flex confidence as unavailable. | Model probability is not accuracy; timestamps are approximate, without forced alignment. |
| Apple acceleration | Preserve the PyTorch backend and choose MPS when available. | CoreML/MLX conversion and a controlled M3 Pro throughput benchmark remain open work. |
| Optional preprocessing | Default to plain decode → ASR; apply filters/corrections only on request. | Denoising can lose speech cues. CTC vocabulary correction requires low confidence; Flex supports exact supplied variants without a confidence gate. |
| Progress after interruption | Atomically update one partial JSON and resume completed chunks. | Resume re-decodes earlier audio, and rewriting checkpoint JSON grows with the transcript. |
| File clutter and comparisons | Replace per-chunk work folders with dated transcript runs and matching plot folders. | Run folders intentionally accumulate; the user manages retention. |
| Unicode uploads and exports | Preserve Gujarati combining marks in filenames, number duplicates, and retain Unicode text in TXT/DOCX. | Display fonts vary across systems. |
| Plotting in restricted environments | Use a headless Matplotlib backend and temporary writable cache. | Plotting is optional and deliberately excluded from the web UI. |

Implementation testing also caught and corrected a NumPy view retaining a resizable audio buffer. The decoder now releases that view before removing consumed samples. These details, development stages, and verification evidence are recorded in the [process report](docs/process-report.md).

## Validation and limitations

Completed development checks include real Flex inference in mixed/native modes, a short Flex CLI run, a 50-second Gujlish CLI run, a browser upload-to-transcript run, and one confidence PNG. Synthetic regression tests cover resume, overlapping text, file collisions, invalid audio, Unicode exports, run separation, history reconstruction, and WER/CER calculations.

These checks establish basic functionality. They do **not** establish dataset-level accuracy, hallucination rates, hour-long reliability, or fastest Apple GPU execution. No labelled corpus benchmark or controlled CoreML/MLX/MPS comparison has been completed. The web tool is intended for a single local user, not public multi-user deployment.

## Roadmap

The current release runs locally: follow the Python/FFmpeg installation steps above, download the required model, and launch the browser interface on your own computer. The capabilities below are planned extensions, not features available in this release. The aim is to make Gujarati recordings useful to more readers while keeping the original transcript available as the source of truth.

### Near term: transliteration and translation

Add optional text-processing actions after transcription, with support for an existing transcript or uploaded text file:

- **Gujarati → Latin transliteration:** represent Gujarati speech in Latin characters for readers who understand Gujarati but prefer reading it in English letters. Preserve the spoken language and meaning; this is a script conversion, not an English translation.
- **Gujarati → English translation:** produce an English-language version for readers who do not understand Gujarati, preserving meaning, names, and technical terminology as faithfully as possible.

Each action should produce a separately labelled result with copy/download options, leaving the source transcript intact. Transliteration and translation quality need their own evaluation, particularly for names and Gujarati–English code-switching. No text-processing provider or model has been selected yet.

### Next: transcript summaries

Add an optional summary workflow to extract the main points, central themes, and key takeaways from a recording's transcript. Where applicable, include decisions or action items only when they are supported by the source. Summaries should remain traceable to the transcript, distinguish uncertainty, and avoid introducing claims absent from the recording. They will be separate derived outputs rather than replacements for the full transcript.

### Provider access: bring your own key

Explore a **bring-your-own-key (BYOK)** option for supported external services, allowing users to supply their own provider credentials for explicitly selected processing tasks. This would complement the local workflow; provider support, billing behavior, and the scope of remote processing remain to be designed.

Security and privacy are requirements for this work: minimise credential retention, keep keys out of logs and browser-persistent storage, protect any necessary storage and transmission, and make removal straightforward. Before sending content externally, explain which provider receives it, what text/audio is sent, and any associated usage costs, and obtain the user's consent. Transcript content must be treated as untrusted input to any model-based text tool. BYOK by itself does not establish a secure multi-user service.

### Longer term: a hosted website

Make the workflow accessible through a public website so users can transcribe without installing Python, FFmpeg, or model weights. This requires a model-serving environment with suitable compute, managed job execution, concurrency limits, and an operating-cost model. Containerisation, potentially with Docker, can help package dependencies, but a deployment approach has not been chosen.

Public hosting also requires authentication or appropriate access controls, isolation between users, upload validation, rate and resource limits, protected credentials, and explicit data-retention/deletion policies. Model-hosting and redistribution permissions must be reviewed against upstream terms before launch. The existing loopback Flask app is a local interface, not the proposed public service.

There are no release dates attached to these items. Immediate development priority is transliteration and translation, followed by summarisation; BYOK and public hosting require further security and operational design.

## Evaluation and development

Evaluate a prediction against a UTF-8 reference:

```bash
python evaluate.py reference.txt transcripts/meeting/flex-mixed/RUN/meeting.flex.txt
```

WER uses whitespace-separated words. CER uses Unicode code points, including internal whitespace; neither metric applies language-specific normalisation. Results are rates (`0.1250` means 12.5%). Empty reference and prediction yield zero; an empty reference with non-empty prediction yields one by convention. Evaluation downloads no datasets.

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
