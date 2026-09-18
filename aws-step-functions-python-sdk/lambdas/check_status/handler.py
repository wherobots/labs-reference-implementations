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
    job = WherobotsJob.from_run_id(event["run_id"])
    status = job.get_status().status  # JobStatus enum

    return {
        "run_id": event["run_id"],
        "status": status.value,
        "is_terminal": status.is_terminal,
    }
