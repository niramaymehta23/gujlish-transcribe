# Putting the transcriber online with Google Cloud Run

This runs the web app on Google Cloud Run with one NVIDIA L4 GPU that **scales to zero**: when nobody is using the site, nothing runs and nothing is billed. The first visitor after a quiet spell waits for the machine to start and the model to load (expect around a minute or two); after that it is fast.

## Before you start: the Flex license

Flex's license (the Bodhan AI Open Model License, in the model repository) requires Bodhan AI's **prior written approval** before you host Flex for other people ("Third-Party Hosting", section 3). Exceptions include registered nonprofits, trusts and societies using it for their own charitable purpose, and academic research use (section 13). If you host Flex, the site must also show "Built with Indic-Transcribe Flex from Bodhan AI / AI4Bharat." The app does this automatically whenever Flex is enabled.

The deployment therefore offers only **Gujlish** (Apache 2.0) by default. Add Flex by setting `MODELS="flex,gujlish"` in `deploy/config.sh` once you have approval or are sure an exception applies. This note is not legal advice.

## What the hosted site does differently

`GUJLISH_HOSTED=1` (set in the Docker image) switches on:

| Behavior | Default when hosted | Change with |
| --- | --- | --- |
| Password for the whole site | Whatever you entered in setup, or none | `deploy/setup.sh` |
| Visitors see only their own runs | Always | — |
| Upload size limit | 300 MB | `GUJLISH_MAX_UPLOAD_MB` |
| Recording length limit | 90 minutes | `GUJLISH_MAX_MINUTES` |
| Recordings and results deleted | After 24 hours (the bucket also deletes anything older than 2 days) | `GUJLISH_RETENTION_HOURS` |
| One recording in progress per visitor | Always | — |
| Translation and summaries | Off: Claude and Gemini would run on your API key | `GUJLISH_ALLOW_CLOUD_LLM=1` plus a key (see below) |
| Large uploads | Sent in 8 MB pieces, because Cloud Run caps one request at 32 MB | — |

Visitors are told apart by a signed cookie. Someone who clears their cookies or switches browsers no longer sees their earlier runs.

## One-time setup (about 20 minutes)

1. **Google Cloud account and project.** At [console.cloud.google.com](https://console.cloud.google.com), create a project and link a billing account. New accounts usually get free trial credit. Note the project ID.
2. **Budget alert.** In Billing → Budgets & alerts, create a budget (for example $10/month) with email alerts, so you hear about unexpected costs early. A budget alerts you; it does not stop the service.
3. **Install the gcloud CLI** and sign in:
   ```bash
   brew install --cask google-cloud-sdk
   gcloud auth login
   ```
4. **Hugging Face token.** At [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens), create a token with **read** access. The build uses it to download the models.
5. **GPU quota.** Cloud Run GPUs may need a quota increase in a new project. If the first deploy reports a quota error, request "Total Nvidia L4 GPU allocation without zonal redundancy, per project per region" for your region under IAM & Admin → Quotas, then deploy again.
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

Main Cloud Run settings, all in `deploy/deploy.sh`:

- **One L4 GPU, 4 CPUs, 16 GB memory**, at most **one instance**, so the bill can't multiply and the in-memory job list stays correct.
- **Minimum zero instances**, so you pay nothing while idle. Setting a minimum of 1 removes cold starts but bills around the clock.
- **Recordings and results** are stored in the Cloud Storage bucket, mounted at `/data`.

## Costs

You pay for the GPU instance while it runs: while it works, and for a short idle period afterwards before it scales to zero. Smaller charges cover storage, builds and the image repository. Check current rates on Google's [Cloud Run pricing page](https://cloud.google.com/run/pricing) for your region. As a rough guide, an L4 GPU instance costs on the order of a dollar per hour while running.

## Optional: translation and summaries online

To let visitors translate and summarize with Gemini or Claude on **your** API key:

1. Store the key as a secret and allow the service to read it, for example:
   ```bash
   printf '%s' "$GEMINI_API_KEY" | gcloud secrets create gujlish-gemini-key --data-file=-
   gcloud secrets add-iam-policy-binding gujlish-gemini-key \
     --member "serviceAccount:$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)')-compute@developer.gserviceaccount.com" \
     --role roles/secretmanager.secretAccessor
   ```
2. Add `GUJLISH_ALLOW_CLOUD_LLM=1` to `--set-env-vars` and `GEMINI_API_KEY=gujlish-gemini-key:latest` to the secrets in `deploy/deploy.sh`, then deploy again.

Keep the site password on if you do this, and set a spending limit on the API key itself.

## Known limitations

- **Closing the tab mid-job.** The browser's progress polling keeps the instance busy. If the only visitor closes the tab during a long transcription, Cloud Run may shut the instance down when it decides it is idle, which stops that job. It can be resumed with Retry. A proper fix moves transcription to a background job service.
- **One job at a time.** Visitors queue behind each other and see how many recordings are ahead of theirs.
- **Cold starts.** The first request after an idle period waits for the instance and model to load.
