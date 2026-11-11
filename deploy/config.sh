# Settings shared by setup.sh, deploy.sh and guardrails.sh. Edit these, then run the scripts from the repository root.
PROJECT_ID="${PROJECT_ID:-gujarati-transcriber-369}"
REGION="${REGION:-asia-southeast1}"         # Singapore: closest Cloud Run L4 GPU region to India
SERVICE="${SERVICE:-gujarati-transcriber}"  # the website: CPU only, never a GPU
GPU_JOB="${GPU_JOB:-${SERVICE}-gpu}"        # the Cloud Run job that transcribes one recording on an L4 GPU, then exits
MODELS="${MODELS:-flex}"                    # personal use behind a password; serving others needs Bodhan AI's approval
BUCKET="${BUCKET:-${PROJECT_ID}-gujlish-data}"
REPO="${REPO:-gujlish}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}"

# Cost guardrails (see docs/deploy-cloud-run.md, "Costs").
WEB_CPU="${WEB_CPU:-1}"                     # the website's CPU and memory (the word-timing model needs ~2 GiB)
WEB_MEMORY="${WEB_MEMORY:-4Gi}"
GPU_HOURS_PER_MONTH="${GPU_HOURS_PER_MONTH:-5}"    # the site starts no new transcriptions past this (0 = no limit)
GPU_POUNDS_PER_HOUR="${GPU_POUNDS_PER_HOUR:-0.77}" # for the site's cost estimate (L4 + 4 vCPU + 16 GiB, Nov 2026 bill)
ALERT_EMAIL="${ALERT_EMAIL:-}"              # where cost alerts go; set it to get alert emails (blank = no alerts)
ALERT_GPU_HOURS_PER_DAY="${ALERT_GPU_HOURS_PER_DAY:-2}"   # email when the GPU job runs longer than this in a day
ALERT_WEB_HOURS_PER_DAY="${ALERT_WEB_HOURS_PER_DAY:-8}"   # email when the website stays up longer than this in a day
BILLING_ACCOUNT="${BILLING_ACCOUNT:-}"      # billing account id (XXXXXX-XXXXXX-XXXXXX) for the budget; blank = skip
BUDGET_GBP="${BUDGET_GBP:-10}"              # monthly budget; at 100% the kill switch takes the site offline
