# Design notes: accuracy, translation and summaries

These notes record why the pipeline looks the way it does, what the research did and did not establish, and what is still unmeasured.

## Accuracy: where the transcript loses words

Flex is a 1.2B model that decodes 30 s windows. Cutting audio every 27.5 s regardless of content splits words and sentences; the model then sees half a word at each edge, and the overlap that hides this needs text-level de-duplication. Pause-aware chunking removes most of that at the source:

1. Per-frame (32 ms) speech probabilities come from Silero VAD (MIT, weights bundled in the wheel) or, without it, an adaptive energy gate.
2. A chunk ends at the latest clean pause (at least 0.25 s) between 10 s and 27.5 s. Failing that, the longest short pause (at least 0.10 s) after 6 s. Failing that, a forced cut with 0.4 s overlap, the only case where text de-duplication runs.
3. Edges are trimmed with a 0.4 s pad (0.2 s clipped short word onsets in testing), and chunks with under 0.3 s of speech are skipped, which avoids the hallucinated text recognisers tend to produce on silence.
4. Output guards flag repeated phrases and implausible density (over 35 non-space characters per second) and re-decode that chunk in two halves. They never edit text, because chanting and deliberate repetition are legitimate.

Measured: on real English speech, with a whole-file Silero pass as the reference, chunk boundaries inside speech fell from 8 of 8 (fixed cuts) to 1 of 8, and no speech frames were lost. Not measured: WER on Gujarati. The model could not be run in the development environment, so the benefit for transcription accuracy follows from the mechanism, not from a score. Guard thresholds are heuristics. Use `--chunking fixed` to compare on your own recordings with `evaluate.py --normalize`.

Silero VAD sets PyTorch to one thread on import, which would slow CPU inference; the code restores the previous thread count.

## Translation: why an LLM with context, not a translation model

Candidates examined:

- **IndicTrans2** (AI4Bharat) and **Sarvam-Translate** (a 4B fine-tune of Gemma 3 covering 22 Indian languages, open weights; its authors say it preserves tone, judged by experts and an LLM judge in their own evaluation) are dedicated translation models: one instruction, no register control, trained largely on written text.
- **TranslateGemma** (4B, 12B, 27B, built on Gemma 3) reports gains over Gemma 3 on WMT24++ and lists Gujarati, but the Gujarati figures seen were English→Gujarati, the opposite direction.
- General instruction-following LLMs accept a tone brief, preceding context, a glossary and an `[unclear]` convention. Published work on LLM translation of Indian languages shows large open models doing well on high-resource pairs and weaker on low-resource ones, so Gujarati quality varies a lot by model.

The requirement was spoken, direct English that follows the speaker's tone, not a literal rendering. That needs control over register and cross-segment context, so the design is an instruction-following LLM behind one small interface (OpenAI-compatible local server, or the Anthropic API), with dedicated translation models usable as that model if they follow the brief. Which local model is best for spoken Gujarati is an open question here; `--limit` and `--label` make an A/B comparison on your own audio quick.

Behaviour that matters:

- One segment per call, in order, with the previous three segments and their English, a preview of the next, and the glossary. A failure loses one chunk and a run resumes from a JSONL file.
- Replies are cleaned (reasoning tags, fences, labels, wrapping quotes) and validated (empty, still mostly Gujarati, far shorter than the source). One retry, then the source is kept with an `[untranslated]` marker. Nothing is silently dropped.
- Transcript text is escaped so it cannot close the prompt's tags.

## Summary

The English is summarised in sections sized to the model's context (larger for Claude, smaller for local models), then merged in groups. The schema is fixed JSON (overview, themes with points, stories, quotes, asks, names, unclear). The model's times are snapped to actual segment starts, and each quote is checked against the translation text and marked as a paraphrase when it is not found. The summary is not checked against the original Gujarati, so errors in the translation carry through.

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
