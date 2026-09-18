#!/usr/bin/env bash
# Step 2 — build the AWS infrastructure with plain AWS CLI calls.
#
# Creates (all names start with $RESOURCE_PREFIX):
#   1 IAM role for the Lambdas          (basic execution + SendTask* so the
#                                        callback relay can complete tasks)
#   4 Lambda functions                  (submit, check-status, callback-relay
#                                        + its public Function URL, find-run)
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

# Asset tags — ManagedBy=claude-code is what the aws-asset-catalog ledger keys
# on for `aws resourcegroupstaggingapi get-resources` reconciliation.
TAG_PROJECT="aws-step-functions-python-sdk"
TAG_CREATED_AT="$(date -u +%Y-%m-%d)"
TAG_TEARDOWN_BY="$(python3 -c 'import datetime as d; print((d.date.today() + d.timedelta(days=30)).isoformat())')"
# Same four tags, in each service's own CLI syntax
IAM_TAGS=(Key=ManagedBy,Value=claude-code Key=Project,Value="$TAG_PROJECT" Key=CreatedAt,Value="$TAG_CREATED_AT" Key=TeardownBy,Value="$TAG_TEARDOWN_BY")
LAMBDA_TAGS="ManagedBy=claude-code,Project=${TAG_PROJECT},CreatedAt=${TAG_CREATED_AT},TeardownBy=${TAG_TEARDOWN_BY}"
SFN_TAGS=(key=ManagedBy,value=claude-code key=Project,value="$TAG_PROJECT" key=CreatedAt,value="$TAG_CREATED_AT" key=TeardownBy,value="$TAG_TEARDOWN_BY")

echo "── Who am I ──────────────────────────────────────────────────────────"
run aws sts get-caller-identity --query Account --output text
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo
echo "── 1/5 IAM role for the Lambdas ──────────────────────────────────────"
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
LAMBDA_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE}"

echo
echo "── 2/5 Package the Lambda handlers ───────────────────────────────────"
run zip -j -q lambdas/submit.zip lambdas/submit/handler.py
run zip -j -q lambdas/check_status.zip lambdas/check_status/handler.py
run zip -j -q lambdas/callback_relay.zip lambdas/callback_relay/handler.py
run zip -j -q lambdas/find_run.zip lambdas/find_run/handler.py

echo
echo "── 3/5 Lambda functions ──────────────────────────────────────────────"
# Environment maps are built as JSON so a secret containing '=' or ',' can
# never reshape the CLI's shorthand-map parsing.
lambda_env_json() { # [extra_key extra_value]...
  python3 - "$@" <<'PY'
import json, os, sys
variables = {
    "WHEROBOTS_API_KEY": os.environ["WHEROBOTS_API_KEY"],
    "WHEROBOTS_REGION": os.environ.get("WHEROBOTS_REGION", "aws-us-west-2"),
}
extra = sys.argv[1:]
variables.update(dict(zip(extra[::2], extra[1::2])))
print(json.dumps({"Variables": variables}))
PY
}
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
    echo "▶ aws lambda update-function-configuration --function-name $name --timeout $timeout --memory-size $memory --environment (hidden)"
    aws lambda update-function-configuration --function-name "$name" \
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
# Function URL: many orgs (this one included) block anonymous
# lambda:InvokeFunctionUrl via SCP, and API Gateway is the sanctioned way
# to expose a public HTTPS endpoint. The unguessable single-use task token
# remains the capability; production hardening adds an API key or WAF.
RELAY_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${RELAY_FN}"
API_NAME="${RESOURCE_PREFIX}-callback"
API_ID=$(aws apigatewayv2 get-apis --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)
if [ "$API_ID" = "None" ] || [ -z "$API_ID" ]; then
  run aws apigatewayv2 create-api --name "$API_NAME" --protocol-type HTTP \
    --target "$RELAY_ARN" \
    --tags "ManagedBy=claude-code,Project=${TAG_PROJECT},CreatedAt=${TAG_CREATED_AT},TeardownBy=${TAG_TEARDOWN_BY}" \
    --query ApiId --output text
  API_ID=$(aws apigatewayv2 get-apis --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)
fi
quiet aws lambda add-permission --function-name "$RELAY_FN" \
  --statement-id apigw-invoke --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*/*" || true
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
echo "── 4/5 IAM role for the state machines ───────────────────────────────"
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
echo "── 5/5 Step Functions state machines ─────────────────────────────────"
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
    for attempt in 1 2 3 4 5 6; do
      if aws stepfunctions create-state-machine --name "$name" \
          --definition "$(render "$asl")" --role-arn "$SFN_ROLE_ARN" \
          --tags "${SFN_TAGS[@]}" \
          --query stateMachineArn --output text 2>/dev/null; then
        break
      fi
      echo "  waiting for IAM role propagation (attempt $attempt) ..."
      sleep 10
    done
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
