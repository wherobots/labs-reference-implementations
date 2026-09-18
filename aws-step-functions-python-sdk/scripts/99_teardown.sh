#!/usr/bin/env bash
# Teardown — delete everything 02_deploy.sh created (and nothing else).
# Only resources named with $RESOURCE_PREFIX are touched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[ -f .env ] || { echo "ERROR: .env not found."; exit 1; }
set -a; source .env; set +a
: "${AWS_REGION:=us-west-2}"
: "${RESOURCE_PREFIX:=wherobots-sfn-ref}"
export AWS_DEFAULT_REGION="$AWS_REGION"
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE; do
  [ -z "${!v:-}" ] && unset "$v"
done

# Only a confirmed not-found is "already gone". Any other failure
# (AccessDenied, dependency conflict, throttling) is reported loudly and
# marks the teardown as incomplete — the resource may still exist and bill.
FAILED=0
run() {
  echo; echo "▶ $*"
  local out
  if out=$("$@" 2>&1); then
    # (plain 'test && echo' would return falsy on empty output and, under
    # set -e, abort the script after a perfectly successful delete)
    if [ -n "$out" ]; then echo "$out"; fi
  elif echo "$out" | grep -qiE "NoSuchEntity|ResourceNotFound|NotFoundException|StateMachineDoesNotExist|Function not found|does not exist|cannot be found"; then
    echo "  (already gone — ok)"
  else
    echo "$out"
    echo "  ✗ step failed — this resource may still exist"
    FAILED=1
  fi
}

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "── State machines ─────────────────────────────────────────────────────"
for sm in "${RESOURCE_PREFIX}-pipeline" "${RESOURCE_PREFIX}-job-poller"; do
  run aws stepfunctions delete-state-machine \
    --state-machine-arn "arn:aws:states:${AWS_REGION}:${ACCOUNT_ID}:stateMachine:${sm}"
done

echo
echo "── API Gateway (callback relay endpoint) ──────────────────────────────"
API_ID=$(aws apigatewayv2 get-apis --query "Items[?Name=='${RESOURCE_PREFIX}-callback'].ApiId | [0]" --output text)
if [ "$API_ID" != "None" ] && [ -n "$API_ID" ]; then
  run aws apigatewayv2 delete-api --api-id "$API_ID"
fi

echo
echo "── Lambda functions ───────────────────────────────────────────────────"
for fn in "${RESOURCE_PREFIX}-submit" "${RESOURCE_PREFIX}-check-status" \
          "${RESOURCE_PREFIX}-callback-relay" "${RESOURCE_PREFIX}-find-run"; do
  run aws lambda delete-function --function-name "$fn"
done

echo
echo "── IAM roles ──────────────────────────────────────────────────────────"
run aws iam delete-role-policy --role-name "${RESOURCE_PREFIX}-lambda-role" \
  --policy-name "${RESOURCE_PREFIX}-sendtask-policy"
run aws iam detach-role-policy --role-name "${RESOURCE_PREFIX}-lambda-role" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
run aws iam delete-role --role-name "${RESOURCE_PREFIX}-lambda-role"
run aws iam delete-role-policy --role-name "${RESOURCE_PREFIX}-relay-role" \
  --policy-name "${RESOURCE_PREFIX}-sendtask-policy"
run aws iam detach-role-policy --role-name "${RESOURCE_PREFIX}-relay-role" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
run aws iam delete-role --role-name "${RESOURCE_PREFIX}-relay-role"
run aws iam delete-role-policy --role-name "${RESOURCE_PREFIX}-sfn-role" \
  --policy-name "${RESOURCE_PREFIX}-sfn-policy"
run aws iam delete-role --role-name "${RESOURCE_PREFIX}-sfn-role"

if [ "$FAILED" -ne 0 ]; then
  echo
  echo "❌ Teardown INCOMPLETE — one or more delete calls failed (see above)."
  echo "   Local state is kept so a re-run can finish the job."
  exit 1
fi

echo
echo "── Local state ────────────────────────────────────────────────────────"
run rm -f .state.json lambdas/*.zip

echo
echo "✅ Teardown complete. (The uploaded job script in Wherobots storage is"
echo "   untouched — remove it from the Wherobots console if you want.)"
