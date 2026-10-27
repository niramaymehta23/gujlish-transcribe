# Settings shared by setup.sh and deploy.sh. Edit these, then run the scripts from the repository root.
PROJECT_ID="${PROJECT_ID:-gujarati-transcriber-369}"
REGION="${REGION:-asia-southeast1}"         # Singapore: closest Cloud Run L4 GPU region to India
SERVICE="${SERVICE:-gujarati-transcriber}"
MODELS="${MODELS:-gujlish}"                 # add flex only once Bodhan AI has approved hosting it
BUCKET="${BUCKET:-${PROJECT_ID}-gujlish-data}"
REPO="${REPO:-gujlish}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}"
