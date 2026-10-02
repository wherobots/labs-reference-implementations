"""Relay a Wherobots job's HTTPS callback into the Step Functions API.

The job script runs in Wherobots' AWS account and cannot sign calls to
your Step Functions API, so it POSTs plain JSON to this function's
API endpoint instead and we relay:

  {"task_token": "...", "action": "success",   "output": {...}}
  {"task_token": "...", "action": "failure",   "error": "...", "cause": "..."}
  {"task_token": "...", "action": "heartbeat"}

Trust model (reference-grade, documented so you can decide if it fits):
the endpoint (API Gateway HTTP API) is public, throttled at the stage,
and the unguessable, single-use task token is the capability — a request
without a live token can do nothing (Step Functions rejects it). The
token travels to the job as a plain job argument, so anyone who can read
that run's arguments in your Wherobots organization (or the Step
Functions execution history in your AWS account) could post a forged
callback for that one run; both surfaces are already inside your trust
boundary. `output` is therefore treated as untrusted downstream: the
dashboard HTML-escapes it, and this relay caps its size. For production,
add an authorizer (API key / IAM / WAF) in front of the route.

Anonymous callers get generic errors; details go to CloudWatch logs only.
"""

import base64
import json
import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sfn = boto3.client("stepfunctions")

MAX_BODY_BYTES = 262144  # SendTaskSuccess output is capped at 256 KB anyway
_BAD_REQUEST = {"error": "bad request"}


def lambda_handler(event, context):
    try:
        body = event.get("body") or "{}"
        # Enforce the cap in BYTES, before any parsing: len() on a decoded
        # string counts characters, and multibyte UTF-8 would sail under it.
        raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
        if len(raw) > MAX_BODY_BYTES:
            logger.info("rejected oversize callback body (%d bytes)", len(raw))
            return _resp(400, _BAD_REQUEST)
        req = json.loads(raw)
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
            logger.info("rejected unknown action %r", action)
            return _resp(400, _BAD_REQUEST)
        return _resp(200, {"ok": True, "action": action})
    except Exception:
        # Invalid/expired token, malformed body, boto errors: log the detail,
        # hand the anonymous caller nothing to learn from.
        logger.exception("callback relay rejected a request")
        return _resp(400, _BAD_REQUEST)


def _resp(code, obj):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(obj)}
