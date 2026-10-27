# Security and privacy

By default this is a single-user local tool. `app.py` binds to `127.0.0.1` with debug mode disabled; keep it on the loopback interface.

Hosted mode (`GUJLISH_HOSTED=1`, see [docs/deploy-cloud-run.md](docs/deploy-cloud-run.md)) is for a small shared site: one optional shared password (no individual accounts), visitors separated by a signed cookie, upload and length limits, automatic deletion after a retention period, and cloud language models off unless the owner allows them. The password and session key come from Secret Manager, never from the repository. It is not designed for large or adversarial audiences.

Recordings and transcripts are saved unencrypted in the project directories. DOCX downloads are generated in memory. The initial model download and authentication contact Hugging Face; inference uses local model weights. Flex loads Python inference code supplied by the upstream model repository. Review and trust that upstream source before running it.

The server checks audio tracks with FFprobe, avoids overwriting upload filenames, rejects mismatched POST origins, and limits uploads to 4 GB. These checks do not make the application a hardened service for untrusted remote users.

Translation and summaries run on a local model server by default. If you choose Claude or Gemini, transcript text (never audio) is sent to Anthropic or Google using the key in the `ANTHROPIC_API_KEY` or `GEMINI_API_KEY` environment variable; the app does not accept a key through the browser or command line and does not write it to disk. Transcript text is escaped before it is placed in prompts, but a model can still be influenced by what a recording says, so review summaries before acting on them.

Do not put Hugging Face tokens in source files or share private recordings/transcripts in issues. If you find a vulnerability, use the repository's private vulnerability-reporting feature if its owner has enabled it; otherwise contact the maintainer privately through an available channel. Avoid publishing sensitive payloads before coordination.
