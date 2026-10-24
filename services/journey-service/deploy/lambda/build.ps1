<#
.SYNOPSIS
  Builds dist\disruption-lambda.zip for the disruption checker (handler app.lambda_handler.handler).

.DESCRIPTION
  Runs in a python:3.12 Linux container so the packages match the Lambda runtime whatever your own machine is.
  The zip holds our six app files (see make_zip.py) plus httpx and redis; boto3 comes with the Lambda runtime.
  -Verify then unpacks the zip into a clean container, adds boto3 the way the runtime would, imports the handler
  and checks that none of the API's packages (FastAPI, PyJWT) came along.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File services\journey-service\deploy\lambda\build.ps1 -Verify
#>
param(
  [string]$OutDir = (Join-Path $PSScriptRoot 'dist'),
  [switch]$Verify
)

$ErrorActionPreference = 'Stop'
$ServiceDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$Zip = Join-Path $OutDir 'disruption-lambda.zip'

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "'docker' is not installed or not on PATH" }
docker info 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Docker is not running. Start Docker Desktop and retry.' }

Write-Host '==> Building the package in python:3.12 (linux/amd64)' -ForegroundColor Cyan
Remove-Item $Zip -ErrorAction SilentlyContinue
docker run --rm --platform linux/amd64 `
  -v "${ServiceDir}:/src:ro" -v "${PSScriptRoot}:/tools:ro" -v "${OutDir}:/out" `
  python:3.12-slim bash -c "set -e; pip install --no-cache-dir --quiet --target /build httpx redis; python /tools/make_zip.py /src /build /out/disruption-lambda.zip"
if ($LASTEXITCODE -ne 0) { throw 'The build failed' }

if ($Verify) {
  Write-Host '==> Verifying the package in a clean container' -ForegroundColor Cyan
  $check = @'
import sys, zipfile
zipfile.ZipFile("/out/disruption-lambda.zip").extractall("/pkg")
sys.path[:0] = ["/pkg", "/runtime"]            # the package, then boto3 as the Lambda runtime provides it
import app.lambda_handler
ours = sorted(m.split(".")[1] for m in sys.modules if m.startswith("app.") and m.count(".") == 1)
bad = [p for p in ("fastapi", "starlette", "pydantic", "jwt") if p in sys.modules]
print("app modules:", ", ".join(ours))
print("API packages loaded:", bad or "none")
sys.exit(1 if bad else 0)
'@
  $checkFile = Join-Path $OutDir 'verify_package.py'
  [IO.File]::WriteAllText($checkFile, $check, (New-Object System.Text.UTF8Encoding $false))
  try {
    docker run --rm --platform linux/amd64 -v "${OutDir}:/out:ro" python:3.12-slim `
      bash -c "set -e; pip install --no-cache-dir --quiet --target /runtime boto3; python /out/verify_package.py"
    if ($LASTEXITCODE -ne 0) { throw 'Verification failed' }
  } finally {
    Remove-Item $checkFile -ErrorAction SilentlyContinue
  }
}

Write-Host "Built $Zip" -ForegroundColor Green
