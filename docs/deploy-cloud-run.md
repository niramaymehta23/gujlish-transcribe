# Putting the transcriber online with Google Cloud Run

This runs the transcriber on Google Cloud Run in two parts:

- **The website** (`SERVICE`): a small **CPU-only** service that scales to zero when nobody uses it. Anyone with the link (and the password) can open it from any device or network. Opening it, leaving a tab open, sharing the link or a bot visiting it never switches on a GPU.
- **The GPU job** (`GPU_JOB`): a Cloud Run job with one NVIDIA L4 GPU. The website starts one run of it for each recording; it transcribes, saves the transcript to the bucket and exits. **The GPU is billed only while a recording is being transcribed.**

Each transcription waits a minute or two for the job to start and load the model; Gujlish, translation, captions and summaries run on the website and start at once.

## Before you start: the Flex license

Flex's license (the Bodhan AI Open Model License, in the model repository) requires Bodhan AI's **prior written approval** before you host Flex for other people ("Third-Party Hosting", section 3). Exceptions include registered nonprofits, trusts and societies using it for their own charitable purpose, and academic research use (section 13). If you host Flex, the site must also show "Built with Indic-Transcribe Flex from Bodhan AI / AI4Bharat." The app does this automatically whenever Flex is enabled.

This deployment runs **Flex only** (`MODELS="flex"` in `deploy/config.sh`) for the owner's personal use, behind the site password; it is not offered to other people. If that changes, get Bodhan AI's approval first, or switch back to `MODELS="gujlish"` (Apache 2.0). This note is not legal advice.

## What the hosted site does differently

`VAANI_HOSTED=1` (set in the Docker image) switches on:

| Behavior | Default when hosted | Change with |
| --- | --- | --- |
| Password for the whole site | Whatever you entered in setup, or none | `deploy/setup.sh` |
| Everyone signed in sees the same talks | Always | — |
| Upload size limit | 300 MB | `VAANI_MAX_UPLOAD_MB` |
| Recording length limit | 90 minutes | `VAANI_MAX_MINUTES` |
| Recordings and results deleted | Never: kept in your bucket until you delete them | `VAANI_RETENTION_HOURS` |
| One recording transcribed (or waiting) at a time on the whole site | Always | — |
| Translation and summaries | On when a Gemini key is stored (setup asks for it); Gemini runs on your API key | `VAANI_ALLOW_GEMINI` |
| Search: finding words | Always | — |
| Search: similar meaning and questions | With translation: on when a Gemini key is stored | `VAANI_ALLOW_GEMINI` |
| Large uploads | Sent in 8 MB pieces, because Cloud Run caps one request at 32 MB | — |

The site password is the account: everyone who signs in, on any device, sees, opens, renames and deletes the same talks, and **Sign out** (in Details) asks for the password again. Without a password, anyone with the address can. A browser's signed cookie only keeps a half-sent upload to that browser.

## One-time setup (about 20 minutes)

1. **Google Cloud account and project.** At [console.cloud.google.com](https://console.cloud.google.com), create a project and link a billing account. New accounts usually get free trial credit. Note the project ID.
2. **Budget alert.** In Billing → Budgets & alerts, create a budget (for example $10/month) with email alerts, so you hear about unexpected costs early. A budget alerts you; it does not stop the service.
3. **Install the gcloud CLI** and sign in:
   ```bash
   brew install --cask google-cloud-sdk
   gcloud auth login
   ```
4. **Hugging Face token.** At [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens), create a token with **read** access. The build uses it to download the models.
5. **GPU quota.** A project gets a quota of 3 L4 GPUs (without zonal redundancy) automatically on its first GPU deployment in a region, which is more than this setup uses. Only request more under IAM & Admin → Quotas if a deploy reports a quota error.
6. **Edit `deploy/config.sh`.** Set `PROJECT_ID`, and choose a `REGION` that offers Cloud Run L4 GPUs (see Google's [Cloud Run GPU docs](https://cloud.google.com/run/docs/configuring/services/gpu) for the current list; pick the one nearest your users).
7. **Run the setup script** from the repository folder. It turns on the needed Google services, creates the image repository, storage bucket and secrets, and grants permissions. It asks for your Hugging Face token and a site password without displaying them.
   ```bash
   deploy/setup.sh
   ```

## Deploying

```bash
deploy/deploy.sh
```

The script builds the image on Google Cloud Build (the first build downloads PyTorch and the model, so allow 15–30 minutes), then deploys it. It prints the site's address (`https://…run.app`) at the end. Run the same command to ship later changes.

Main Cloud Run settings, all in `deploy/deploy.sh` and `deploy/config.sh`:

- **Website:** 1 CPU, 4 GB memory, **no GPU**, at most one instance, minimum zero. The script checks after every deploy that the website has no GPU, and takes it offline if it does.
- **GPU job:** one L4 GPU, 4 CPUs, 16 GB memory, one task, no automatic retries. Each run has a time limit from the recording's length (15 minutes to 1 hour; a 12-minute talk takes about 3.5 minutes), so a stuck job cannot run on.
- **One transcription at a time.** Recordings queue on the website and show how many are ahead.
- **Recordings and results** are stored in the Cloud Storage bucket, mounted at `/data`, with one copy of each recording: `recordings/.fingerprints.json` keeps each file's SHA-256, and each time an instance starts it merges identical copies and removes leftovers from uploads that never became a run. So is your correction dictionary (`dictionary.json`, with its audio clips in `dictionary-clips/`), so fixes made on the site survive restarts and redeploys.

## Package versions

The image installs `requirements.lock`, which pins every Python package to an exact version, so a rebuild installs what was tested and never a newer release. The checks on GitHub install `requirements-test.lock`, pinned to the same versions.

- **After changing `requirements.txt`:** run `deploy/lock.sh` (it needs [uv](https://docs.astral.sh/uv/)). Packages already pinned keep their versions. A test fails if a package is missing from the lock.
- **To upgrade on purpose:** run `deploy/lock.sh --upgrade` (or `--upgrade-package torch`), run the checks, then deploy. New pins rebuild the image's later layers, so that deploy downloads the models again and takes longer.

## Costs

**What you pay for:**

| Part | When it is billed | Rough cost (Singapore, Nov 2026) |
| --- | --- | --- |
| GPU job | Only while a recording is being transcribed, plus a minute or two to start | about £0.77 an hour |
| Website | While someone is using it, and about 15 minutes after | about £0.07 an hour |
| Images, builds, bucket | Storage, and about 10 minutes of build per deploy | under £2 a month |

Check current rates on Google's [Cloud Run pricing page](https://cloud.google.com/run/pricing).

**Why it is split:** until November 2026 the website itself had the GPU. Any request kept it switched on, and an open tab that asked for the run list every 20 seconds kept a GPU running for days: about 30 billed hours (£23.59) for roughly 3 hours of real transcription. Now the website has no GPU, and the page only asks the server for updates while something is running and you are on the page.

**Guardrails** (`deploy/guardrails.sh`, run by every deploy):

- **Monthly GPU limit:** the site starts no new transcriptions once the GPU job has run `GPU_HOURS_PER_MONTH` hours this month (default 5). The top bar shows this month's GPU time and its estimated cost. Each run's time is logged in the bucket under `usage/<month>/`.
- **Alert emails** (set `ALERT_EMAIL`): when the GPU job runs more than `ALERT_GPU_HOURS_PER_DAY` hours in a day, or the website stays up more than `ALERT_WEB_HOURS_PER_DAY`.
- **Budget and kill switch** (set `BILLING_ACCOUNT`; needs billing-account admin rights): a `BUDGET_GBP` monthly budget with emails at 50%, 90% and 100%. At 100% a small function takes the website offline and cancels any running GPU job.
- **Pause and stop end the GPU job at once.** Pausing, stopping or deleting a run cancels its GPU job; the site keeps cancelling until Google confirms it has ended (for up to 5 minutes), and a website that restarts meanwhile still treats the run as paused and cancels any job left running. Resume starts a new job that carries on from the chunks already done, so nothing is billed while a run is paused.
- **Never from your own computer:** the website uses the GPU job only when it runs on Cloud Run itself. `python app.py` always transcribes on the computer it runs on; the only outside service it calls is Gemini, for translation and summaries.
- **Image cleanup:** only the newest 2 images are kept, and each build reuses the previous image's layers instead of downloading the models again.

**Checking what is running:**

```bash
gcloud run jobs executions list --job gujarati-transcriber-gpu --region asia-southeast1
```

**Taking the site offline by hand** (deploying again brings it back):

```bash
gcloud run services remove-iam-policy-binding gujarati-transcriber --region asia-southeast1 --member=allUsers --role=roles/run.invoker
```

**Bringing it back without a deploy:**

```bash
gcloud run services add-iam-policy-binding gujarati-transcriber --region asia-southeast1 --member=allUsers --role=roles/run.invoker
```

## Rolling back

`deploy/deploy.sh` refuses to run with uncommitted changes, names each image after its commit, and checks `/health` once the new website is up. Only the newest two images are kept, so you can go back one deploy. Data written by a newer version (the talk catalogue, the search index, talk names) is ignored by an older one, so going back loses nothing.

```bash
gcloud run revisions list --service gujarati-transcriber --region asia-southeast1
```

```bash
gcloud run services update-traffic gujarati-transcriber --region asia-southeast1 --to-revisions PREVIOUS_REVISION=100
```

```bash
gcloud run jobs update gujarati-transcriber-gpu --region asia-southeast1 --image asia-southeast1-docker.pkg.dev/gujarati-transcriber-369/gujlish/gujarati-transcriber:PREVIOUS_COMMIT
```

Traffic then stays on that revision, even after later deploys, until you send it back to the newest one (after deploying the fix):

```bash
gcloud run services update-traffic gujarati-transcriber --region asia-southeast1 --to-latest
```

## Translation and summaries online

`deploy/setup.sh` asks for a Gemini API key (pressing Enter keeps the one already stored). When a key is stored, `deploy/deploy.sh` gives it to the service from Secret Manager and switches translation on. Keep the site password on, and set a spending limit on the key.

## Search online

Search works the same as on your computer (see the [README](../README.md#search-every-talk)), built so that a search costs the bucket nothing:

- **The index lives in the website's memory**, about 1.5 to 2 MB per hour of talk, twice that for a moment while it is saved. A thousand hours of talks would hold about 2 GB, and briefly twice that while saving, so raise the website's 4 GB well before the library gets that large.
- **One file in the bucket,** `/data/.search/index.sqlite`, written whole (built on the instance, copied under a temporary name, then renamed) a minute after the last change and at most every ten minutes. A file can't be updated in place in a bucket, so it is never written a piece at a time. A save that fails is tried again later, waiting longer each time (up to half an hour), so a lasting fault doesn't mean a full write every minute.
- **After a cold start** the website reads that one file, compares it with the talk catalogue, re-reads only talks that differ, and then checks each talk's four files in the background for changes the last save missed. Searches meanwhile answer from what is loaded, and the page says when it is still getting ready. If the file can't be read just then, it tries again shortly rather than rebuilding. A damaged file, or one from another version, is rebuilt from the talks.
- **Each change updates one talk:** a word fix, a rename, a new talk, a finished step or a delete. Nothing is rebuilt whole.
- **Costs:** meaning search embeds each passage once with `gemini-embedding-2` (about $0.006 per hour of talk) and each search once you stop typing; a question costs about $0.01 with `gemini-4-hydrogen`. There is no daily limit on questions, so keep the key's spending limit and the budget above.
- **Logs:** a search's words are part of its address (`/api/search?q=…`), so Cloud Run's request log keeps them like any address. Questions are sent in the request body and are not logged.

## Known limitations

- **The website sleeps; the GPU job does not.** The website scales to zero when nobody has the page open, but a GPU job it started keeps going and saves its transcript to the bucket. The run's state file (`.run.json`) records the job, so on the next visit the website follows it again, or carries on with Gujlish if it has finished, instead of starting a second one.
- **The website can stop mid-step.** Cloud Run sometimes replaces it. Each run keeps its state in its folder in the bucket, so the next instance lists it, opens it by the same link and resumes unfinished work from the last saved point. After three restarts in a row it waits for you to press **Resume** instead.
- **Live updates pause.** The page stops asking for updates when the tab is hidden, after 30 minutes without a click or key press, or after repeated failed tries. Clicking or coming back to the tab refreshes it.
- **Start-up time.** Each transcription waits for the GPU job to start and load the model, and the first page after a quiet spell waits for the website to start.
