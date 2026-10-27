#!/usr/bin/env bash
# One-time Google Cloud setup: APIs, image repository, storage bucket and secrets. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.sh
[[ "$PROJECT_ID" == "your-project-id" ]] && { echo "Set PROJECT_ID in deploy/config.sh first."; exit 1; }

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com

gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION"

# Recordings and transcripts. They are kept until you delete them (no automatic deletion).
if ! gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$BUCKET" --location "$REGION" --uniform-bucket-level-access
fi
gcloud storage buckets update "gs://$BUCKET" --clear-lifecycle >/dev/null

secret() {  # secret NAME VALUE: create or add a new version, without echoing the value
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=-
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy automatic
  fi
}
ask() {  # ask NAME PROMPT: hidden prompt; blank keeps the stored value when there is one
  local value keep=""
  gcloud secrets describe "$1" >/dev/null 2>&1 && keep=" (blank = keep the current one)"
  read -rsp "$2$keep: " value; echo
  if [[ -n "$value" ]]; then secret "$1" "$value"; elif [[ -z "$keep" ]]; then return 1; fi
}
ask gujlish-hf-token "Hugging Face read token (input hidden)" || { echo "A Hugging Face token is required."; exit 1; }
ask gujlish-password "Password for opening the site (input hidden)" ||
  echo "No password: anyone with the link can use the site."
ask gujlish-gemini-key "Gemini API key for translation and summaries (input hidden)" ||
  echo "No Gemini key: translation and summaries stay off on the site."
gcloud secrets describe gujlish-secret-key >/dev/null 2>&1 || secret gujlish-secret-key "$(openssl rand -hex 32)"

# The default compute service account runs both the build and the service in new projects.
number=$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)')
account="${number}-compute@developer.gserviceaccount.com"
for name in gujlish-hf-token gujlish-password gujlish-gemini-key gujlish-secret-key; do
  gcloud secrets describe "$name" >/dev/null 2>&1 || continue
  gcloud secrets add-iam-policy-binding "$name" --member "serviceAccount:$account" \
    --role roles/secretmanager.secretAccessor >/dev/null
done
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member "serviceAccount:$account" \
  --role roles/storage.objectUser >/dev/null
for role in roles/artifactregistry.writer roles/logging.logWriter roles/storage.objectViewer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$account" --role "$role" \
    --condition None >/dev/null
done
echo "Setup finished. Next: deploy/deploy.sh"
