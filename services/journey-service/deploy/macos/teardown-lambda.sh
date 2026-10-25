#!/usr/bin/env bash
# macOS/Linux version of ../windows/teardown-lambda.ps1: removes the disruption checker: our CloudFormation stack
# (function, role, schedule, log group) and the uploaded zips.
#
# Run this BEFORE the platform's scripts/macos/teardown.sh. A Lambda inside a VPC holds network interfaces in the
# platform's subnets and uses its security group; until they are released, deleting the platform stack can fail. AWS
# releases them some minutes after the function is deleted (it can take longer than this script waits), so if the
# platform teardown complains about a subnet or security group, wait and re-run it.
#
# NOT TESTED AGAINST REAL AWS.
#
# Usage: bash services/journey-service/deploy/macos/teardown-lambda.sh [--yes]
#          [--region REGION] [--lambda-stack NAME] [--platform-stack NAME] [--resource-prefix PREFIX] [--bucket NAME]

set -euo pipefail

YES=false
LAMBDA_STACK="${LAMBDA_STACK:-careroute-journey-lambda}"
PLATFORM_STACK="${PLATFORM_STACK:-careroute-stack-1}"
REGION="${REGION:-ap-southeast-1}"
RESOURCE_PREFIX="${RESOURCE_PREFIX:-}"
BUCKET="${BUCKET:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --yes)             YES=true; shift ;;
    --region)          REGION="$2"; shift 2 ;;
    --lambda-stack)    LAMBDA_STACK="$2"; shift 2 ;;
    --platform-stack)  PLATFORM_STACK="$2"; shift 2 ;;
    --resource-prefix) RESOURCE_PREFIX="$2"; shift 2 ;;
    --bucket)          BUCKET="$2"; shift 2 ;;
    -h|--help)         sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/resource-names.sh"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

command -v aws >/dev/null 2>&1 || die "'aws' is not installed or not on PATH"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || die "AWS credentials are not configured (run 'aws configure')"
RESOURCE_PREFIX="$(resolve_resource_prefix "$PLATFORM_STACK" "$REGION" "$RESOURCE_PREFIX")"
BUCKET="$(shared_bucket_name "$RESOURCE_PREFIX" "$ACCOUNT_ID" "$BUCKET")"

if [ "$YES" != true ]; then
  read -r -p "This deletes stack $LAMBDA_STACK and s3://$BUCKET/lambda/. Type the stack name to continue: " confirm
  [ "$confirm" = "$LAMBDA_STACK" ] || die "Aborted."
fi

step "Deleting stack $LAMBDA_STACK"
STATUS="$(aws cloudformation describe-stacks --stack-name "$LAMBDA_STACK" --region "$REGION" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
if [ -z "$STATUS" ]; then
  info "Stack not found; skipping"
else
  aws cloudformation delete-stack --stack-name "$LAMBDA_STACK" --region "$REGION" || die "delete-stack failed"
  aws cloudformation wait stack-delete-complete --stack-name "$LAMBDA_STACK" --region "$REGION" \
    || die "The stack did not delete cleanly. Check: aws cloudformation describe-stack-events --stack-name $LAMBDA_STACK"
  info "Stack deleted"
fi

step "Deleting the uploaded packages from s3://$BUCKET/lambda/"
if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  aws s3 rm "s3://$BUCKET/lambda/" --recursive --region "$REGION" || true
else
  info "Bucket $BUCKET not found"
fi

printf '\n\033[1;32mDisruption checker removed. Network interfaces may take a few more minutes to be released.\033[0m\n'
