#!/usr/bin/env bash
# local stack on kind plus the caregiver and traveller apps and optionally one simulated trip, also for linux
#   bash scripts/macos/local-sim.sh --setup                          first time and after code changes
#   bash scripts/macos/local-sim.sh --scenario off_route --caregiver G-777
set -euo pipefail

CLUSTER=csc3104-local
NS=csc3104
CONFIG=k8s/local/kind-config.yaml
APP_URL=http://localhost:5174
TRAVELLER_URL=http://localhost:5173
# image name and build folder
IMAGES=(
  "cloud-fastapi:."
  "checkin-service:services/checkin-service"
  "alert-service:services/alert-service"
  "journey-service:services/journey-service"
  "easyread-service:services/easyread-service"
  "cloud-detectors:services/detectors"
  "coordinator:services/coordinator"
  "aggregator:services/aggregator"
)
# deployments running the images built here
BUILT=(cloud-fastapi checkin-service alert-service journey-service easyread-service coordinator aggregator
  detector-route detector-heading detector-dwell detector-progress)
# what must be ready before a trip, ollama left out as it is optional and large
DEPLOYMENTS=(cloud-redis cloud-localstack "${BUILT[@]}")
# local address and the service behind it
HEALTH=("8001 Check-in Service" "8005 Alert Service" "8006 Journey Service")

SETUP=0
SCENARIO=""
CAREGIVER=G-003
INTERVAL=1

usage() {
  echo "usage: bash scripts/macos/local-sim.sh [--setup] [--scenario NAME] [--caregiver G-...] [--interval SECONDS]"
  echo "scenarios: normal off_route wrong_direction long_dwell normal_bus_wait poor_gps"
}

die() {
  echo "error: $*" >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --setup) SETUP=1 ;;
    --scenario) SCENARIO="${2:?--scenario needs a name}"; shift ;;
    --caregiver) CAREGIVER="${2:?--caregiver needs an id}"; shift ;;
    --interval) INTERVAL="${2:?--interval needs seconds}"; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage; die "unknown option $1" ;;
  esac
  shift
done

cd "$(dirname "$0")/../.."

for tool in kind kubectl npm curl; do
  command -v "$tool" >/dev/null || die "$tool is not installed"
done
PYTHON=$(command -v python3 || command -v python || true)
[ -n "$PYTHON" ] || die "python is not installed"

# podman needs its own kind provider and full image names
PODMAN=0
if docker --version 2>/dev/null | grep -qi podman || { ! command -v docker >/dev/null && command -v podman >/dev/null; }; then
  PODMAN=1
  export KIND_EXPERIMENTAL_PROVIDER=podman
fi
ENGINE=$([ "$PODMAN" = 1 ] && echo podman || echo docker)

# cluster made from the current config so its ports reach localhost (30566, LocalStack, is the newest one)
cluster_ready() {
  kind get clusters 2>/dev/null | grep -qx "$CLUSTER" || return 1
  "$ENGINE" port "$CLUSTER-control-plane" 30566/tcp >/dev/null 2>&1 || return 2
}

# host ports in the config that something else already uses (a compose stack, another LocalStack), so kind
# couldn't create the cluster; with an old cluster ($1 = 1), the ports it publishes itself are its own and skipped
busy_ports() {
  local have_cluster=$1 container host busy=""
  while read -r container host; do
    if [ "$have_cluster" = 1 ] && "$ENGINE" port "$CLUSTER-control-plane" "$container/tcp" >/dev/null 2>&1; then
      continue
    fi
    # bash's /dev/tcp: connects only if something listens there
    if (exec 3<>"/dev/tcp/127.0.0.1/$host") 2>/dev/null; then busy="$busy $host"; fi
  done < <(sed -nE 's/.*containerPort: *([0-9]+), *hostPort: *([0-9]+).*/\1 \2/p' "$CONFIG")
  echo "${busy# }"
}

ensure_cluster() {
  local state=0 busy
  cluster_ready || state=$?
  [ "$state" = 0 ] && return
  [ "$SETUP" = 1 ] || {
    [ "$state" = 1 ] && die "no kind cluster $CLUSTER yet, run once with --setup"
    die "cluster $CLUSTER was made with an older $CONFIG (it has no port 4566 for LocalStack's pack and photo links). Run with --setup to recreate it; its data (Redis, LocalStack) is lost"
  }
  # before deleting anything: a port kind needs that is taken would leave no cluster at all
  busy=$(busy_ports "$([ "$state" = 2 ] && echo 1 || echo 0)")
  [ -z "$busy" ] || die "port(s) $busy on localhost are already in use, e.g. by services/journey-service/compose.journey.yaml (Journey Service on 8006, LocalStack on 4566). Stop it first (docker compose ... down); $CLUSTER was not changed"
  if [ "$state" = 2 ]; then
    echo "recreating $CLUSTER with $CONFIG, its data is lost"
    # rootless podman can report a network clean up error after removing the cluster
    kind delete cluster --name "$CLUSTER" || true
    kind get clusters 2>/dev/null | grep -qx "$CLUSTER" && die "could not delete $CLUSTER"
  fi
  kind create cluster --name "$CLUSTER" --config "$CONFIG"
}

build_and_load() {
  local entry name folder
  for entry in "${IMAGES[@]}"; do
    name="${entry%%:*}"
    folder="${entry#*:}"
    echo "== $name"
    "$ENGINE" build -q -t "$name:local" "$folder"
    # podman names it localhost so the pods would not find it
    if [ "$PODMAN" = 1 ]; then
      podman tag "localhost/$name:local" "docker.io/library/$name:local"
      kind load docker-image "docker.io/library/$name:local" --name "$CLUSTER"
    else
      kind load docker-image "$name:local" --name "$CLUSTER"
    fi
  done
}

ensure_secret() {
  if [ "$SETUP" = 1 ]; then
    [ -f .env ] || die "no .env in the repo root, see the repo README"
    grep -qE '^LOCALSTACK_AUTH_TOKEN=.+' .env || die "LOCALSTACK_AUTH_TOKEN is empty in .env"
    kubectl apply -f k8s/local/namespace.yaml >/dev/null
    kubectl create secret generic localstack-auth --from-env-file=.env -n "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  else
    kubectl get secret localstack-auth -n "$NS" >/dev/null 2>&1 || die "no localstack-auth secret, run with --setup"
  fi
}

start_stack() {
  local d
  for k in k8s/local k8s/local/detectors k8s/local/analysis k8s/local/expose; do
    kubectl apply -k "$k" >/dev/null
  done
  # same image tags so pods only pick up new builds when restarted, never localstack as it would lose every table
  [ "$SETUP" = 1 ] && kubectl rollout restart deployment "${BUILT[@]}" -n "$NS" >/dev/null
  echo "waiting for the services"
  for d in "${DEPLOYMENTS[@]}"; do
    kubectl rollout status "deployment/$d" -n "$NS" --timeout=300s >/dev/null || die "$d did not become ready, see kubectl get pods -n $NS"
  done
}

# poll until url answers or give up
wait_for() {
  local url=$1 what=$2 i
  for i in $(seq 1 60); do
    curl -sf -m 2 -o /dev/null "$url" && return
    sleep 1
  done
  die "$what is not answering at $url"
}

APP_PID=""
TRAVELLER_PID=""
stop_app() {
  [ -n "$APP_PID" ] && kill "$APP_PID" 2>/dev/null || true
  [ -n "$TRAVELLER_PID" ] && kill "$TRAVELLER_PID" 2>/dev/null || true
}

start_app() {
  if curl -sf -m 2 -o /dev/null "$APP_URL"; then
    echo "caregiver app already running"
    return
  fi
  (cd apps/caregiver && { [ -d node_modules ] || npm ci --no-audit --no-fund >/dev/null; })
  # vite directly so stopping it stops the whole app
  (cd apps/caregiver && exec ./node_modules/.bin/vite --strictPort >/tmp/careroute-caregiver.log 2>&1) &
  APP_PID=$!
  trap stop_app EXIT INT TERM
  wait_for "$APP_URL" "the caregiver app (log in /tmp/careroute-caregiver.log)"
}

# the traveller app, where the caregiver app's trip links lead
start_traveller_app() {
  if curl -sf -m 2 -o /dev/null "$TRAVELLER_URL"; then
    echo "traveller app already running"
    return
  fi
  (cd apps/traveller && { [ -d node_modules ] || npm ci --no-audit --no-fund >/dev/null; })
  (cd apps/traveller && exec ./node_modules/.bin/vite --strictPort >/tmp/careroute-traveller.log 2>&1) &
  TRAVELLER_PID=$!
  trap stop_app EXIT INT TERM
  wait_for "$TRAVELLER_URL" "the traveller app (log in /tmp/careroute-traveller.log)"
}

ensure_cluster
kubectl config use-context "kind-$CLUSTER" >/dev/null
[ "$SETUP" = 1 ] && build_and_load
ensure_secret
start_stack
for entry in "${HEALTH[@]}"; do
  wait_for "http://localhost:${entry%% *}/health" "${entry#* }"
done
start_app
start_traveller_app

if [ -n "$SCENARIO" ]; then
  "$PYTHON" -m tools.simulator live --scenario "$SCENARIO" --caregiver "$CAREGIVER" --interval "$INTERVAL"
fi

echo
echo "caregiver app: $APP_URL  act as $CAREGIVER"
echo "traveller app: $TRAVELLER_URL  open a trip's link from the caregiver app's Trips tab"
echo "aggregator dashboard: http://localhost:8002/dashboard"
echo "another trip: $PYTHON -m tools.simulator live --scenario off_route --caregiver $CAREGIVER"
# keep running while either app started here runs, so the exit trap doesn't stop the other one
if [ -n "$APP_PID$TRAVELLER_PID" ]; then
  echo "press Ctrl+C to stop the apps, the cluster keeps running"
  wait $APP_PID $TRAVELLER_PID || true
fi
