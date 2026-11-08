#!/usr/bin/env bash
# macOS/Linux version of ../windows/deploy-eks.ps1: deploys the Journey Service onto the platform's EKS cluster (the stack the
# platform owner's scripts/macos/deploy.sh creates).
#
# Run the platform's deploy script first; this script only adds our service to what it built:
#   stack outputs -> kubeconfig -> pack bucket CORS -> Secret for the LTA/OneMap keys -> build and push our image
#   to the stack's ECR repository (tag journey-<tag>) -> render and apply deploy/eks/journey-service.yaml
#   -> wait for the rollout -> /ready check through a port-forward.
#
# Names: tables are <prefix>-routes and <prefix>-trips, the pack bucket is <prefix>-files-<account id>. The prefix is
# read from the platform stack's ResourcePrefix parameter (the team decided on 'careroute'; this warns if the stack
# was built with anything else). --resource-prefix and --bucket override.
#
# Safe to re-run. It never touches the platform's own manifests, and the API keys are read from .env and go
# straight into a Kubernetes Secret (a temporary file that is deleted at the end), never into Git or the image.
#
# NOT TESTED AGAINST REAL AWS. --render-only checks the manifest without any AWS call.
#
# Usage: bash services/journey-service/deploy/macos/deploy-eks.sh
#          [--region REGION] [--stack-name NAME] [--tag v1] [--namespace careroute]
#          [--resource-prefix PREFIX] [--bucket NAME] [--env-file PATH] [--cors-origin ORIGIN]
#        bash services/journey-service/deploy/macos/deploy-eks.sh --render-only
#          --image URI --redis-endpoint HOST --bucket NAME --out-file PATH [--resource-prefix PREFIX]
# Every option can also be set through the matching environment variable below.

set -euo pipefail

STACK_NAME="${STACK_NAME:-careroute-stack-1}"
REGION="${REGION:-ap-southeast-1}"
RESOURCE_PREFIX="${RESOURCE_PREFIX:-}"
IMAGE_TAG="${IMAGE_TAG:-v1}"
APP_NAMESPACE="${APP_NAMESPACE:-careroute}"
ENV_FILE="${ENV_FILE:-}"
CORS_ORIGIN="${CORS_ORIGIN:-*}"
RENDER_ONLY=false
IMAGE="${IMAGE:-}"
REDIS_ENDPOINT="${REDIS_ENDPOINT:-}"
BUCKET="${BUCKET:-}"
OUT_FILE="${OUT_FILE:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --region)          REGION="$2"; shift 2 ;;
    --stack-name)      STACK_NAME="$2"; shift 2 ;;
    --tag)             IMAGE_TAG="$2"; shift 2 ;;
    --namespace)       APP_NAMESPACE="$2"; shift 2 ;;
    --resource-prefix) RESOURCE_PREFIX="$2"; shift 2 ;;
    --bucket)          BUCKET="$2"; shift 2 ;;
    --env-file)        ENV_FILE="$2"; shift 2 ;;
    --cors-origin)     CORS_ORIGIN="$2"; shift 2 ;;
    --render-only)     RENDER_ONLY=true; shift ;;
    --image)           IMAGE="$2"; shift 2 ;;
    --redis-endpoint)  REDIS_ENDPOINT="$2"; shift 2 ;;
    --out-file)        OUT_FILE="$2"; shift 2 ;;
    -h|--help)         sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$SERVICE_DIR/../.." && pwd)"
MANIFEST="$SCRIPT_DIR/../eks/journey-service.yaml"
. "$SCRIPT_DIR/resource-names.sh"
[ -n "$ENV_FILE" ] || ENV_FILE="$REPO_ROOT/.env"
WORK_DIR="$(mktemp -d)"
FORWARD_PID=""
cleanup() {
  [ -z "$FORWARD_PID" ] || kill "$FORWARD_PID" 2>/dev/null || true
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

stack_output() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

# Escapes a value for the right-hand side of a sed s|...|...| command.
sed_value() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }

# The committed manifest keeps its placeholders; this fills them in and refuses to continue if any is left.
render_manifest() {  # image redis bucket prefix out-file
  sed -e "s|<JOURNEY_IMAGE>|$(sed_value "$1")|g" -e "s|<REDIS_ENDPOINT>|$(sed_value "$2")|g" \
      -e "s|<PACKS_BUCKET>|$(sed_value "$3")|g" -e "s|<RESOURCE_PREFIX>|$(sed_value "$4")|g" "$MANIFEST" > "$5"
  local left
  left="$(grep -Eo '<[A-Z_]+>' "$5" | head -n 1 || true)"
  [ -z "$left" ] || die "Manifest still has an unfilled placeholder: $left"
}

# Prints one KEY=VALUE from the .env file (the last one wins, surrounding quotes removed); prints nothing if absent.
env_value() {
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" | tail -n 1 \
    | tr -d '\r' | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

if [ "$RENDER_ONLY" = true ]; then
  [ -n "$IMAGE" ] && [ -n "$REDIS_ENDPOINT" ] && [ -n "$BUCKET" ] && [ -n "$OUT_FILE" ] \
    || die "--render-only needs --image, --redis-endpoint, --bucket and --out-file"
  render_manifest "$IMAGE" "$REDIS_ENDPOINT" "$BUCKET" "${RESOURCE_PREFIX:-$TEAM_PREFIX}" "$OUT_FILE"
  info "Rendered manifest written to $OUT_FILE"
  exit 0
fi

# ---------------------------------------------------------------------------
step "Checking prerequisites"
for cmd in aws kubectl docker curl; do
  command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' is not installed or not on PATH"
done
docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop and retry."
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || die "AWS credentials are not configured (run 'aws configure')"
STATUS="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
case "$STATUS" in
  *_COMPLETE) ;;
  *) die "Stack $STACK_NAME is not deployed in $REGION (status: $STATUS). Run the platform's deploy script first." ;;
esac
info "Account $ACCOUNT_ID, region $REGION, stack $STACK_NAME ($STATUS)"

# ---------------------------------------------------------------------------
step "Reading the stack outputs"
ECR_URI="$(stack_output EcrRepositoryUri)"
REDIS_ENDPOINT="$(stack_output RedisEndpoint)"
CLUSTER_NAME="$(stack_output EksClusterName)"
for pair in "EcrRepositoryUri=$ECR_URI" "RedisEndpoint=$REDIS_ENDPOINT" "EksClusterName=$CLUSTER_NAME"; do
  case "${pair#*=}" in ""|None) die "The stack has no output named ${pair%%=*}" ;; esac
done

RESOURCE_PREFIX="$(resolve_resource_prefix "$STACK_NAME" "$REGION" "$RESOURCE_PREFIX")"
BUCKET="$(shared_bucket_name "$RESOURCE_PREFIX" "$ACCOUNT_ID" "$BUCKET")"
IMAGE="$ECR_URI:journey-$IMAGE_TAG"
info "Cluster $CLUSTER_NAME | Redis $REDIS_ENDPOINT | image $IMAGE | pack bucket $BUCKET"

# The tables belong to the platform stack. Checked here with YOUR credentials (the pods' role cannot DescribeTable),
# so a wrong prefix is caught now instead of as a failing /ready later.
for table in "$RESOURCE_PREFIX-routes" "$RESOURCE_PREFIX-trips"; do
  aws dynamodb describe-table --table-name "$table" --region "$REGION" --query 'Table.TableStatus' --output text >/dev/null 2>&1 \
    || die "DynamoDB table $table does not exist. Stack $STACK_NAME was built with prefix '$RESOURCE_PREFIX'; if its tables use another prefix, pass --resource-prefix."
  info "Table $table found"
done

step "Connecting kubectl to the cluster"
aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER_NAME"

# ---------------------------------------------------------------------------
step "Pack bucket $BUCKET: CORS for downloads from the traveller app"
aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 \
  || die "Bucket $BUCKET does not exist. The platform's deploy script creates it (its name must be <prefix>-files-<account id>, with the stack's prefix, or the pods' role cannot reach it); run that first, or pass --bucket."
# The pods' role cannot set this (it only has object read/write), so it is done here with your credentials.
printf '{"CORSRules":[{"AllowedMethods":["GET"],"AllowedOrigins":["%s"],"AllowedHeaders":["*"],"MaxAgeSeconds":600}]}' \
  "$CORS_ORIGIN" > "$WORK_DIR/cors.json"
aws s3api put-bucket-cors --bucket "$BUCKET" --cors-configuration "file://$WORK_DIR/cors.json"
info "Allowed origin: $CORS_ORIGIN (tighten it to the traveller app's address with --cors-origin)"

# ---------------------------------------------------------------------------
step "Creating the Secret with the LTA, OneMap and Google Maps keys"
SECRET_FILE="$WORK_DIR/secret.env"
( umask 077; : > "$SECRET_FILE" )
for name in LTA_API_KEY ONEMAP_API_EMAIL ONEMAP_API_PASSWORD ONEMAP_API_KEY GOOGLE_MAPS_API_KEY; do
  value="$(env_value "$name")"
  if [ -n "$value" ]; then
    printf '%s=%s\n' "$name" "$value" >> "$SECRET_FILE"
    info "$name found in $ENV_FILE"
  else
    info "NOTE: $name not found in $ENV_FILE"
  fi
done
unset value
if ! { grep -q '^ONEMAP_API_EMAIL=' "$SECRET_FILE" && grep -q '^ONEMAP_API_PASSWORD=' "$SECRET_FILE"; } \
   && ! grep -q '^ONEMAP_API_KEY=' "$SECRET_FILE"; then
  info "WARNING: no OneMap account (ONEMAP_API_EMAIL + ONEMAP_API_PASSWORD) or token (ONEMAP_API_KEY): route planning will not work"
fi
kubectl get namespace "$APP_NAMESPACE" >/dev/null 2>&1 \
  || die "Namespace $APP_NAMESPACE does not exist in the cluster. The platform's deploy script creates it."
kubectl create secret generic journey-api-keys --from-env-file="$SECRET_FILE" -n "$APP_NAMESPACE" --dry-run=client -o yaml \
  | kubectl apply -f - || die "Could not create the Secret"

# ---------------------------------------------------------------------------
step "Building and pushing $IMAGE"
ECR_REGISTRY="${ECR_URI%%/*}"
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR_REGISTRY"
# Fargate runs linux/amd64; on Apple Silicon Docker builds it by emulation (slower, but the right architecture).
docker build --platform linux/amd64 -t "$IMAGE" "$SERVICE_DIR"
docker push "$IMAGE"

# ---------------------------------------------------------------------------
step "Applying the manifest"
RENDERED="$WORK_DIR/journey-service.yaml"
render_manifest "$IMAGE" "$REDIS_ENDPOINT" "$BUCKET" "$RESOURCE_PREFIX" "$RENDERED"
EXISTED=false
kubectl get deployment journey-service -n "$APP_NAMESPACE" >/dev/null 2>&1 && EXISTED=true
kubectl apply -f "$RENDERED"
# Re-pushing the same tag does not change the spec, so force new pods to pull it.
[ "$EXISTED" = false ] || kubectl rollout restart deployment/journey-service -n "$APP_NAMESPACE"
kubectl rollout status deployment/journey-service -n "$APP_NAMESPACE" --timeout=10m

# ---------------------------------------------------------------------------
step "Checking /ready through a port-forward"
kubectl port-forward svc/journey-service 18006:8000 -n "$APP_NAMESPACE" >/dev/null 2>&1 &
FORWARD_PID=$!
READY=false
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 5
  if body="$(curl -fsS --max-time 5 http://localhost:18006/ready 2>&1)"; then
    info "GET /ready -> 200 $body"
    READY=true
    break
  fi
  info "not ready yet: $body"
done
kill "$FORWARD_PID" 2>/dev/null || true
FORWARD_PID=""

echo
if [ "$READY" = true ]; then
  printf '\033[1;32mJourney Service deployed and ready (inside the cluster; no Ingress).\033[0m\n'
else
  printf '\033[1;33m/ready did not answer 200. The 503 body names the failing store, e.g. "routes table: AccessDeniedException".\033[0m\n'
  echo "Check: kubectl logs deploy/journey-service -n $APP_NAMESPACE ; kubectl describe pod -l app=journey-service -n $APP_NAMESPACE"
fi
echo "Reach it with: kubectl port-forward svc/journey-service 8006:8000 -n $APP_NAMESPACE"
