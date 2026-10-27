# Gujarati ASR: implementation report

**Date:** 14 October 2026  
**Scope:** Local Gujarati/Gujarati–English CLI, browser workflow, experiment organisation, and GitHub preparation.

## Objective and starting point

The goal was practical transcription on an Apple Silicon M3 Pro with 18 GB unified memory: preserve code-switching, minimise unsupported generated text, and accommodate long recordings without large decoded-audio files. The initial project contained one CLI script and local recordings. It exposed Flex, Core, and Whisper, defaulted to CPU/native-script output, and saved separate chunk transcripts in a work directory.

The first pass inspected that implementation instead of treating its interfaces as assumptions. The user's prior comparisons favoured Flex for Gujarati; this was accepted as project context rather than presented as a measured benchmark.

## Backend verification and stabilisation

The cached Bodhan wrapper and upstream model documentation confirmed that Flex accepts in-memory audio, an explicit Gujarati language code, and mixed/native modes. The wrapper's duration warning supported bounded inference. Flex was retained as primary, with mixed-script output selected by default. Whisper and Core were removed from active selection; IndicConformer was not added because no maintained backend existed in the starting script.

Gujlish supplied a standard Wav2Vec2 CTC checkpoint, but its generated `AutoProcessor` example failed because required processor configuration was missing. Downloading small configuration/vocabulary files exposed this before full inference. The loader was changed to the model author's direct raw-waveform and vocabulary approach. Real inference subsequently succeeded.

CTC greedy frame probabilities were used to estimate word spans and geometric-mean confidence. Metadata markers were stripped. Flex confidence remained unavailable. Neither output is described as an accuracy percentage.

## Streaming, correctness, and recovery

FFmpeg became the ingestion layer for mono 16 kHz PCM. Fixed overlapping chunks replaced default pause-based segmentation: 27.5 seconds for Flex and 15.5 seconds for Gujlish, reflecting the latter's documented 16-second training limit. About 400 ms of overlap protects boundary speech; exact word overlap is removed when merging.

Testing found a NumPy buffer view that prevented resizing the pending bytearray. Releasing the view corrected streaming. One atomically replaced hidden partial JSON replaced per-chunk files, allowing resume while keeping normal outputs to TXT/JSON. Resume skips completed inference but re-decodes preceding audio. These simple choices trade some repeated decoding/checkpoint writing for a smaller implementation.

VAD boundaries, FFmpeg denoising, vocabulary correction, and confidence plotting remained optional CLI features. A headless plotting backend and temporary cache addressed a restricted-environment Matplotlib failure. No LLM rewrite or language-routing layer was introduced.

## Browser workflow and organisation

A loopback-only Flask server added uploads, model/script selection, chunk-based progress, transcript display, clipboard copy, and TXT/DOCX downloads. One ASR subprocess runs at a time. The UI distinguishes preparation from measurable transcription progress and has no plotting.

Recordings, dated transcript runs, and CLI plots moved into separate directories. Names derive from the recording, backend/mode, and timestamp. Duplicate uploads receive numbered suffixes; saved JSON/TXT files reconstruct history after restart. Unicode regression testing caught Gujarati combining marks being replaced in upload names, and the sanitiser was corrected. DOCX export preserves Unicode through standard paragraphs.

## Verification and evidence limits

Development verification included real Flex mixed/native inference and a short CLI transcription, a complete 50-second Gujlish CLI transcription, a real browser upload producing a displayed transcript, and one confidence PNG. Regression checks cover interrupted-run resume, overlap merging, output paths, invalid audio, duplicate/Unicode filenames, export text, saved-run history, and evaluation calculations.

Accelerator selection is implemented, but no controlled M3 Pro throughput comparison was completed. MPS was unavailable in the earlier sandbox checks. CoreML encoder/MLX decoder work was considered in the brief but not implemented or benchmarked. Full hour-long model inference, labelled-dataset WER/CER, hallucination measurement, and visual DOCX rendering across office applications remain unverified. Smoke-test success must not be read as proof of recognition quality or production readiness.

## Repository preparation and next work

GitHub preparation added the selected MIT application license, contributor/security guidance, issue/PR templates, formatting checks, and CI using synthetic audio without model downloads. Ignore rules exclude private recordings, outputs, credentials, environments, and model weights. Upstream model licenses remain separate.

The next evidence-producing steps are a labelled Gujarati/code-switching evaluation, a controlled M3 Pro runtime/memory comparison, and an hour-plus recovery test. The implemented application provides a small, inspectable base for those experiments; it does not claim those objectives have already been validated.

## Later revision: chunking, translation and summaries

Pause-aware chunking, output guards, spoken-tone English translation and structured summaries were added afterwards. The design, the research behind it and the unmeasured parts are in [design-notes.md](design-notes.md). Verification covered unit tests, real-speech boundary measurements with Silero VAD, and an end-to-end run with a stand-in recogniser and scripted language-model servers; Flex and real translation models were not run.
