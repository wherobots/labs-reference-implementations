"""Relay a Wherobots job's HTTPS callback into the Step Functions API.

The job script runs in Wherobots' AWS account and cannot sign calls to
your Step Functions API, so it POSTs plain JSON to this function's
API endpoint instead and we relay:

  {"task_token": "...", "action": "success",   "output": {...}}
  {"task_token": "...", "action": "failure",   "error": "...", "cause": "..."}
  {"task_token": "...", "action": "heartbeat"}

Auth model (reference-grade): the endpoint (API Gateway HTTP API) is public and
the unguessable, single-use task token is the capability — a request
without a live token can do nothing. For production put this behind API
Gateway with an API key or IAM auth.
"""

import base64
import json

import boto3

sfn = boto3.client("stepfunctions")


def lambda_handler(event, context):
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()
        req = json.loads(body)
        token = req["task_token"]
        action = req.get("action", "heartbeat")

        if action == "success":
            sfn.send_task_success(taskToken=token, output=json.dumps(req.get("output", {})))
        elif action == "failure":
            sfn.send_task_failure(
                taskToken=token,
                error=str(req.get("error", "JobFailed"))[:256],
                cause=str(req.get("cause", ""))[:32768],
            )
        elif action == "heartbeat":
            sfn.send_task_heartbeat(taskToken=token)
        else:
            return _resp(400, {"error": f"unknown action '{action}'"})
        return _resp(200, {"ok": True, "action": action})
    except Exception as exc:  # invalid/expired token, malformed body, ...
        return _resp(400, {"error": str(exc)})


def _resp(code, obj):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(obj)}
