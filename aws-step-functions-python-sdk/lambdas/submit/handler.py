"""Submit a Wherobots job run and return immediately with the run_id.

Installs wherobots-python-sdk from PyPI into /tmp on cold start so the
deployment needs no layers or containers (reference pattern — bake a
Lambda layer for production).

The event is the output of the previous Step Functions state:
    {"script": "s3://...", "job_name": "...", "runtime": "tiny"}
Waiting for completion is NOT done here — that belongs to the reusable
poller state machine, which has no 15-minute Lambda ceiling.
"""

import os
import subprocess
import sys

SDK_DIR = "/tmp/sdk"
if not os.path.exists(SDK_DIR):
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "wherobots-python-sdk", "--target", SDK_DIR]
    )
sys.path.insert(0, SDK_DIR)

from wherobots import WherobotsJob  # noqa: E402


def lambda_handler(event, context):
    # Callback mode: the state machine passes {"input": <execution input>,
    # "task_token": <$$.Task.Token>}. The token + relay URL travel to the
    # job as plain args; the job posts success/failure/heartbeats back.
    data = event.get("input", event)
    args = []
    task_token = event.get("task_token")
    callback_url = os.environ.get("CALLBACK_URL")
    if task_token and callback_url:
        args += ["--task-token", task_token, "--callback-url", callback_url]
    if data.get("force_fail"):
        args += ["--fail", "1"]
    if data.get("hard_fail"):
        args += ["--hard-fail", "1"]

    with WherobotsJob(
        script=data["script"],  # s3:// URI — already uploaded, no auto-upload
        name=data["job_name"],  # 8-255 chars, [a-zA-Z0-9_\-.]+
        runtime=data.get("runtime", "tiny"),
        region=os.environ.get("WHEROBOTS_REGION", "aws-us-west-2"),
        args=args,
        # api_key is read from the WHEROBOTS_API_KEY env var on this function
    ) as job:
        run_id = job.submit()  # returns in seconds

    # In callback mode this return value is discarded (the state's output
    # comes from SendTaskSuccess); the run_id is recoverable from the
    # deterministic job name via the find_run Lambda on the fallback path.
    return {"run_id": run_id, "input": data}
