#!/usr/bin/env bash
# Build the image on Google Cloud, then deploy:
#   - the GPU job ($GPU_JOB): transcribes one recording on an L4 GPU and exits. It runs only when the website starts it,
#     so the GPU is billed only while a recording is being transcribed.
#   - the website ($SERVICE): CPU only, scales to zero when nobody uses it. No tab, visitor or bot can bill a GPU.
# Then it applies the cost guardrails (deploy/guardrails.sh) and checks that the website has no GPU.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.sh
[[ "$PROJECT_ID" == "your-project-id" ]] && { echo "Set PROJECT_ID in deploy/config.sh first."; exit 1; }
# The image is built from this folder but named after the commit, so a rollback to that name must get that commit.
if [[ -n "$(git status --porcelain)" && "${ALLOW_DIRTY:-}" != "1" ]]; then
  echo "Commit or stash your changes first (the image is named after the commit). ALLOW_DIRTY=1 overrides." >&2
  exit 1
fi

tag="$IMAGE:$(git rev-parse --short HEAD)"
number=$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)')
account="${number}-compute@developer.gserviceaccount.com"  # runs the website and the job
# The bucket holds recordings, transcripts and the GPU job's progress files. Both sides read what the other just
# wrote, so the mount must not cache file listings.
volume="name=data,type=cloud-storage,bucket=$BUCKET,mount-options=metadata-cache-ttl-secs=0"

secrets="VAANI_SECRET_KEY=gujlish-secret-key:latest"
if gcloud secrets describe gujlish-password --project "$PROJECT_ID" >/dev/null 2>&1; then
  secrets="$secrets,VAANI_ACCESS_PASSWORD=gujlish-password:latest"
fi
env_vars="VAANI_MODELS=$MODELS,VAANI_RETENTION_HOURS=0"  # 0 = keep recordings and results
if gcloud secrets describe gujlish-gemini-key --project "$PROJECT_ID" >/dev/null 2>&1; then
  secrets="$secrets,GEMINI_API_KEY=gujlish-gemini-key:latest"
  env_vars="$env_vars,VAANI_ALLOW_GEMINI=1"
fi

gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --config deploy/cloudbuild.yaml \
  --substitutions "_IMAGE=$tag,_CACHE=$IMAGE:latest,_MODELS=$MODELS" .

# The GPU job. One task, no retries (a failed run waits for you to resume it), and a hard time limit; the website
# sets a shorter one for each recording from its length. (Over an hour would be a beta Cloud Run feature; a 3-hour
# talk takes about half an hour.)
gcloud run jobs deploy "$GPU_JOB" --project "$PROJECT_ID" --region "$REGION" --image "$tag" \
  --gpu 1 --gpu-type nvidia-l4 --no-gpu-zonal-redundancy --cpu 4 --memory 16Gi \
  --tasks 1 --parallelism 1 --max-retries 0 --task-timeout 1h \
  --command python --args scripts/run_job.py \
  --add-volume "$volume" --add-volume-mount volume=data,mount-path=/data \
  --set-env-vars "$env_vars" \
  --service-account "$account"
# The website starts and cancels executions of this job (and nothing else).
gcloud run jobs add-iam-policy-binding "$GPU_JOB" --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:$account" --role roles/run.developer >/dev/null

# The website. --gpu 0 removes the GPU the service used to have. --no-cpu-throttling lets Gujlish, translation and
# captions finish between page requests; it costs CPU only, and only while the site is up (it scales to zero).
# --max-instances 1: runs are tracked in memory, so there is a single copy of the site.
gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$tag" \
  --gpu 0 --cpu "$WEB_CPU" --memory "$WEB_MEMORY" --no-cpu-throttling \
  --min-instances 0 --max-instances 1 --concurrency 40 --timeout 3600 \
  --allow-unauthenticated \
  --add-volume "$volume" --add-volume-mount volume=data,mount-path=/data \
  --set-env-vars "$env_vars,VAANI_GPU_JOB=$GPU_JOB,VAANI_PROJECT=$PROJECT_ID,VAANI_REGION=$REGION,VAANI_GPU_HOURS_PER_MONTH=$GPU_HOURS_PER_MONTH,VAANI_GPU_POUNDS_PER_HOUR=$GPU_POUNDS_PER_HOUR" \
  --set-secrets "$secrets" \
  --service-account "$account"

# Refuse to leave a website with a GPU running: that is what billed £23 of idle GPU time in November 2026.
gpus=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
  --format 'value(spec.template.spec.containers[0].resources.limits."nvidia.com/gpu")')
if [[ -n "$gpus" && "$gpus" != "0" ]]; then
  echo "ERROR: the website still has a GPU ($gpus). Taking it offline until this is fixed." >&2
  gcloud run services remove-iam-policy-binding "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --member allUsers --role roles/run.invoker >/dev/null || true
  exit 1
fi
echo "Website has no GPU. Transcriptions run on the $GPU_JOB job."

# Smoke test: the new revision answers (this also starts it, so the first visit after a deploy is quick).
url=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')
if ! curl -fsS --max-time 120 "$url/health" >/dev/null; then
  echo "ERROR: $url/health did not answer. To roll back, see docs/deploy-cloud-run.md, \"Rolling back\"." >&2
  exit 1
fi
echo "Website is up: $url"

deploy/guardrails.sh
