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

# Recordings and transcripts. The app deletes runs after 24 hours; this lifecycle rule is a backstop.
if ! gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$BUCKET" --location "$REGION" --uniform-bucket-level-access
fi
printf '{"rule":[{"action":{"type":"Delete"},"condition":{"age":2}}]}' >/tmp/gujlish-lifecycle.json
gcloud storage buckets update "gs://$BUCKET" --lifecycle-file /tmp/gujlish-lifecycle.json
rm -f /tmp/gujlish-lifecycle.json

secret() {  # secret NAME VALUE: create or add a new version, without echoing the value
  if gcloud secrets describe "$1" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=-
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy automatic
  fi
}
read -rsp "Hugging Face read token (input hidden): " hf_token; echo
read -rsp "Password people will use to open the site (input hidden, blank = no password): " password; echo
secret gujlish-hf-token "$hf_token"
if [[ -n "$password" ]]; then
  secret gujlish-password "$password"
else
  gcloud secrets delete gujlish-password --quiet >/dev/null 2>&1 || true
  echo "No password: anyone with the link can use the site."
fi
gcloud secrets describe gujlish-secret-key >/dev/null 2>&1 || secret gujlish-secret-key "$(openssl rand -hex 32)"

# The default compute service account runs both the build and the service in new projects.
number=$(gcloud projects describe "$PROJECT_ID" --format 'value(projectNumber)')
account="${number}-compute@developer.gserviceaccount.com"
for name in gujlish-hf-token gujlish-password gujlish-secret-key; do
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
