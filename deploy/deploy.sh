#!/usr/bin/env bash
# Build the image on Google Cloud and deploy it to Cloud Run with one L4 GPU that scales to zero when idle.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.sh
[[ "$PROJECT_ID" == "your-project-id" ]] && { echo "Set PROJECT_ID in deploy/config.sh first."; exit 1; }
tag="$IMAGE:$(git rev-parse --short HEAD)"
secrets="GUJLISH_SECRET_KEY=gujlish-secret-key:latest"
if gcloud secrets describe gujlish-password --project "$PROJECT_ID" >/dev/null 2>&1; then
  secrets="$secrets,GUJLISH_ACCESS_PASSWORD=gujlish-password:latest"
fi
env_vars="GUJLISH_MODELS=$MODELS,GUJLISH_RETENTION_HOURS=0"  # 0 = keep recordings and results
if gcloud secrets describe gujlish-gemini-key --project "$PROJECT_ID" >/dev/null 2>&1; then
  secrets="$secrets,GEMINI_API_KEY=gujlish-gemini-key:latest"
  env_vars="$env_vars,GUJLISH_ALLOW_GEMINI=1"
fi

gcloud builds submit --project "$PROJECT_ID" --region "$REGION" --config deploy/cloudbuild.yaml \
  --substitutions "_IMAGE=$tag,_MODELS=$MODELS" .

# --max-instances 1: one GPU at most, so the bill can't multiply and in-memory job tracking stays correct.
# --no-cpu-throttling: transcription runs in the background between browser requests (GPUs require it anyway).
gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$tag" \
  --gpu 1 --gpu-type nvidia-l4 --no-gpu-zonal-redundancy --cpu 4 --memory 16Gi --no-cpu-throttling \
  --min-instances 0 --max-instances 1 --concurrency 40 --timeout 3600 \
  --allow-unauthenticated \
  --add-volume name=data,type=cloud-storage,bucket="$BUCKET" --add-volume-mount volume=data,mount-path=/data \
  --set-env-vars "$env_vars" \
  --set-secrets "$secrets"
