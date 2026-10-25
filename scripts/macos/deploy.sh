#!/usr/bin/env bash
# Deploys the full CareRoute stack end to end (README "Deploy the infrastructure"
# and "Deploy the application" sections):
#   CloudFormation stack -> kubeconfig -> AWS Load Balancer Controller ->
#   image build/push -> S3 bucket -> rendered k8s manifests -> health check.
#
# Safe to re-run. The stack is created or updated from the current
# cloudformation/*.yaml, so edits to the template are picked up on the next run.
#
# With --service NAME, only that service is built and deployed onto an existing
# stack (no CloudFormation or Load Balancer Controller steps):
#   services/NAME/Dockerfile      image build context
#   k8s/eks/services/NAME.yaml    manifest; a Deployment named NAME, or (for one image run
#                                 as several Deployments, e.g. detectors) every Deployment
#                                 labelled app.kubernetes.io/part-of: NAME.
#                                 Placeholders: __IMAGE__ __NAMESPACE__ __REGION__
#                                 __REDIS_ENDPOINT__ __CHECKINS_QUEUE_URL__
#                                 __DISRUPTIONS_QUEUE_URL__
# The image goes to ECR repository careroute-NAME (created if missing).
#
# Usage: ./scripts/macos/deploy.sh [--email you@example.com] [--tag v2]
#                                  [--service NAME]
#                                  [--stack-name NAME] [--region REGION]
# Every option can also be set through the matching environment variable below.

set -euo pipefail

STACK_NAME="${STACK_NAME:-careroute-stack-1}"
REGION="${REGION:-ap-southeast-1}"
CLUSTER_NAME="${CLUSTER_NAME:-careroute-cluster-1}"
COMPUTE_MODE="${COMPUTE_MODE:-fargate}"
APP_PORT="${APP_PORT:-8000}"
APP_NAMESPACE="${APP_NAMESPACE:-careroute}"
BUDGET_EMAIL="${BUDGET_EMAIL:-}"
IMAGE_TAG="${IMAGE_TAG:-v1}"
SERVICE="${SERVICE:-}"
LBC_POLICY_NAME="AWSLoadBalancerControllerIAMPolicy"

while [ $# -gt 0 ]; do
  case "$1" in
    --email)      BUDGET_EMAIL="$2"; shift 2 ;;
    --tag)        IMAGE_TAG="$2"; shift 2 ;;
    --service)    SERVICE="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --region)     REGION="$2"; shift 2 ;;
    -h|--help)    sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEMPLATE="$REPO_ROOT/cloudformation/careroute-vpc-eks-standard.yaml"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

stack_output() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

# ---------------------------------------------------------------------------
step "Checking prerequisites"
for cmd in aws kubectl helm eksctl docker curl; do
  command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' is not installed or not on PATH"
done
docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop and retry."
[ -f "$TEMPLATE" ] || die "Template not found: $TEMPLATE"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" \
  || die "AWS credentials are not configured (run 'aws configure')"
info "Account $ACCOUNT_ID, region $REGION, stack $STACK_NAME"

# ---------------------------------------------------------------------------
# --service: build and deploy one service onto the existing stack, then exit.
# Sets TARGET to the kubectl arguments selecting this service's Deployment(s); fails if there are none.
service_target() {
  if kubectl get deployment "$SERVICE" -n "$APP_NAMESPACE" >/dev/null 2>&1; then
    TARGET=(deployment/"$SERVICE")
  elif [ -n "$(kubectl get deployment -n "$APP_NAMESPACE" -l "app.kubernetes.io/part-of=$SERVICE" -o name 2>/dev/null)" ]; then
    TARGET=(deployment -l "app.kubernetes.io/part-of=$SERVICE")
  else
    return 1
  fi
}

if [ -n "$SERVICE" ]; then
  printf '%s' "$SERVICE" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$' \
    || die "Service name must be lowercase letters, digits and dashes: '$SERVICE'"
  SERVICE_DIR="$REPO_ROOT/services/$SERVICE"
  SERVICE_MANIFEST="$REPO_ROOT/k8s/eks/services/$SERVICE.yaml"
  [ -f "$SERVICE_DIR/Dockerfile" ] || die "No Dockerfile at services/$SERVICE/Dockerfile"
  [ -f "$SERVICE_MANIFEST" ] || die "No manifest at k8s/eks/services/$SERVICE.yaml"

  STATUS="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE)"
  case "$STATUS" in
    CREATE_COMPLETE|UPDATE_COMPLETE) ;;
    *) die "Stack $STACK_NAME is $STATUS. Run this script without --service first to create it." ;;
  esac

  CLUSTER_NAME="$(stack_output EksClusterName)"
  REDIS_ENDPOINT="$(stack_output RedisEndpoint)"
  CHECKINS_QUEUE_URL="$(stack_output CheckinsQueueUrl)"
  DISRUPTIONS_QUEUE_URL="$(stack_output DisruptionsQueueUrl)"
  REPO_NAME="careroute-$SERVICE"
  ECR_REGISTRY="$ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com"
  IMAGE="$ECR_REGISTRY/$REPO_NAME:$IMAGE_TAG"

  step "Connecting kubectl to the cluster"
  aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER_NAME"

  # Created outside the stack, like the S3 bucket; teardown.sh removes careroute-* repos.
  step "Ensuring ECR repository $REPO_NAME exists"
  if aws ecr describe-repositories --repository-names "$REPO_NAME" --region "$REGION" >/dev/null 2>&1; then
    info "Repository already exists"
  else
    aws ecr create-repository --repository-name "$REPO_NAME" --region "$REGION" >/dev/null
    info "Created repository $REPO_NAME"
  fi

  step "Building and pushing image $IMAGE"
  aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
  docker build --platform linux/amd64 -t "$IMAGE" "$SERVICE_DIR"
  docker push "$IMAGE"

  step "Rendering and applying k8s/eks/services/$SERVICE.yaml"
  RENDERED="$WORK_DIR/$SERVICE.yaml"
  sed -e "s|__IMAGE__|$IMAGE|g" \
      -e "s|__NAMESPACE__|$APP_NAMESPACE|g" \
      -e "s|__REGION__|$REGION|g" \
      -e "s|__REDIS_ENDPOINT__|$REDIS_ENDPOINT|g" \
      -e "s|__CHECKINS_QUEUE_URL__|$CHECKINS_QUEUE_URL|g" \
      -e "s|__DISRUPTIONS_QUEUE_URL__|$DISRUPTIONS_QUEUE_URL|g" \
      "$SERVICE_MANIFEST" > "$RENDERED"
  DEPLOYMENT_EXISTED=false
  service_target && DEPLOYMENT_EXISTED=true
  kubectl apply -f "$RENDERED"
  service_target || die "The manifest has no Deployment named $SERVICE or labelled app.kubernetes.io/part-of=$SERVICE"
  # Re-pushing the same tag does not change the spec, so force new pods to pull it.
  if [ "$DEPLOYMENT_EXISTED" = true ]; then
    kubectl rollout restart "${TARGET[@]}" -n "$APP_NAMESPACE"
  fi
  kubectl rollout status "${TARGET[@]}" -n "$APP_NAMESPACE" --timeout=10m

  echo
  printf '\033[1;32mService %s deployed.\033[0m Logs: kubectl logs -n %s deploy/%s -f\n' \
    "$SERVICE" "$APP_NAMESPACE" "$SERVICE"
  exit 0
fi

# ---------------------------------------------------------------------------
step "Validating CloudFormation template"
aws cloudformation validate-template --template-body "file://$TEMPLATE" --region "$REGION" >/dev/null

# A stack stuck in ROLLBACK_COMPLETE cannot be updated, only deleted.
STATUS="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE)"
if [ "$STATUS" = "ROLLBACK_COMPLETE" ]; then
  step "Stack is in ROLLBACK_COMPLETE from a failed create; deleting it first"
  aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
fi

# ---------------------------------------------------------------------------
step "Deploying CloudFormation stack (15-25 minutes on first create)"
PARAMS="ClusterName=$CLUSTER_NAME ComputeMode=$COMPUTE_MODE AppPort=$APP_PORT AppNamespace=$APP_NAMESPACE"
if [ -n "$BUDGET_EMAIL" ]; then
  PARAMS="$PARAMS BudgetAlertEmail=$BUDGET_EMAIL"
elif [ "$STATUS" = "NONE" ] || [ "$STATUS" = "ROLLBACK_COMPLETE" ]; then
  info "No --email given: the budget alert and DLQ alarms will not be created."
fi
# Unlisted parameters keep their previous values on update.
# shellcheck disable=SC2086
aws cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file "$TEMPLATE" \
  --parameter-overrides $PARAMS \
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM \
  --region "$REGION" \
  --no-fail-on-empty-changeset

ECR_URI="$(stack_output EcrRepositoryUri)"
API_ROLE_ARN="$(stack_output ApiServiceRoleArn)"
REDIS_ENDPOINT="$(stack_output RedisEndpoint)"
ALB_SG_ID="$(stack_output AlbSecurityGroupId)"
VPC_ID="$(stack_output VpcId)"
CLUSTER_NAME="$(stack_output EksClusterName)"
info "Cluster:  $CLUSTER_NAME"
info "VPC:      $VPC_ID"
info "ECR:      $ECR_URI"
info "Redis:    $REDIS_ENDPOINT"
info "ALB SG:   $ALB_SG_ID"
info "API role: $API_ROLE_ARN"

# ---------------------------------------------------------------------------
step "Connecting kubectl to the cluster"
aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER_NAME"

# ---------------------------------------------------------------------------
step "Installing the AWS Load Balancer Controller"
helm repo add eks https://aws.github.io/eks-charts >/dev/null 2>&1 || true
helm repo update eks
CHART_INFO="$(helm show chart eks/aws-load-balancer-controller)"
CHART_VERSION="$(printf '%s\n' "$CHART_INFO" | awk '/^version:/ {print $2}')"
APP_VERSION="$(printf '%s\n' "$CHART_INFO" | awk '/^appVersion:/ {print $2}')"
info "Chart $CHART_VERSION (controller $APP_VERSION)"

curl -fsSL -o "$WORK_DIR/iam_policy.json" \
  "https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/$APP_VERSION/docs/install/iam_policy.json"

POLICY_ARN="arn:aws:iam::$ACCOUNT_ID:policy/$LBC_POLICY_NAME"
if aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  # Keep the policy in step with the controller version. IAM allows 5 versions,
  # so drop the old non-default ones before adding the new default.
  for v in $(aws iam list-policy-versions --policy-arn "$POLICY_ARN" \
      --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text); do
    aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$v"
  done
  aws iam create-policy-version --policy-arn "$POLICY_ARN" \
    --policy-document "file://$WORK_DIR/iam_policy.json" --set-as-default >/dev/null
  info "Updated IAM policy $LBC_POLICY_NAME"
else
  aws iam create-policy --policy-name "$LBC_POLICY_NAME" \
    --policy-document "file://$WORK_DIR/iam_policy.json" >/dev/null
  info "Created IAM policy $LBC_POLICY_NAME"
fi

eksctl create iamserviceaccount \
  --cluster "$CLUSTER_NAME" \
  --namespace kube-system \
  --name aws-load-balancer-controller \
  --attach-policy-arn "$POLICY_ARN" \
  --override-existing-serviceaccounts \
  --region "$REGION" \
  --approve

helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  --set clusterName="$CLUSTER_NAME" \
  --set serviceAccount.create=false \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set region="$REGION" \
  --set vpcId="$VPC_ID" \
  --version "$CHART_VERSION"

info "Waiting for the controller to become ready (Fargate pods take 1-2 minutes)"
kubectl rollout status deployment/aws-load-balancer-controller -n kube-system --timeout=10m

# ---------------------------------------------------------------------------
step "Building and pushing image $ECR_URI:$IMAGE_TAG"
ECR_REGISTRY="${ECR_URI%%/*}"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
docker build --platform linux/amd64 -t "$ECR_URI:$IMAGE_TAG" "$REPO_ROOT"
docker push "$ECR_URI:$IMAGE_TAG"

# ---------------------------------------------------------------------------
BUCKET="careroute-files-$ACCOUNT_ID"
step "Ensuring S3 bucket $BUCKET exists"
if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  info "Bucket already exists"
else
  aws s3 mb "s3://$BUCKET" --region "$REGION"
fi

# ---------------------------------------------------------------------------
# The committed manifests keep their placeholders; a filled-in copy is applied
# from a temp directory so account-specific values never end up in Git.
step "Rendering k8s/eks manifests with stack outputs"
MANIFESTS="$WORK_DIR/k8s-eks"
cp -R "$REPO_ROOT/k8s/eks" "$MANIFESTS"

render() {  # render <file> <sed expression>
  sed "$2" "$MANIFESTS/$1" > "$MANIFESTS/$1.tmp" && mv "$MANIFESTS/$1.tmp" "$MANIFESTS/$1"
}
render kustomization.yaml  "s|^\([[:space:]]*newName:\).*|\1 $ECR_URI|"
render kustomization.yaml  "s|^\([[:space:]]*newTag:\).*|\1 $IMAGE_TAG|"
render serviceaccount.yaml "s|\(eks.amazonaws.com/role-arn:\).*|\1 $API_ROLE_ARN|"
render fastapi.yaml        "s|redis://[^:/\"]*:6379|redis://$REDIS_ENDPOINT:6379|"
render ingress.yaml        "s|\(alb.ingress.kubernetes.io/security-groups:\).*|\1 $ALB_SG_ID|"
render aws-logging.yaml    "s|\(log_group_name[[:space:]]*\).*|\1/eks/$CLUSTER_NAME/$APP_NAMESPACE|"

# ---------------------------------------------------------------------------
step "Applying manifests"
DEPLOYMENT_EXISTED=false
kubectl get deployment cloud-fastapi -n "$APP_NAMESPACE" >/dev/null 2>&1 && DEPLOYMENT_EXISTED=true
kubectl apply -k "$MANIFESTS"
# Re-pushing the same tag does not change the spec, so force new pods to pull it.
if [ "$DEPLOYMENT_EXISTED" = true ]; then
  kubectl rollout restart deployment/cloud-fastapi -n "$APP_NAMESPACE"
fi
kubectl rollout status deployment/cloud-fastapi -n "$APP_NAMESPACE" --timeout=10m

# ---------------------------------------------------------------------------
step "Waiting for the ALB address"
ALB_HOST=""
for _ in $(seq 1 60); do
  ALB_HOST="$(kubectl get ingress cloud-fastapi -n "$APP_NAMESPACE" \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  [ -n "$ALB_HOST" ] && break
  sleep 10
done
[ -n "$ALB_HOST" ] || die "Ingress has no address after 10 minutes. Check: kubectl describe ingress cloud-fastapi -n $APP_NAMESPACE"
info "ALB: $ALB_HOST"

step "Waiting for http://$ALB_HOST/health (DNS and target registration take a few minutes)"
HEALTHY=false
for _ in $(seq 1 60); do
  if BODY="$(curl -fsS --max-time 5 "http://$ALB_HOST/health" 2>/dev/null)"; then
    info "Response: $BODY"
    HEALTHY=true
    break
  fi
  sleep 10
done

echo
if [ "$HEALTHY" = true ]; then
  printf '\033[1;32mDeployment complete.\033[0m API: http://%s\n' "$ALB_HOST"
else
  printf '\033[1;33mDeployed, but /health did not respond within 10 minutes.\033[0m\n'
  echo "Check: kubectl get pods -n $APP_NAMESPACE ; kubectl describe ingress cloud-fastapi -n $APP_NAMESPACE"
fi
[ -n "$BUDGET_EMAIL" ] && echo "Remember to confirm the AWS budget/SNS subscription emails sent to $BUDGET_EMAIL."
exit 0
