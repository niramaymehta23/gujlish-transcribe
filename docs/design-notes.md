# Design notes: accuracy, translation and summaries

These notes record why the pipeline looks the way it does, what the research did and did not establish, and what is still unmeasured.

## Accuracy: where the transcript loses words

Flex is a 1.2B model that decodes 30 s windows. Cutting audio every 27.5 s regardless of content splits words and sentences; the model then sees half a word at each edge, and the overlap that hides this needs text-level de-duplication. Pause-aware chunking removes most of that at the source:

1. Per-frame (32 ms) speech probabilities come from Silero VAD (MIT, weights bundled in the wheel) or, without it, an adaptive energy gate.
2. A chunk ends at the latest clean pause (at least 0.25 s) between 10 s and 27.5 s. Failing that, the longest short pause (at least 0.10 s) after 6 s. Failing that, a forced cut with 0.4 s overlap, the only case where text de-duplication runs.
3. Edges are trimmed with a 0.4 s pad (0.2 s clipped short word onsets in testing), and chunks with under 0.3 s of speech are skipped, which avoids the hallucinated text recognizers tend to produce on silence.
4. Output guards flag repeated phrases and implausible density (over 35 non-space characters per second) and re-decode that chunk in two halves. They never edit text, because chanting and deliberate repetition are legitimate.

Measured: on real English speech, with a whole-file Silero pass as the reference, chunk boundaries inside speech fell from 8 of 8 (fixed cuts) to 1 of 8, and no speech frames were lost. Not measured: WER on Gujarati. Flex could not be run in that test environment, so the benefit for transcription accuracy follows from the mechanism, not from a score. Guard thresholds are heuristics. Use `--chunking fixed` to compare on your own recordings with `evaluate.py --normalize`.

Silero VAD sets PyTorch to one thread on import, which would slow CPU inference; the code restores the previous thread count.

## Translation: why an LLM with context, not a translation model

Candidates examined:

- **IndicTrans2** (AI4Bharat) and **Sarvam-Translate** (a 4B fine-tune of Gemma 3 covering 22 Indian languages, open weights; its authors say it preserves tone, judged by experts and an LLM judge in their own evaluation) are dedicated translation models: one instruction, no register control, trained largely on written text.
- **TranslateGemma** (4B, 12B, 27B, built on Gemma 3) reports gains over Gemma 3 on WMT24++ and lists Gujarati, but the Gujarati figures seen were English→Gujarati, the opposite direction.
- General instruction-following LLMs accept a tone brief, preceding context, a glossary and an `[unclear]` convention. Published work on LLM translation of Indian languages shows large open models doing well on high-resource pairs and weaker on low-resource ones, so Gujarati quality varies a lot by model.

The requirement was spoken, direct English that follows the speaker's tone, not a literal rendering. That needs control over register and cross-segment context, so the design is an instruction-following LLM behind one small interface (an OpenAI-compatible local server, the Anthropic API, or Gemini through Google's OpenAI-compatible endpoint), with dedicated translation models usable as that model if they follow the brief. Which local model is best for spoken Gujarati is an open question here; `--limit` and `--label` make an A/B comparison on your own audio quick.

Behavior that matters:

- One segment per call, in order, with the previous three segments and their English, a preview of the next, and the glossary. A failure loses one chunk and a run resumes from a JSONL file.
- Replies are cleaned (reasoning tags, fences, labels, wrapping quotes) and validated (empty, still mostly Gujarati, far shorter than the source). One retry, then the source is kept with an `[untranslated]` marker. Nothing is silently dropped.
- Transcript text is escaped so it cannot close the prompt's tags.

## Summary

The English is summarized in sections sized to the model's context (larger for Claude, smaller for local models), then merged in groups. The schema is fixed JSON (overview, themes with points, stories, quotes, asks, names, unclear). The model's times are snapped to actual segment starts, and each quote is checked against the translation text and marked as a paraphrase when it is not found. The summary is not checked against the original Gujarati, so errors in the translation carry through.

## Translation prompt in brief

**How the tone is kept.** Dedicated translation models take one instruction and offer no tone control, so tone comes from an instruction-following LLM. Each segment is translated with a brief describing the spoken register, the previous three segments with the English already produced (consistent names and phrasing), a short preview of the next segment, and your glossary. The model is told to translate meaning rather than words, carry over emotion and emphasis, use `[unclear]` instead of guessing, and treat transcript text as content, never instructions. `--tone faithful` gives a closer, plainer rendering; `--style-note "a village elder addressing a gathering"` adds context; `--glossary glossary.example.json` fixes names and terms.

## Earlier decisions and trade-offs

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

Implementation testing also caught and corrected a NumPy view retaining a resizable audio buffer. The decoder now releases that view before removing consumed samples. These details, development stages, and verification evidence are recorded in the [process report](process-report.md).

## What has been verified

Completed development checks include real Flex inference in mixed/native modes, a short Flex CLI run, a 50-second Gujlish CLI run, a browser upload-to-transcript run, and one confidence PNG. Synthetic regression tests cover resume, overlapping text, file collisions, invalid audio, Unicode exports, run separation, history reconstruction, and WER/CER calculations.

For the pause-aware chunking, boundaries were measured against a whole-file Silero VAD on real English speech: cuts landing inside speech fell from 8 of 8 (fixed) to 1 of 8, with no speech frames lost. That measures boundary quality, **not word error rate**: the Flex model could not be run in that test environment, so WER with and without pause chunking, the guard thresholds and the translation models on Gujarati are untested. The translation and summary stages were verified end to end with scripted stand-in servers, not with real Gujarati audio.

These checks establish basic functionality. They do **not** establish dataset-level accuracy, hallucination rates, hour-long reliability, or fastest Apple GPU execution. No labeled corpus benchmark or controlled CoreML/MLX/MPS comparison has been completed. The web tool is intended for a single user on their own computer.

## Known gaps

- No Gujarati reference set exists here, so neither WER nor translation quality has been measured.
- Guard thresholds and the pause parameters are not tuned on Gujarati.
- The web stage endpoints run the CLI scripts as subprocesses with one worker; translation and summary queue behind transcription.

## Sources

- Voice of India benchmark: https://arxiv.org/abs/2604.19151
- Sthanika benchmark of 16 Indic speech-to-text models: https://sthanika.ai/research/what-it-actually-said
- Sarvam-Translate: https://www.sarvam.ai/blogs/sarvam-translate
- TranslateGemma: https://arxiv.org/abs/2601.09012
- LLMs and IndicTrans2 on Indian-language translation: https://arxiv.org/abs/2311.09216
- Silero VAD: https://pypi.org/project/silero-vad/
