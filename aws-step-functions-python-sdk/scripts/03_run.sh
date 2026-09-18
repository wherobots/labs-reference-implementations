#!/usr/bin/env bash
# Step 3 — start one pipeline execution and follow it to completion.
#
# Starts the parent state machine with the uploaded script URI from
# .state.json, then polls describe-execution until it reaches a terminal
# state, printing the status on every poll.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[ -f .env ] || { echo "ERROR: .env not found."; exit 1; }
[ -f .state.json ] || { echo "ERROR: .state.json not found — run 01_upload_job.py and 02_deploy.sh first."; exit 1; }
set -a; source .env; set +a
: "${AWS_REGION:=us-west-2}"
: "${JOB_RUNTIME:=tiny}"
export AWS_DEFAULT_REGION="$AWS_REGION"
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE; do
  [ -z "${!v:-}" ] && unset "$v"
done

PIPELINE_ARN=$(python3 -c "import json; print(json.load(open('.state.json'))['pipeline_arn'])")
SCRIPT_URI=$(python3 -c "import json; print(json.load(open('.state.json'))['script_uri'])")
JOB_NAME="sfn-ref-$(date +%Y%m%d-%H%M%S)"
INPUT=$(printf '{"script": "%s", "job_name": "%s", "runtime": "%s"}' "$SCRIPT_URI" "$JOB_NAME" "$JOB_RUNTIME")

echo "▶ aws stepfunctions start-execution"
echo "  input: $INPUT"
EXECUTION_ARN=$(aws stepfunctions start-execution \
  --state-machine-arn "$PIPELINE_ARN" \
  --name "$JOB_NAME" \
  --input "$INPUT" \
  --query executionArn --output text)
echo "  execution: $EXECUTION_ARN"
echo
echo "Console: https://${AWS_REGION}.console.aws.amazon.com/states/home?region=${AWS_REGION}#/v2/executions/details/${EXECUTION_ARN}"
echo "Wherobots Workload History: https://cloud.wherobots.com/workloads"
echo

while true; do
  STATUS=$(aws stepfunctions describe-execution --execution-arn "$EXECUTION_ARN" --query status --output text)
  echo "$(date +%H:%M:%S)  pipeline: $STATUS"
  case "$STATUS" in
    SUCCEEDED)
      echo
      echo "Pipeline output (what NextStep received):"
      aws stepfunctions describe-execution --execution-arn "$EXECUTION_ARN" --query output --output text
      echo "✅ SUCCESS"
      exit 0 ;;
    FAILED|TIMED_OUT|ABORTED)
      echo "❌ pipeline ended: $STATUS — inspect the execution in the console link above"
      exit 1 ;;
  esac
  sleep 15
done
