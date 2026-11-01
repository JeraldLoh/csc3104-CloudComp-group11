#!/usr/bin/env bash
# macOS/Linux version of ../windows/deploy-lambda.ps1: deploys the disruption checker Lambda (builds the package, uploads it,
# creates or updates our own CloudFormation stack) next to the platform's stack, using that stack's outputs.
#
# Run the platform's scripts/macos/deploy.sh first. This script then:
#   stack outputs -> build the zip (build-lambda.sh --verify) -> upload it to the shared bucket under lambda/
#   -> deploy deploy/lambda/disruption-lambda.yaml as stack careroute-journey-lambda -> invoke the function once.
# The LTA key is read from .env and passed to CloudFormation as a NoEcho parameter (it will show briefly in your
# own process list while the command runs, and it ends up in the function's environment variables).
#
# Safe to re-run: the zip's key contains its content hash, so only a changed package is redeployed.
# NOT TESTED AGAINST REAL AWS.
#
# Usage: bash services/journey-service/deploy/macos/deploy-lambda.sh
#          [--region REGION] [--platform-stack NAME] [--lambda-stack NAME]
#          [--resource-prefix PREFIX] [--bucket NAME] [--env-file PATH] [--schedule ENABLED|DISABLED]
# Every option can also be set through the matching environment variable below.

set -euo pipefail

PLATFORM_STACK="${PLATFORM_STACK:-careroute-stack-1}"
LAMBDA_STACK="${LAMBDA_STACK:-careroute-journey-lambda}"
REGION="${REGION:-ap-southeast-1}"
RESOURCE_PREFIX="${RESOURCE_PREFIX:-}"
BUCKET="${BUCKET:-}"
ENV_FILE="${ENV_FILE:-}"
SCHEDULE_STATE="${SCHEDULE_STATE:-ENABLED}"

while [ $# -gt 0 ]; do
  case "$1" in
    --region)          REGION="$2"; shift 2 ;;
    --platform-stack)  PLATFORM_STACK="$2"; shift 2 ;;
    --lambda-stack)    LAMBDA_STACK="$2"; shift 2 ;;
    --resource-prefix) RESOURCE_PREFIX="$2"; shift 2 ;;
    --bucket)          BUCKET="$2"; shift 2 ;;
    --env-file)        ENV_FILE="$2"; shift 2 ;;
    --schedule)        SCHEDULE_STATE="$2"; shift 2 ;;
    -h|--help)         sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$SERVICE_DIR/../.." && pwd)"
TEMPLATE="$SCRIPT_DIR/../lambda/disruption-lambda.yaml"
ZIP="$SCRIPT_DIR/../lambda/dist/disruption-lambda.zip"
[ -n "$ENV_FILE" ] || ENV_FILE="$REPO_ROOT/.env"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
. "$SCRIPT_DIR/resource-names.sh"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

stack_output() {
  aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text
}

case "$SCHEDULE_STATE" in ENABLED|DISABLED) ;; *) die "--schedule must be ENABLED or DISABLED" ;; esac

# ---------------------------------------------------------------------------
step "Checking prerequisites"
for cmd in aws docker; do
  command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' is not installed or not on PATH"
done
# macOS has shasum; many Linux installs (Fedora, Alpine, minimal Debian) only have sha256sum. Same output format.
if command -v sha256sum >/dev/null 2>&1; then SHA256="sha256sum"
elif command -v shasum >/dev/null 2>&1; then SHA256="shasum -a 256"
else die "Neither 'sha256sum' nor 'shasum' is installed"
fi
docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop and retry."
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || die "AWS credentials are not configured (run 'aws configure')"
STATUS="$(aws cloudformation describe-stacks --stack-name "$PLATFORM_STACK" --region "$REGION" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
case "$STATUS" in
  *_COMPLETE) ;;
  *) die "Stack $PLATFORM_STACK is not deployed in $REGION (status: $STATUS). Run the platform's deploy script first." ;;
esac

# The LTA key: read from .env, never printed.
LTA_KEY=""
if [ -f "$ENV_FILE" ]; then
  LTA_KEY="$(sed -n 's/^[[:space:]]*LTA_API_KEY[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" | tail -n 1 \
    | tr -d '\r' | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/")"
fi
[ -n "$LTA_KEY" ] || die "LTA_API_KEY not found in $ENV_FILE (the checker cannot read train alerts without it)"

# ---------------------------------------------------------------------------
step "Reading the platform stack outputs"
SUBNETS="$(stack_output "$PLATFORM_STACK" PrivateSubnetIds)"
SECURITY_GROUP="$(stack_output "$PLATFORM_STACK" ClusterSecurityGroupId)"
REDIS_ENDPOINT="$(stack_output "$PLATFORM_STACK" RedisEndpoint)"
QUEUE_ARN="$(stack_output "$PLATFORM_STACK" DisruptionsQueueArn)"
QUEUE_URL="$(stack_output "$PLATFORM_STACK" DisruptionsQueueUrl)"
for pair in "PrivateSubnetIds=$SUBNETS" "ClusterSecurityGroupId=$SECURITY_GROUP" "RedisEndpoint=$REDIS_ENDPOINT" \
            "DisruptionsQueueArn=$QUEUE_ARN" "DisruptionsQueueUrl=$QUEUE_URL"; do
  case "${pair#*=}" in ""|None) die "The platform stack has no output named ${pair%%=*}" ;; esac
done
QUEUE_NAME="${QUEUE_URL##*/}"

RESOURCE_PREFIX="$(resolve_resource_prefix "$PLATFORM_STACK" "$REGION" "$RESOURCE_PREFIX")"
BUCKET="$(shared_bucket_name "$RESOURCE_PREFIX" "$ACCOUNT_ID" "$BUCKET")"

info "Subnets $SUBNETS | security group $SECURITY_GROUP"
info "Redis $REDIS_ENDPOINT | queue $QUEUE_NAME | code bucket $BUCKET"
aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 \
  || die "Bucket $BUCKET does not exist. The platform's deploy script creates it (named <prefix>-files-<account id>); run that first, or pass --bucket."

# ---------------------------------------------------------------------------
step "Building the package"
bash "$SCRIPT_DIR/build-lambda.sh" --verify || die "The build failed"

# ---------------------------------------------------------------------------
step "Uploading the package"
HASH="$($SHA256 "$ZIP" | cut -c1-12)"
CODE_KEY="lambda/disruption-checker-$HASH.zip"
aws s3 cp "$ZIP" "s3://$BUCKET/$CODE_KEY" --region "$REGION" --only-show-errors
info "s3://$BUCKET/$CODE_KEY"

step "Deploying stack $LAMBDA_STACK"
aws cloudformation deploy \
  --stack-name "$LAMBDA_STACK" \
  --template-file "$TEMPLATE" \
  --capabilities CAPABILITY_IAM \
  --region "$REGION" \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "CodeBucket=$BUCKET" "CodeKey=$CODE_KEY" \
    "PrivateSubnetIds=$SUBNETS" "SecurityGroupId=$SECURITY_GROUP" \
    "RedisEndpoint=$REDIS_ENDPOINT" "DisruptionsQueueArn=$QUEUE_ARN" \
    "DisruptionsQueueName=$QUEUE_NAME" "LtaApiKey=$LTA_KEY" "ScheduleState=$SCHEDULE_STATE"
FUNCTION="$(stack_output "$LAMBDA_STACK" FunctionName)"

step "Invoking $FUNCTION once"
aws lambda invoke --function-name "$FUNCTION" --region "$REGION" --cli-binary-format raw-in-base64-out \
  --payload '{}' "$WORK_DIR/result.json" >/dev/null
info "Result: $(cat "$WORK_DIR/result.json")"

printf '\n\033[1;32mDisruption checker deployed; EventBridge runs it every minute (%s).\033[0m\n' "$SCHEDULE_STATE"
echo "Logs: aws logs tail /aws/lambda/$FUNCTION --follow --region $REGION"
echo 'A result like {"activeTrips": 0, ...} on a normal day is correct: the checker does nothing until LTA reports a disruption.'
