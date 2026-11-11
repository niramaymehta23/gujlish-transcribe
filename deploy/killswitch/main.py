"""Budget kill switch (deployed by deploy/guardrails.sh as a Cloud Run function on the budget's Pub/Sub topic).

When the month's cost reaches the budget it takes the website offline (removes public access, as by hand on
11 Nov 2026) and cancels any GPU job still running. Nothing is deleted. To bring the site back, run deploy/deploy.sh,
or the add-iam-policy-binding command in docs/deploy-cloud-run.md once you have raised the budget.
"""

import base64
import json
import os
import urllib.error
import urllib.request

import functions_framework

API = "https://run.googleapis.com/v2"
TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


def _token() -> str:
    request = urllib.request.Request(TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())["access_token"]


def _call(method: str, url: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def take_offline(project: str, region: str, service: str, job: str) -> list[str]:
    """Remove public access from the website and cancel running GPU executions; return what was done."""
    done = []
    resource = f"{API}/projects/{project}/locations/{region}/services/{service}"
    policy = _call("GET", f"{resource}:getIamPolicy")
    bindings = policy.get("bindings", [])
    for binding in bindings:
        if binding.get("role") == "roles/run.invoker" and "allUsers" in binding.get("members", []):
            binding["members"] = [m for m in binding["members"] if m != "allUsers"]
            done.append("removed public access from the website")
    policy["bindings"] = [b for b in bindings if b.get("members")]
    if done:
        _call("POST", f"{resource}:setIamPolicy", {"policy": policy})
    executions = _call("GET", f"{API}/projects/{project}/locations/{region}/jobs/{job}/executions")
    for execution in executions.get("executions", []):
        if not execution.get("completionTime"):
            try:
                _call("POST", f"{API}/{execution['name']}:cancel", {})
                done.append(f"cancelled {execution['name'].rsplit('/', 1)[-1]}")
            except urllib.error.HTTPError:
                pass  # finished meanwhile
    return done


@functions_framework.cloud_event
def stop_spending(cloud_event):
    message = json.loads(base64.b64decode(cloud_event.data["message"]["data"]))
    cost, budget = float(message.get("costAmount", 0)), float(message.get("budgetAmount", 0))
    if not budget or cost < budget:
        print(f"Spent {cost:.2f} of {budget:.2f}: nothing to do.")
        return
    done = take_offline(os.environ["PROJECT_ID"], os.environ["REGION"], os.environ["SERVICE"], os.environ["GPU_JOB"])
    print(f"Budget used up ({cost:.2f} of {budget:.2f}): {'; '.join(done) or 'already offline'}.")
