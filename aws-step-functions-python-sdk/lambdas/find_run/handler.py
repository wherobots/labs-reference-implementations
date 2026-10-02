"""Resolve a Wherobots run_id from its job name.

Used only on the FALLBACK path: when the callback never arrives (hard
OOM, cluster death — nothing in the job survives to report), the state
machine's heartbeat timeout fires and this Lambda recovers the run_id so
the reusable poller can ask Wherobots what actually happened. The job
name is deterministic (it's in the execution input), which is what makes
this recovery possible.

Event:  {"job_name": "sfn-ref-20260821-101500"}
Return: {"run_id": "...", "status": "..."}
"""

import os

import boto3

# API key from Secrets Manager at cold start — never in the function config.
if "WHEROBOTS_API_KEY" not in os.environ:
    os.environ["WHEROBOTS_API_KEY"] = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["WHEROBOTS_API_KEY_SECRET_ARN"]
    )["SecretString"]

from wherobots import WherobotsJob  # noqa: E402  (vendored into the zip at deploy time)


def lambda_handler(event, context):
    job_name = event["job_name"]
    page = WherobotsJob.list_runs(name_pattern=job_name, size=10)
    runs = sorted(page.items, key=lambda r: r.create_time or "", reverse=True)
    if not runs:
        raise RuntimeError(f"no Wherobots run found with name '{job_name}'")
    newest = runs[0]
    return {"run_id": newest.id, "status": newest.status.value if newest.status else None}
