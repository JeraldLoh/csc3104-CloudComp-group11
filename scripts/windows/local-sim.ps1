# local stack on kind plus the caregiver app and optionally one simulated trip
#   .\scripts\windows\local-sim.ps1 -Setup                          first time and after code changes
#   .\scripts\windows\local-sim.ps1 -Scenario off_route -Caregiver G-777
param(
  [switch]$Setup,
  [ValidateSet("normal", "off_route", "wrong_direction", "long_dwell", "normal_bus_wait", "poor_gps")]
  [string]$Scenario,
  [string]$Caregiver = "G-003",
  [ValidateRange(0.01, 10)]
  [double]$Interval = 1
)

# native tools write progress to stderr so failures are checked by exit code instead
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..\..")

$Cluster = "csc3104-local"
$Ns = "csc3104"
$Config = "k8s/local/kind-config.yaml"
$AppUrl = "http://localhost:5174"
$AppLog = Join-Path $env:TEMP "careroute-caregiver.log"
# image name and build folder
$Images = [ordered]@{
  "cloud-fastapi"    = "."
  "checkin-service"  = "services/checkin-service"
  "alert-service"    = "services/alert-service"
  "journey-service"  = "services/journey-service"
  "easyread-service" = "services/easyread-service"
  "cloud-detectors"  = "services/detectors"
  "coordinator"      = "services/coordinator"
  "aggregator"       = "services/aggregator"
}
# deployments running the images built here
$Built = @("cloud-fastapi", "checkin-service", "alert-service", "journey-service", "easyread-service",
  "coordinator", "aggregator", "detector-route", "detector-heading", "detector-dwell", "detector-progress")
# what must be ready before a trip, ollama left out as it is optional and large
$Deployments = @("cloud-redis", "cloud-localstack") + $Built
# local port and the service behind it
$Health = [ordered]@{ "8001" = "Check-in Service"; "8005" = "Alert Service"; "8006" = "Journey Service" }

function Fail([string]$Message) {
  Write-Host "error: $Message" -ForegroundColor Red
  exit 1
}

# run a tool and stop on a non zero exit code
function Run([string]$Exe, [string[]]$ArgList) {
  & $Exe @ArgList
  if ($LASTEXITCODE -ne 0) { Fail "$Exe $($ArgList -join ' ') failed" }
}

foreach ($tool in "kind", "kubectl", "npm", "node") {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "$tool is not installed" }
}
$Python = @("py", "python", "python3") | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if (-not $Python) { Fail "python is not installed" }

# podman needs its own kind provider and full image names
$Podman = (-not (Get-Command docker -ErrorAction SilentlyContinue)) -or ((docker --version 2>$null) -match "podman")
if ($Podman) {
  if (-not (Get-Command podman -ErrorAction SilentlyContinue)) { Fail "docker is not installed" }
  $env:KIND_EXPERIMENTAL_PROVIDER = "podman"
}
$Engine = if ($Podman) { "podman" } else { "docker" }

# 0 ready, 1 missing, 2 made without the config so its ports do not reach localhost
function Get-ClusterState {
  $clusters = kind get clusters 2>$null
  if (-not ($clusters -contains $Cluster)) { return 1 }
  & $Engine port "$Cluster-control-plane" 30001/tcp 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { return 2 }
  return 0
}

function Initialize-Cluster {
  $state = Get-ClusterState
  if ($state -eq 0) { return }
  if (-not $Setup) {
    if ($state -eq 1) { Fail "no kind cluster $Cluster yet, run once with -Setup" }
    Fail "cluster $Cluster was made without $Config, run with -Setup to recreate it"
  }
  if ($state -eq 2) {
    Write-Host "recreating $Cluster with $Config, its data is lost"
    # rootless podman can report a network clean up error after removing the cluster
    kind delete cluster --name $Cluster
    if ((kind get clusters 2>$null) -contains $Cluster) { Fail "could not delete $Cluster" }
  }
  Run kind @("create", "cluster", "--name", $Cluster, "--config", $Config)
}

function Build-Images {
  foreach ($name in $Images.Keys) {
    Write-Host "== $name"
    Run $Engine @("build", "-q", "-t", "${name}:local", $Images[$name])
    # podman names it localhost so the pods would not find it
    if ($Podman) {
      Run podman @("tag", "localhost/${name}:local", "docker.io/library/${name}:local")
      Run kind @("load", "docker-image", "docker.io/library/${name}:local", "--name", $Cluster)
    } else {
      Run kind @("load", "docker-image", "${name}:local", "--name", $Cluster)
    }
  }
}

function Initialize-Secret {
  if ($Setup) {
    if (-not (Test-Path .env)) { Fail "no .env in the repo root, see the repo README" }
    if (-not (Select-String -Path .env -Pattern '^LOCALSTACK_AUTH_TOKEN=.+' -Quiet)) { Fail "LOCALSTACK_AUTH_TOKEN is empty in .env" }
    Run kubectl @("apply", "-f", "k8s/local/namespace.yaml")
    $yaml = kubectl create secret generic localstack-auth --from-env-file=.env -n $Ns --dry-run=client -o yaml
    if ($LASTEXITCODE -ne 0) { Fail "could not read .env" }
    $yaml | kubectl apply -f - | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "could not save the localstack-auth secret" }
  } else {
    kubectl get secret localstack-auth -n $Ns 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "no localstack-auth secret, run with -Setup" }
  }
}

function Start-Stack {
  foreach ($k in "k8s/local", "k8s/local/detectors", "k8s/local/analysis", "k8s/local/expose") {
    Run kubectl @("apply", "-k", $k)
  }
  # same image tags so pods only pick up new builds when restarted, never localstack as it would lose every table
  if ($Setup) { Run kubectl (@("rollout", "restart", "deployment") + $Built + @("-n", $Ns)) }
  Write-Host "waiting for the services"
  foreach ($d in $Deployments) {
    kubectl rollout status "deployment/$d" -n $Ns --timeout=300s | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "$d did not become ready, see kubectl get pods -n $Ns" }
  }
}

function Test-Url([string]$Url) {
  try {
    Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 | Out-Null
    return $true
  } catch {
    return $false
  }
}

# poll until the url answers or give up
function Wait-Url([string]$Url, [string]$What) {
  for ($i = 0; $i -lt 60; $i++) {
    if (Test-Url $Url) { return }
    Start-Sleep -Seconds 1
  }
  Fail "$What is not answering at $Url"
}

function Start-App {
  if (Test-Url $AppUrl) {
    Write-Host "caregiver app already running"
    return $null
  }
  if (-not (Test-Path apps/caregiver/node_modules)) {
    Push-Location apps/caregiver
    npm ci --no-audit --no-fund
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Fail "npm ci failed" }
  }
  # vite through node so stopping it stops the whole app
  $app = Start-Process -FilePath node -ArgumentList "node_modules/vite/bin/vite.js", "--strictPort" `
    -WorkingDirectory apps/caregiver -PassThru -NoNewWindow -RedirectStandardOutput $AppLog -RedirectStandardError "$AppLog.err"
  Wait-Url $AppUrl "the caregiver app (log in $AppLog)"
  return $app
}

Initialize-Cluster
Run kubectl @("config", "use-context", "kind-$Cluster")
if ($Setup) { Build-Images }
Initialize-Secret
Start-Stack
foreach ($port in $Health.Keys) { Wait-Url "http://localhost:$port/health" $Health[$port] }
$app = Start-App

try {
  if ($Scenario) {
    & $Python -m tools.simulator live --scenario $Scenario --caregiver $Caregiver --interval $Interval
  }
  Write-Host ""
  Write-Host "caregiver app: $AppUrl  act as $Caregiver"
  Write-Host "aggregator dashboard: http://localhost:8002/dashboard"
  Write-Host "another trip: $Python -m tools.simulator live --scenario off_route --caregiver $Caregiver"
  if ($app) {
    Write-Host "press Ctrl+C to stop the app, the cluster keeps running"
    Wait-Process -Id $app.Id
  }
} finally {
  if ($app -and -not $app.HasExited) { Stop-Process -Id $app.Id -Force }
}
