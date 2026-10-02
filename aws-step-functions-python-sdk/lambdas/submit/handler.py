"""Submit a Wherobots job run and return immediately with the run_id.

The SDK is vendored into the deployment zip at deploy time (pinned in
requirements.txt), so cold starts run no network installs and every
instance runs the same code.

The event is the output of the previous Step Functions state:
    {"script": "s3://...", "job_name": "...", "runtime": "tiny"}
Waiting for completion is NOT done here — that belongs to the reusable
poller state machine, which has no 15-minute Lambda ceiling.
"""

import os

import boto3

# The Wherobots API key lives in Secrets Manager, never in the function's
# configuration: fetched once per cold start into the process environment,
# where the SDK reads it. Reading the function config shows only the ARN.
if "WHEROBOTS_API_KEY" not in os.environ:
    os.environ["WHEROBOTS_API_KEY"] = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["WHEROBOTS_API_KEY_SECRET_ARN"]
    )["SecretString"]

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
        # api_key: the SDK reads WHEROBOTS_API_KEY from the process env,
        # populated above from Secrets Manager
    ) as job:
        run_id = job.submit()  # returns in seconds

    # In callback mode this return value is discarded (the state's output
    # comes from SendTaskSuccess); the run_id is recoverable from the
    # deterministic job name via the find_run Lambda on the fallback path.
    return {"run_id": run_id, "input": data}
