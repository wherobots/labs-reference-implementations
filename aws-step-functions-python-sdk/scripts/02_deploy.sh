#!/usr/bin/env bash
# Step 2 — build the AWS infrastructure with plain AWS CLI calls.
#
# Creates (all names start with $RESOURCE_PREFIX):
#   1 Secrets Manager secret            (the Wherobots API key — the Lambdas
#                                        read it at cold start; it is never
#                                        stored in a function's configuration)
#   1 IAM role for the Lambdas          (basic execution + GetSecretValue on
#                                        that one secret)
#   1 IAM role for the callback relay   (basic execution + SendTask*)
#   4 Lambda functions                  (submit, check-status, callback-relay
#                                        behind an API Gateway HTTP API, find-run)
#   1 IAM role for the state machines   (invoke the Lambdas + start the child
#                                        poller execution + the EventBridge
#                                        rule the .sync pattern requires)
#   2 Step Functions state machines     (job-poller, pipeline)
#
# Every command is echoed before it runs so you can see exactly what is
# being created. Safe to re-run: existing resources are updated, not
# duplicated. Undo everything with scripts/99_teardown.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

[ -f .env ] || { echo "ERROR: .env not found. Copy .env.example to .env and fill it in."; exit 1; }
set -a; source .env; set +a
: "${WHEROBOTS_API_KEY:?WHEROBOTS_API_KEY must be set in .env}"
: "${AWS_REGION:=us-west-2}"
: "${WHEROBOTS_REGION:=aws-us-west-2}"
: "${RESOURCE_PREFIX:=wherobots-sfn-ref}"
export AWS_DEFAULT_REGION="$AWS_REGION"
# Blank AWS_* lines in .env would otherwise shadow the default credential chain
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE; do
  [ -z "${!v:-}" ] && unset "$v"
done

run() { echo; echo "▶ $*"; "$@"; }
quiet() { "$@" >/dev/null 2>&1; }

LAMBDA_ROLE="${RESOURCE_PREFIX}-lambda-role"
RELAY_ROLE="${RESOURCE_PREFIX}-relay-role"
SFN_ROLE="${RESOURCE_PREFIX}-sfn-role"
SUBMIT_FN="${RESOURCE_PREFIX}-submit"
CHECK_FN="${RESOURCE_PREFIX}-check-status"
RELAY_FN="${RESOURCE_PREFIX}-callback-relay"
FIND_FN="${RESOURCE_PREFIX}-find-run"
POLLER_SM="${RESOURCE_PREFIX}-job-poller"
PIPELINE_SM="${RESOURCE_PREFIX}-pipeline"

# Asset tags — every resource this script creates carries the same four tags,
# so everything is identifiable (e.g. via `aws resourcegroupstaggingapi
# get-resources`) and sweepable by the teardown script.
TAG_PROJECT="aws-step-functions-python-sdk"
TAG_CREATED_AT="$(date -u +%Y-%m-%d)"
TAG_TEARDOWN_BY="$(python3 -c 'import datetime as d; print((d.date.today() + d.timedelta(days=30)).isoformat())')"
# Same four tags, in each service's own CLI syntax
IAM_TAGS=(Key=ManagedBy,Value=wherobots-labs Key=Project,Value="$TAG_PROJECT" Key=CreatedAt,Value="$TAG_CREATED_AT" Key=TeardownBy,Value="$TAG_TEARDOWN_BY")
LAMBDA_TAGS="ManagedBy=wherobots-labs,Project=${TAG_PROJECT},CreatedAt=${TAG_CREATED_AT},TeardownBy=${TAG_TEARDOWN_BY}"
SFN_TAGS=(key=ManagedBy,value=wherobots-labs key=Project,value="$TAG_PROJECT" key=CreatedAt,value="$TAG_CREATED_AT" key=TeardownBy,value="$TAG_TEARDOWN_BY")

echo "── Who am I ──────────────────────────────────────────────────────────"
run aws sts get-caller-identity --query Account --output text
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo
echo "── 1/6 Secrets Manager: the Wherobots API key ────────────────────────"
# The key lives in Secrets Manager, not in Lambda environment variables:
# anyone with lambda:GetFunctionConfiguration could read a plain env var,
# while the secret needs secretsmanager:GetSecretValue on this one ARN.
SECRET_NAME="${RESOURCE_PREFIX}-wherobots-api-key"
create_secret() {
  echo "▶ aws secretsmanager create-secret --name $SECRET_NAME (secret hidden)"
  SECRET_ARN=$(aws secretsmanager create-secret --name "$SECRET_NAME" \
    --description "Wherobots API key for the ${RESOURCE_PREFIX} reference pipeline" \
    --secret-string "$WHEROBOTS_API_KEY" \
    --tags "${IAM_TAGS[@]}" \
    --query ARN --output text)
}
put_secret() {
  echo "▶ aws secretsmanager put-secret-value --secret-id $SECRET_NAME (secret hidden)"
  aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" \
    --secret-string "$WHEROBOTS_API_KEY" --query VersionId --output text
}
# Three possible states: live (update in place), absent (create), or pending
# deletion. A soft-deleted secret restores; teardown's force-delete cannot be
# restored, so wait for the asynchronous deletion to finish and create fresh.
DELETED_AT=$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" \
  --query DeletedDate --output text 2>/dev/null || echo "ABSENT")
if [ "$DELETED_AT" = "ABSENT" ]; then
  create_secret
elif [ "$DELETED_AT" = "None" ]; then
  SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --query ARN --output text)
  put_secret
elif quiet aws secretsmanager restore-secret --secret-id "$SECRET_NAME"; then
  SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --query ARN --output text)
  put_secret
else
  echo "secret $SECRET_NAME is mid force-delete — waiting for Secrets Manager to finish ..."
  for attempt in $(seq 1 24); do
    if ! quiet aws secretsmanager describe-secret --secret-id "$SECRET_NAME"; then
      break
    fi
    sleep 5
  done
  if quiet aws secretsmanager describe-secret --secret-id "$SECRET_NAME"; then
    echo "✗ secret $SECRET_NAME is still pending deletion after 2 minutes — retry the deploy shortly" >&2
    exit 1
  fi
  create_secret
fi
echo "secret: $SECRET_ARN"

echo
echo "── 2/6 IAM role for the Lambdas ──────────────────────────────────────"
TRUST_LAMBDA='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if quiet aws iam get-role --role-name "$LAMBDA_ROLE"; then
  echo "role $LAMBDA_ROLE already exists — skipping create"
  run aws iam tag-role --role-name "$LAMBDA_ROLE" --tags "${IAM_TAGS[@]}"
else
  run aws iam create-role --role-name "$LAMBDA_ROLE" \
    --assume-role-policy-document "$TRUST_LAMBDA" \
    --tags "${IAM_TAGS[@]}" \
    --query Role.Arn --output text
fi
run aws iam attach-role-policy --role-name "$LAMBDA_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
# Read access to exactly one secret — the API key the SDK Lambdas fetch at
# cold start. No other secret in the account is readable through this role.
SECRET_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "secretsmanager:GetSecretValue",
    "Resource": "${SECRET_ARN}"
  }]
}
EOF
)
run aws iam put-role-policy --role-name "$LAMBDA_ROLE" \
  --policy-name "${RESOURCE_PREFIX}-secret-policy" \
  --policy-document "$SECRET_POLICY"
# Upgrade path: earlier versions attached SendTask* to this shared role before
# the relay got its own. Remove the legacy policy so a redeploy over an old
# stack actually sheds the excess permission (no-op on a fresh account).
if quiet aws iam get-role-policy --role-name "$LAMBDA_ROLE" \
    --policy-name "${RESOURCE_PREFIX}-sendtask-policy"; then
  run aws iam delete-role-policy --role-name "$LAMBDA_ROLE" \
    --policy-name "${RESOURCE_PREFIX}-sendtask-policy"
fi
LAMBDA_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE}"

echo
echo "── 3/6 Package the Lambda handlers ───────────────────────────────────"
# The SDK Lambdas get the pinned wherobots-python-sdk vendored into their
# zips: no network install at cold start, reproducible code on every
# instance. The relay needs only boto3, which the Lambda runtime provides.
BUILD_DIR="lambdas/.build"
rm -rf "$BUILD_DIR"
echo
echo "▶ pip install -r requirements.txt --target $BUILD_DIR (vendoring the pinned SDK)"
python3 -m pip install --quiet --disable-pip-version-check \
  -r requirements.txt --target "$BUILD_DIR" \
  --platform manylinux2014_x86_64 --python-version 3.12 --only-binary=:all: \
  || python3 -m pip install --quiet --disable-pip-version-check \
       -r requirements.txt --target "$BUILD_DIR"
package_fn() { # zipfile handler-file
  local zipfile=$1 handler=$2
  rm -f "$zipfile"
  (cd "$BUILD_DIR" && zip -r -q "../../$zipfile" . -x '*.pyc' -x '*__pycache__*')
  zip -j -q "$zipfile" "$handler"
}
run package_fn lambdas/submit.zip lambdas/submit/handler.py
run package_fn lambdas/check_status.zip lambdas/check_status/handler.py
run package_fn lambdas/find_run.zip lambdas/find_run/handler.py
run zip -j -q lambdas/callback_relay.zip lambdas/callback_relay/handler.py

echo
echo "── 4/6 Lambda functions ──────────────────────────────────────────────"
# Environment maps are built as JSON so a value containing '=' or ',' can
# never reshape the CLI's shorthand-map parsing. The API key itself is NOT
# here — only the ARN of the secret holding it.
lambda_env_json() { # [extra_key extra_value]...
  python3 - "$@" <<'PY'
import json, os, sys
variables = {
    "WHEROBOTS_API_KEY_SECRET_ARN": os.environ["SECRET_ARN"],
    "WHEROBOTS_REGION": os.environ.get("WHEROBOTS_REGION", "aws-us-west-2"),
}
extra = sys.argv[1:]
variables.update(dict(zip(extra[::2], extra[1::2])))
print(json.dumps({"Variables": variables}))
PY
}
export SECRET_ARN
LAMBDA_ENV="$(lambda_env_json)"

deploy_fn() { # name zipfile timeout memory [env] [role_arn]
  local name=$1 zipfile=$2 timeout=$3 memory=$4 env="${5:-$LAMBDA_ENV}" role="${6:-$LAMBDA_ROLE_ARN}"
  local fn_arn="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${name}"
  if quiet aws lambda get-function --function-name "$name"; then
    echo "function $name already exists — updating code + config"
    run aws lambda update-function-code --function-name "$name" \
      --zip-file "fileb://$zipfile" --query FunctionArn --output text
    run aws lambda wait function-updated-v2 --function-name "$name"
    # NOT routed through run(): the echoed argument list would put the
    # environment map (secrets included) on stdout, which the dashboard
    # streams into the browser.
    echo
    echo "▶ aws lambda update-function-configuration --function-name $name --role $role --timeout $timeout --memory-size $memory --environment (hidden)"
    aws lambda update-function-configuration --function-name "$name" \
      --role "$role" \
      --timeout "$timeout" --memory-size "$memory" \
      --environment "$env" --query FunctionArn --output text
    run aws lambda tag-resource --resource "$fn_arn" --tags "$LAMBDA_TAGS"
  else
    # retry loop: a freshly created IAM role takes ~10s to propagate
    for attempt in 1 2 3 4 5 6; do
      if aws lambda create-function --function-name "$name" \
          --runtime python3.12 --handler handler.lambda_handler \
          --role "$role" --zip-file "fileb://$zipfile" \
          --timeout "$timeout" --memory-size "$memory" \
          --environment "$env" \
          --tags "$LAMBDA_TAGS" \
          --query FunctionArn --output text 2>/dev/null; then
        break
      fi
      echo "  waiting for IAM role propagation (attempt $attempt) ..."
      sleep 10
    done
  fi
  run aws lambda wait function-active-v2 --function-name "$name"
}

# The relay gets its own role: it is the only function that may complete
# tasks (SendTask*), and the other Lambdas take attacker-adjacent input
# (job names, run ids) — least privilege keeps the blast radius small.
POLLER_ARN="arn:aws:states:${AWS_REGION}:${ACCOUNT_ID}:stateMachine:${POLLER_SM}"
PIPELINE_ARN="arn:aws:states:${AWS_REGION}:${ACCOUNT_ID}:stateMachine:${PIPELINE_SM}"
if quiet aws iam get-role --role-name "$RELAY_ROLE"; then
  echo "role $RELAY_ROLE already exists — skipping create"
  run aws iam tag-role --role-name "$RELAY_ROLE" --tags "${IAM_TAGS[@]}"
else
  run aws iam create-role --role-name "$RELAY_ROLE" \
    --assume-role-policy-document "$TRUST_LAMBDA" \
    --tags "${IAM_TAGS[@]}" \
    --query Role.Arn --output text
fi
run aws iam attach-role-policy --role-name "$RELAY_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
# SendTask* scoped to the two state machines. Verified working live
# (SendTaskSuccess, SendTaskFailure, and heartbeats all succeeded under
# this exact scoping in end-to-end runs).
RELAY_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["states:SendTaskSuccess", "states:SendTaskFailure", "states:SendTaskHeartbeat"],
    "Resource": ["${PIPELINE_ARN}", "${POLLER_ARN}"]
  }]
}
EOF
)
run aws iam put-role-policy --role-name "$RELAY_ROLE" \
  --policy-name "${RESOURCE_PREFIX}-sendtask-policy" \
  --policy-document "$RELAY_POLICY"
RELAY_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${RELAY_ROLE}"

# Relay first: the submit Lambda needs the relay's endpoint URL in its env.
echo "▶ aws lambda create-function --function-name $RELAY_FN ..."
deploy_fn "$RELAY_FN" lambdas/callback_relay.zip 30 256 '{"Variables":{"NOOP":"1"}}' "$RELAY_ROLE_ARN"

echo
echo "── API Gateway HTTP API for the callback relay ───────────────────────"
# The relay is fronted by an API Gateway HTTP API rather than a Lambda
# Function URL: many orgs block anonymous lambda:InvokeFunctionUrl via SCP,
# and API Gateway is the widely-sanctioned way to expose a public HTTPS
# endpoint. The unguessable single-use task token remains the capability;
# production hardening adds an API key or WAF.
RELAY_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${RELAY_FN}"
API_NAME="${RESOURCE_PREFIX}-callback"
API_ID=$(aws apigatewayv2 get-apis --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)
if [ "$API_ID" = "None" ] || [ -z "$API_ID" ]; then
  run aws apigatewayv2 create-api --name "$API_NAME" --protocol-type HTTP \
    --target "$RELAY_ARN" \
    --tags "ManagedBy=wherobots-labs,Project=${TAG_PROJECT},CreatedAt=${TAG_CREATED_AT},TeardownBy=${TAG_TEARDOWN_BY}" \
    --query ApiId --output text
  API_ID=$(aws apigatewayv2 get-apis --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)
fi
# Tolerate only the benign already-exists conflict; any other failure means
# API Gateway cannot invoke the relay and callbacks would silently degrade
# to the fallback poller — fail the deploy loudly instead.
if ! PERM_OUT=$(aws lambda add-permission --function-name "$RELAY_FN" \
    --statement-id apigw-invoke --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*/*" 2>&1); then
  if echo "$PERM_OUT" | grep -q "ResourceConflictException"; then
    echo "  (invoke permission already in place — ok)"
  else
    echo "$PERM_OUT"
    echo "ERROR: could not grant API Gateway permission to invoke the relay"
    exit 1
  fi
fi
# Stage throttle: without it the only cap is the account-level API Gateway
# limit, so anyone could flood the public endpoint and run up Lambda cost.
run aws apigatewayv2 update-stage --api-id "$API_ID" --stage-name '$default' \
  --default-route-settings '{"ThrottlingBurstLimit":10,"ThrottlingRateLimit":5}'
CALLBACK_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/"
echo "callback URL: $CALLBACK_URL"

SUBMIT_ENV="$(lambda_env_json CALLBACK_URL "$CALLBACK_URL")"
echo "▶ aws lambda create-function --function-name $SUBMIT_FN ... (env vars hidden)"
deploy_fn "$SUBMIT_FN" lambdas/submit.zip 120 512 "$SUBMIT_ENV"
echo "▶ aws lambda create-function --function-name $CHECK_FN ... (env vars hidden)"
deploy_fn "$CHECK_FN" lambdas/check_status.zip 60 256
echo "▶ aws lambda create-function --function-name $FIND_FN ... (env vars hidden)"
deploy_fn "$FIND_FN" lambdas/find_run.zip 60 256

SUBMIT_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${SUBMIT_FN}"
CHECK_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${CHECK_FN}"
FIND_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${FIND_FN}"

echo
echo "── 5/6 IAM role for the state machines ───────────────────────────────"
TRUST_SFN='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"states.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
if quiet aws iam get-role --role-name "$SFN_ROLE"; then
  echo "role $SFN_ROLE already exists — skipping create"
  run aws iam tag-role --role-name "$SFN_ROLE" --tags "${IAM_TAGS[@]}"
else
  run aws iam create-role --role-name "$SFN_ROLE" \
    --assume-role-policy-document "$TRUST_SFN" \
    --tags "${IAM_TAGS[@]}" \
    --query Role.Arn --output text
fi
SFN_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": ["${SUBMIT_ARN}", "${CHECK_ARN}", "${FIND_ARN}"]
    },
    {
      "Effect": "Allow",
      "Action": ["states:StartExecution"],
      "Resource": "${POLLER_ARN}"
    },
    {
      "Effect": "Allow",
      "Action": ["states:DescribeExecution", "states:StopExecution"],
      "Resource": "arn:aws:states:${AWS_REGION}:${ACCOUNT_ID}:execution:${POLLER_SM}:*"
    },
    {
      "Effect": "Allow",
      "Action": ["events:PutTargets", "events:PutRule", "events:DescribeRule"],
      "Resource": "arn:aws:events:${AWS_REGION}:${ACCOUNT_ID}:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule"
    }
  ]
}
EOF
)
run aws iam put-role-policy --role-name "$SFN_ROLE" \
  --policy-name "${RESOURCE_PREFIX}-sfn-policy" \
  --policy-document "$SFN_POLICY"
SFN_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${SFN_ROLE}"

echo
echo "── 6/6 Step Functions state machines ─────────────────────────────────"
render() { # asl-file  (substitutes the ${...} placeholders)
  sed -e "s|\${CHECK_LAMBDA_ARN}|${CHECK_ARN}|" \
      -e "s|\${SUBMIT_LAMBDA_ARN}|${SUBMIT_ARN}|" \
      -e "s|\${FIND_RUN_LAMBDA_ARN}|${FIND_ARN}|" \
      -e "s|\${POLLER_ARN}|${POLLER_ARN}|" "$1"
}

deploy_sm() { # name asl-file
  local name=$1 asl=$2 arn="arn:aws:states:${AWS_REGION}:${ACCOUNT_ID}:stateMachine:$1"
  if quiet aws stepfunctions describe-state-machine --state-machine-arn "$arn"; then
    echo "state machine $name already exists — updating definition"
    run aws stepfunctions update-state-machine --state-machine-arn "$arn" \
      --definition "$(render "$asl")" --role-arn "$SFN_ROLE_ARN" \
      --query updateDate --output text
    run aws stepfunctions tag-resource --resource-arn "$arn" --tags "${SFN_TAGS[@]}"
  else
    # Track a confirmed create: without this, an exhausted retry loop would
    # fall through with status 0 and the deploy would declare victory with no
    # state machine behind it.
    local created="" out=""
    for attempt in 1 2 3 4 5 6; do
      if out=$(aws stepfunctions create-state-machine --name "$name" \
          --definition "$(render "$asl")" --role-arn "$SFN_ROLE_ARN" \
          --tags "${SFN_TAGS[@]}" \
          --query stateMachineArn --output text 2>&1); then
        echo "$out"
        created=yes
        break
      fi
      echo "  waiting for IAM role propagation (attempt $attempt) ..."
      sleep 10
    done
    if [ -z "$created" ]; then
      echo "✗ failed to create state machine $name after 6 attempts; last error:" >&2
      echo "$out" >&2
      exit 1
    fi
  fi
}

echo "▶ aws stepfunctions create-state-machine --name $POLLER_SM ..."
deploy_sm "$POLLER_SM" statemachines/wherobots-job-poller.asl.json
echo "▶ aws stepfunctions create-state-machine --name $PIPELINE_SM ..."
deploy_sm "$PIPELINE_SM" statemachines/wherobots-pipeline.asl.json

echo
echo "── Saving ARNs to .state.json ────────────────────────────────────────"
python3 - "$PIPELINE_ARN" "$POLLER_ARN" "$AWS_REGION" <<'PY'
import json, sys, pathlib
p = pathlib.Path(".state.json")
state = json.loads(p.read_text()) if p.exists() else {}
state.update(pipeline_arn=sys.argv[1], poller_arn=sys.argv[2], aws_region=sys.argv[3])
p.write_text(json.dumps(state, indent=2) + "\n")
print(json.dumps(state, indent=2))
PY

echo
echo "✅ Infrastructure ready — green to run. Next: ./scripts/03_run.sh (or the dashboard's Start button)."
