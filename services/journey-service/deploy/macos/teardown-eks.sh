#!/usr/bin/env bash
# macOS/Linux version of ../windows/teardown-eks.ps1: removes what deploy-eks.sh added: our Deployment, Service and
# Secret, our image tags in ECR, and the packs stored in the shared S3 bucket. It does not touch the platform's own
# resources.
#
# Order when tearing everything down:  1. teardown-lambda.sh   2. this script   3. the platform's scripts/macos/teardown.sh
# (The platform's teardown deletes the whole stack, the cluster and the bucket, and fails if our Lambda still holds
# network interfaces in its subnets.)
#
# NOT TESTED AGAINST REAL AWS.
#
# Usage: bash services/journey-service/deploy/macos/teardown-eks.sh [--yes]
#          [--region REGION] [--stack-name NAME] [--namespace careroute] [--resource-prefix PREFIX] [--bucket NAME]

set -euo pipefail

YES=false
STACK_NAME="${STACK_NAME:-careroute-stack-1}"
REGION="${REGION:-ap-southeast-1}"
APP_NAMESPACE="${APP_NAMESPACE:-careroute}"
RESOURCE_PREFIX="${RESOURCE_PREFIX:-}"
BUCKET="${BUCKET:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --yes)             YES=true; shift ;;
    --region)          REGION="$2"; shift 2 ;;
    --stack-name)      STACK_NAME="$2"; shift 2 ;;
    --namespace)       APP_NAMESPACE="$2"; shift 2 ;;
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

stack_output() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null || true
}

for cmd in aws kubectl; do
  command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' is not installed or not on PATH"
done
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || die "AWS credentials are not configured (run 'aws configure')"
# The shared bucket is <prefix>-files-<account id>; the prefix is read from the stack (or --resource-prefix / --bucket).
RESOURCE_PREFIX="$(resolve_resource_prefix "$STACK_NAME" "$REGION" "$RESOURCE_PREFIX")"
BUCKET="$(shared_bucket_name "$RESOURCE_PREFIX" "$ACCOUNT_ID" "$BUCKET")"

if [ "$YES" != true ]; then
  echo
  echo "This deletes the journey-service Deployment, Service and Secret in namespace $APP_NAMESPACE,"
  echo "our journey-* images in ECR, and everything under s3://$BUCKET/packs/ (saved routes and trips in"
  echo "DynamoDB are NOT deleted here; they go with the platform stack)."
  read -r -p "Type journey-service to continue: " confirm
  [ "$confirm" = "journey-service" ] || die "Aborted."
fi

step "Deleting our Kubernetes resources"
CLUSTER_NAME="$(stack_output EksClusterName)"
if [ -n "$CLUSTER_NAME" ] && [ "$CLUSTER_NAME" != "None" ]; then
  aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER_NAME" >/dev/null 2>&1 || true
  kubectl delete deployment,service journey-service -n "$APP_NAMESPACE" --ignore-not-found --timeout=5m || true
  kubectl delete secret journey-api-keys -n "$APP_NAMESPACE" --ignore-not-found || true
else
  info "Stack $STACK_NAME not found; nothing to delete in the cluster"
fi

step "Deleting our images from ECR (tags starting with journey-)"
ECR_URI="$(stack_output EcrRepositoryUri)"
if [ -n "$ECR_URI" ] && [ "$ECR_URI" != "None" ]; then
  REPO="${ECR_URI##*/}"
  TAGS="$(aws ecr list-images --repository-name "$REPO" --region "$REGION" \
    --query "imageIds[?imageTag!=null] | [?starts_with(imageTag, 'journey-')].imageTag" --output text 2>/dev/null || true)"
  for tag in $TAGS; do
    [ "$tag" != "None" ] || continue
    aws ecr batch-delete-image --repository-name "$REPO" --region "$REGION" --image-ids "imageTag=$tag" >/dev/null 2>&1 || true
    info "Deleted $REPO:$tag"
  done
fi

step "Deleting packs from s3://$BUCKET/packs/"
if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  aws s3 rm "s3://$BUCKET/packs/" --recursive --region "$REGION" || true
else
  info "Bucket $BUCKET not found"
fi

printf '\n\033[1;32mJourney Service removed.\033[0m\n'
