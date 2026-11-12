#!/usr/bin/env bash
# Cost guardrails for the hosted site. Run by deploy.sh after every deploy; safe to re-run on its own.
#   1. Old images are deleted automatically (only the newest 2 are kept).
#   2. Email alerts (ALERT_EMAIL) when the GPU job or the website runs longer than expected in a day.
#   3. A monthly budget (BILLING_ACCOUNT, BUDGET_GBP) with emails at 50%, 90% and 100%. At 100% a small kill-switch
#      function takes the website offline and cancels any running GPU job, so the bill cannot keep growing.
# The site itself also stops starting transcriptions once GPU_HOURS_PER_MONTH is reached (web/usage.py).
set -euo pipefail
cd "$(dirname "$0")/.."
source deploy/config.sh

# 1. Image cleanup: keep the newest 2 images, delete the rest (41 GB of old images had piled up by Nov 2026).
policy=$(mktemp)
cat > "$policy" <<'JSON'
[
  {"name": "keep-newest-2", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
  {"name": "delete-older", "action": {"type": "Delete"}, "condition": {"tagState": "ANY", "olderThan": "1d"}}
]
JSON
gcloud artifacts repositories set-cleanup-policies "$REPO" --project "$PROJECT_ID" --location "$REGION" \
  --policy "$policy" --no-dry-run >/dev/null
rm -f "$policy"
echo "Image cleanup: newest 2 images kept."

api() {  # api METHOD URL [JSON]: call a Google API as the signed-in gcloud user
  curl -sS --fail-with-body -X "$1" "$2" -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "Content-Type: application/json" ${3:+-d "$3"}
}
monitoring="https://monitoring.googleapis.com/v3/projects/$PROJECT_ID"

# 2. Alerts. Each is one policy, replaced on every run so a changed threshold takes effect.
if [[ -n "$ALERT_EMAIL" ]]; then
  gcloud services enable monitoring.googleapis.com --project "$PROJECT_ID"
  channel=$(api GET "$monitoring/notificationChannels" | python3 -c "
import json, sys
email = sys.argv[1]
print(next((c['name'] for c in json.load(sys.stdin).get('notificationChannels', [])
            if c.get('labels', {}).get('email_address') == email), ''))" "$ALERT_EMAIL")
  if [[ -z "$channel" ]]; then
    channel=$(api POST "$monitoring/notificationChannels" \
      "{\"type\": \"email\", \"displayName\": \"HariSena Vaani Studio costs\", \"labels\": {\"email_address\": \"$ALERT_EMAIL\"}}" |
      python3 -c "import json, sys; print(json.load(sys.stdin)['name'])")
  fi
  alert() {  # alert NAME FILTER SECONDS: email when the summed billable time over a day goes above SECONDS
    local existing
    existing=$(api GET "$monitoring/alertPolicies" | python3 -c "
import json, sys
print(' '.join(p['name'] for p in json.load(sys.stdin).get('alertPolicies', []) if p.get('displayName') == sys.argv[1]))" "$1")
    for name in $existing; do api DELETE "https://monitoring.googleapis.com/v3/$name" >/dev/null; done
    alert_policy "$1" "$2" "$3" "$channel" | api POST "$monitoring/alertPolicies" @- >/dev/null
  }
  # The policy is written by Python from a heredoc and piped to curl: macOS's bash 3.2 splits a long quoted Python
  # program apart when it sits inside "$( )".
  alert_policy() {
    python3 - "$@" <<'PY'
import json, sys
name, metric_filter, seconds, channel = sys.argv[1:]
print(json.dumps({
    "displayName": name,
    "combiner": "OR",
    "notificationChannels": [channel],
    "documentation": {"content": 'See docs/deploy-cloud-run.md, "Costs", for how to check and stop it.',
                      "mimeType": "text/markdown"},
    "conditions": [{
        "displayName": name,
        "conditionThreshold": {
            "filter": metric_filter,
            "aggregations": [{"alignmentPeriod": "86400s", "perSeriesAligner": "ALIGN_SUM",
                              "crossSeriesReducer": "REDUCE_SUM"}],
            "comparison": "COMPARISON_GT",
            "thresholdValue": float(seconds),
            "duration": "0s",
        },
    }],
}))
PY
  }
  metric='metric.type="run.googleapis.com/container/billable_instance_time"'
  alert "GPU job ran over ${ALERT_GPU_HOURS_PER_DAY} h today" \
    "$metric AND resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"$GPU_JOB\"" \
    "$((ALERT_GPU_HOURS_PER_DAY * 3600))"
  alert "Website was up over ${ALERT_WEB_HOURS_PER_DAY} h today" \
    "$metric AND resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$SERVICE\"" \
    "$((ALERT_WEB_HOURS_PER_DAY * 3600))"
  echo "Alerts: emails go to the address in ALERT_EMAIL."
else
  echo "Alerts: skipped (set ALERT_EMAIL in deploy/config.sh to get cost alert emails)."
fi

# 3. Budget and kill switch.
if [[ -z "$BILLING_ACCOUNT" ]]; then
  echo "Budget: skipped (set BILLING_ACCOUNT in deploy/config.sh to add the £$BUDGET_GBP budget and its kill switch)."
  exit 0
fi
gcloud services enable pubsub.googleapis.com cloudfunctions.googleapis.com eventarc.googleapis.com \
  billingbudgets.googleapis.com cloudbuild.googleapis.com --project "$PROJECT_ID"
topic="gujlish-budget"
gcloud pubsub topics describe "$topic" --project "$PROJECT_ID" >/dev/null 2>&1 ||
  gcloud pubsub topics create "$topic" --project "$PROJECT_ID" >/dev/null

# The kill switch runs as its own account, allowed to change the website's access and cancel the GPU job.
killer="gujlish-killswitch@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$killer" --project "$PROJECT_ID" >/dev/null 2>&1 ||
  gcloud iam service-accounts create gujlish-killswitch --project "$PROJECT_ID" \
    --display-name "Takes the site offline when the budget is used up" >/dev/null
gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:$killer" --role roles/run.admin >/dev/null
gcloud run jobs add-iam-policy-binding "$GPU_JOB" --project "$PROJECT_ID" --region "$REGION" \
  --member "serviceAccount:$killer" --role roles/run.developer >/dev/null
gcloud functions deploy gujlish-killswitch --gen2 --project "$PROJECT_ID" --region "$REGION" \
  --runtime python312 --source deploy/killswitch --entry-point stop_spending --trigger-topic "$topic" \
  --service-account "$killer" --memory 256Mi --max-instances 1 \
  --set-env-vars "PROJECT_ID=$PROJECT_ID,REGION=$REGION,SERVICE=$SERVICE,GPU_JOB=$GPU_JOB" >/dev/null

name="HariSena Vaani Studio monthly budget"
existing=$(gcloud billing budgets list --billing-account "$BILLING_ACCOUNT" \
  --filter "displayName=\"$name\"" --format 'value(name)' | head -1)
rules=(--threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0)
if [[ -n "$existing" ]]; then
  gcloud billing budgets update "$existing" --billing-account "$BILLING_ACCOUNT" \
    --budget-amount "${BUDGET_GBP}GBP" --clear-threshold-rules "${rules[@]/--threshold-rule/--add-threshold-rule}" \
    --notifications-rule-pubsub-topic "projects/$PROJECT_ID/topics/$topic" >/dev/null
else
  gcloud billing budgets create --billing-account "$BILLING_ACCOUNT" --display-name "$name" \
    --budget-amount "${BUDGET_GBP}GBP" --filter-projects "projects/$PROJECT_ID" "${rules[@]}" \
    --notifications-rule-pubsub-topic "projects/$PROJECT_ID/topics/$topic" >/dev/null
fi
echo "Budget: £$BUDGET_GBP a month, emails at 50/90/100%; at 100% the kill switch takes the site offline."
