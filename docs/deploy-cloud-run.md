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
| Visitors see only their own runs | Always | — |
| Upload size limit | 300 MB | `VAANI_MAX_UPLOAD_MB` |
| Recording length limit | 90 minutes | `VAANI_MAX_MINUTES` |
| Recordings and results deleted | Never: kept in your bucket until you delete them | `VAANI_RETENTION_HOURS` |
| One recording in progress per visitor | Always | — |
| Translation and summaries | On when a Gemini key is stored (setup asks for it); Gemini runs on your API key | `VAANI_ALLOW_GEMINI` |
| Large uploads | Sent in 8 MB pieces, because Cloud Run caps one request at 32 MB | — |

Visitors are told apart by a signed cookie. Someone who clears their cookies or switches browsers no longer sees their earlier runs.

## One-time setup (about 20 minutes)

1. **Google Cloud account and project.** At [console.cloud.google.com](https://console.cloud.google.com), create a project and link a billing account. New accounts usually get free trial credit. Note the project ID.
2. **Budget alert.** In Billing → Budgets & alerts, create a budget (for example $10/month) with email alerts, so you hear about unexpected costs early. A budget alerts you; it does not stop the service.
3. **Install the gcloud CLI** and sign in:
   ```bash
   brew install --cask google-cloud-sdk
   gcloud components install beta   # the GPU job runs up to 3 hours, a beta Cloud Run feature
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
- **GPU job:** one L4 GPU, 4 CPUs, 16 GB memory, one task, no automatic retries. Each run has a time limit from the recording's length (30 minutes to 3 hours), so a stuck job cannot run on.
- **One transcription at a time.** Recordings queue on the website and show how many are ahead.
- **Recordings and results** are stored in the Cloud Storage bucket, mounted at `/data`, with one copy of each recording: `recordings/.fingerprints.json` keeps each file's SHA-256, and each time an instance starts it merges identical copies and removes leftovers from uploads that never became a run. So is your correction dictionary (`dictionary.json`, with its audio clips in `dictionary-clips/`), so fixes made on the site survive restarts and redeploys.

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

## Translation and summaries online

`deploy/setup.sh` asks for a Gemini API key (pressing Enter keeps the one already stored). When a key is stored, `deploy/deploy.sh` gives it to the service from Secret Manager and switches translation on. Keep the site password on, and set a spending limit on the key.

## Known limitations

- **The website sleeps; the GPU job does not.** The website scales to zero when nobody has the page open, but a GPU job it started keeps going and saves its transcript to the bucket. The run's state file (`.run.json`) records the job, so on the next visit the website follows it again, or carries on with Gujlish if it has finished, instead of starting a second one.
- **The website can stop mid-step.** Cloud Run sometimes replaces it. Each run keeps its state in its folder in the bucket, so the next instance lists it, opens it by the same link and resumes unfinished work from the last saved point. After three restarts in a row it waits for you to press **Resume** instead.
- **Live updates pause.** The page stops asking for updates when the tab is hidden, after 30 minutes without a click or key press, or after repeated failed tries. Clicking or coming back to the tab refreshes it.
- **Start-up time.** Each transcription waits for the GPU job to start and load the model, and the first page after a quiet spell waits for the website to start.
