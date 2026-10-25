#!/usr/bin/env bash
# macOS/Linux version of ../windows/build-lambda.ps1: builds deploy/lambda/dist/disruption-lambda.zip for the
# disruption checker (handler app.lambda_handler.handler).
#
# Runs in a python:3.12 Linux container so the packages match the Lambda runtime whatever your own machine is
# (on Apple Silicon Docker emulates linux/amd64). The zip holds our app files (see ../lambda/make_zip.py) plus httpx
# and redis; boto3 comes with the Lambda runtime. --verify then unpacks the zip into a clean container, adds boto3 the
# way the runtime would, imports the handler and checks that none of the API's packages (FastAPI, PyJWT) came along.
#
# Usage: bash services/journey-service/deploy/macos/build-lambda.sh [--verify] [--out-dir DIR]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
LAMBDA_DIR="$(cd "$SCRIPT_DIR/../lambda" && pwd)"   # make_zip.py
OUT_DIR="$LAMBDA_DIR/dist"
VERIFY=false

while [ $# -gt 0 ]; do
  case "$1" in
    --verify)  VERIFY=true; shift ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

die() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "'docker' is not installed or not on PATH"
docker info >/dev/null 2>&1 || die "Docker is not running. Start Docker Desktop and retry."
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
ZIP="$OUT_DIR/disruption-lambda.zip"

printf '\033[1;36m==> Building the package in python:3.12 (linux/amd64)\033[0m\n'
rm -f "$ZIP"
docker run --rm --platform linux/amd64 \
  -v "$SERVICE_DIR:/src:ro" -v "$LAMBDA_DIR:/tools:ro" -v "$OUT_DIR:/out" \
  python:3.12-slim bash -c "set -e; pip install --no-cache-dir --quiet --target /build httpx redis; python /tools/make_zip.py /src /build /out/disruption-lambda.zip" \
  || die "The build failed"

if [ "$VERIFY" = true ]; then
  printf '\033[1;36m==> Verifying the package in a clean container\033[0m\n'
  # Next to the zip, not in a temp dir: Docker Desktop only mounts the folders it shares.
  CHECK_FILE="$OUT_DIR/verify_package.py"
  trap 'rm -f "$CHECK_FILE"' EXIT
  cat > "$CHECK_FILE" <<'PY'
import sys, zipfile
zipfile.ZipFile("/out/disruption-lambda.zip").extractall("/pkg")
sys.path[:0] = ["/pkg", "/runtime"]            # the package, then boto3 as the Lambda runtime provides it
import app.lambda_handler
ours = sorted(m.split(".")[1] for m in sys.modules if m.startswith("app.") and m.count(".") == 1)
bad = [p for p in ("fastapi", "starlette", "pydantic", "jwt") if p in sys.modules]
print("app modules:", ", ".join(ours))
print("API packages loaded:", bad or "none")
sys.exit(1 if bad else 0)
PY
  docker run --rm --platform linux/amd64 -v "$OUT_DIR:/out:ro" python:3.12-slim \
    bash -c "set -e; pip install --no-cache-dir --quiet --target /runtime boto3; python /out/verify_package.py" \
    || die "Verification failed"
fi

printf '\033[1;32mBuilt %s\033[0m\n' "$ZIP"
