# Settings shared by setup.sh and deploy.sh. Edit these, then run the scripts from the repository root.
PROJECT_ID="${PROJECT_ID:-your-project-id}"
REGION="${REGION:-us-central1}"             # must offer Cloud Run L4 GPUs; see the GPU docs for the current list
SERVICE="${SERVICE:-gujarati-transcriber}"
MODELS="${MODELS:-gujlish}"                 # add flex only once Bodhan AI has approved hosting it
BUCKET="${BUCKET:-${PROJECT_ID}-gujlish-data}"
REPO="${REPO:-gujlish}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}"
