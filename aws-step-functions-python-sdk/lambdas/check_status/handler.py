"""Check the status of an already-submitted Wherobots job run.

Uses WherobotsJob.from_run_id() — the SDK's designed attach point for an
existing run: no script required, read-only (submit() is disabled on the
returned instance), and JobStatus.is_terminal replaces any hand-rolled
terminal-state set.

Invoked in a loop by the wherobots-job-poller state machine every
poll_seconds. Cheap: warm invocations are ~1s at 256 MB.

Event:  {"run_id": "..."}
Return: {"run_id": "...", "status": "RUNNING", "is_terminal": false}
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
    job = WherobotsJob.from_run_id(event["run_id"])
    status = job.get_status().status  # JobStatus enum

    return {
        "run_id": event["run_id"],
        "status": status.value,
        "is_terminal": status.is_terminal,
    }
