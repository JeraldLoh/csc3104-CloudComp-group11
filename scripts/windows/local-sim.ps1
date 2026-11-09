# local stack on kind plus the caregiver and traveller apps and optionally one simulated trip
#   .\scripts\windows\local-sim.ps1 -Setup                          first time and after code changes
#   .\scripts\windows\local-sim.ps1 -Scenario off_route -Caregiver G-777
#   .\scripts\windows\local-sim.ps1 -Cognito                        with real logins (the careroute-cognito stack)
param(
  [switch]$Setup,
  # Logins (issue 1.5): the Alert, Check-in and Journey Services check Cognito tokens and both apps show their login
  # pages. Without it they use the local stand-ins ("Act as", trip links), whatever an earlier run left.
  [switch]$Cognito,
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
$TravellerUrl = "http://localhost:5173"
$TravellerLog = Join-Path $env:TEMP "careroute-traveller.log"
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
  "heartbeat-monitor" = "services/heartbeat-monitor"
}
# deployments running the images built here
$Built = @("cloud-fastapi", "checkin-service", "alert-service", "journey-service", "easyread-service",
  "coordinator", "aggregator", "heartbeat-monitor", "detector-route", "detector-heading", "detector-dwell", "detector-progress")
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
if ($Cognito) {
  # a simulated trip's check-ins carry no login token, so the Check-in Service would refuse every one
  if ($Scenario) { Fail "-Scenario sends check-ins without a login token; run it without -Cognito" }
  if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { Fail "-Cognito needs the AWS CLI, to read the user pool's IDs" }
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

# 0 ready, 1 missing, 2 made without the current config so its ports do not reach localhost
# (30566, LocalStack, is the newest port)
function Get-ClusterState {
  $clusters = kind get clusters 2>$null
  if (-not ($clusters -contains $Cluster)) { return 1 }
  & $Engine port "$Cluster-control-plane" 30566/tcp 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { return 2 }
  return 0
}

# Host ports in $Config that something else already uses (a compose stack, another LocalStack), so kind couldn't
# create the cluster. With $HaveCluster, ports the old cluster publishes itself are its own and are skipped.
function Get-BusyPorts([bool]$HaveCluster) {
  $busy = @()
  foreach ($m in Select-String -Path $Config -Pattern 'containerPort:\s*(\d+),\s*hostPort:\s*(\d+)') {
    $containerPort = $m.Matches[0].Groups[1].Value
    $hostPort = [int]$m.Matches[0].Groups[2].Value
    if ($HaveCluster) {
      & $Engine port "$Cluster-control-plane" "$containerPort/tcp" 2>$null | Out-Null
      if ($LASTEXITCODE -eq 0) { continue }
    }
    $tcp = New-Object System.Net.Sockets.TcpClient
    try { if ($tcp.ConnectAsync("127.0.0.1", $hostPort).Wait(500)) { $busy += $hostPort } } catch { } finally { $tcp.Dispose() }
  }
  return , $busy
}

function Initialize-Cluster {
  $state = Get-ClusterState
  if ($state -eq 0) { return }
  if (-not $Setup) {
    if ($state -eq 1) { Fail "no kind cluster $Cluster yet, run once with -Setup" }
    Fail ("cluster $Cluster was made with an older $Config (it has no port 4566 for LocalStack's pack and photo " +
          "links). Run with -Setup to recreate it; its data (Redis, LocalStack) is lost")
  }
  # Before deleting anything: a port kind needs that is taken would leave no cluster at all
  $busy = Get-BusyPorts ($state -eq 2)
  if ($busy.Count) {
    Fail ("port(s) $($busy -join ', ') on localhost are already in use, e.g. by services/journey-service/compose.journey.yaml " +
          "(Journey Service on 8006, LocalStack on 4566). Stop it first (docker compose ... down); $Cluster was not changed")
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
    # The Journey Service's LTA, OneMap and Google Maps keys (k8s/local/journey-service.yaml); without them it runs on
    # saved data and saves routes without Street View photos
    $yaml = kubectl create secret generic journey-api-keys --from-env-file=.env -n $Ns --dry-run=client -o yaml
    $yaml | kubectl apply -f - | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "could not save the journey-api-keys secret" }
  } else {
    kubectl get secret localstack-auth -n $Ns 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "no localstack-auth secret, run with -Setup" }
  }
}

# the user pool's IDs, from the careroute-cognito stack (repo README, "Logins (Cognito)")
$CognitoStack = "careroute-cognito"
$CognitoRegion = "ap-southeast-1"
function Get-CognitoIds {
  $json = aws cloudformation describe-stacks --stack-name $CognitoStack --region $CognitoRegion --query "Stacks[0].Outputs" --output json 2>$null
  if ($LASTEXITCODE -ne 0) { Fail "no $CognitoStack stack in $CognitoRegion (or no AWS login), see the repo README, Logins (Cognito)" }
  $ids = @{}
  foreach ($o in ($json | Out-String | ConvertFrom-Json)) { $ids[$o.OutputKey] = $o.OutputValue }
  if (-not ($ids["UserPoolId"] -and $ids["UserPoolClientId"])) { Fail "$CognitoStack has no UserPoolId or UserPoolClientId output" }
  return $ids
}

# Applying k8s/local puts the Alert Service back on AUTH_MODE=dev; this sets the services to the mode of this run.
$AuthServices = @("alert-service", "checkin-service", "journey-service")
$CognitoVars = @("COGNITO_REGION", "COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID")
function Set-AuthMode {
  if ($Cognito) {
    $vars = @("AUTH_MODE=cognito", "COGNITO_REGION=$CognitoRegion", "COGNITO_USER_POOL_ID=$($CognitoIds['UserPoolId'])",
              "COGNITO_CLIENT_ID=$($CognitoIds['UserPoolClientId'])")
    Run kubectl (@("set", "env") + ($AuthServices | ForEach-Object { "deployment/$_" }) + @("-n", $Ns) + $vars) | Out-Null
    return
  }
  foreach ($d in $AuthServices) {
    # only the settings an earlier -Cognito run added; the Alert Service's own AUTH_MODE=dev stays
    $names = (kubectl get "deployment/$d" -n $Ns -o "jsonpath={.spec.template.spec.containers[0].env[*].name}") -split " "
    $remove = @($CognitoVars | Where-Object { $names -contains $_ } | ForEach-Object { "$_-" })
    if ($d -eq "alert-service") { $remove += "AUTH_MODE=dev" } elseif ($names -contains "AUTH_MODE") { $remove += "AUTH_MODE-" }
    if ($remove.Count) { Run kubectl (@("set", "env", "deployment/$d", "-n", $Ns) + $remove) | Out-Null }
  }
}

function Start-Stack {
  foreach ($k in "k8s/local", "k8s/local/detectors", "k8s/local/analysis", "k8s/local/expose") {
    Run kubectl @("apply", "-k", $k)
  }
  Set-AuthMode
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

# Vite reads VITE_COGNITO_* when it starts: from this environment with -Cognito, or from an app's .env.local
function Set-AppLogins {
  if ($Cognito) {
    $env:VITE_COGNITO_REGION = $CognitoRegion
    $env:VITE_COGNITO_CLIENT_ID = $CognitoIds["UserPoolClientId"]
    return
  }
  foreach ($a in "caregiver", "traveller") {
    if ((Test-Path "apps/$a/.env.local") -and (Select-String -Path "apps/$a/.env.local" -Pattern '^VITE_COGNITO_CLIENT_ID=.+' -Quiet)) {
      Write-Host "warning: apps/$a/.env.local turns its login pages on, but the services aren't checking Cognito tokens. Delete it, or run with -Cognito" -ForegroundColor Yellow
    }
  }
}

function Start-App {
  if (Test-Url $AppUrl) {
    Write-Host "caregiver app already running (restart it if it should switch logins on or off)"
    return $null
  }
  if (-not (Test-Path apps/caregiver/node_modules)) {
    Push-Location apps/caregiver
    npm ci --no-audit --no-fund | Out-Host   # to the screen, so the function returns only the process
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

# the traveller app, where the caregiver app's trip links lead
function Start-TravellerApp {
  if (Test-Url $TravellerUrl) {
    Write-Host "traveller app already running (restart it if it should switch logins on or off)"
    return $null
  }
  if (-not (Test-Path apps/traveller/node_modules)) {
    Push-Location apps/traveller
    npm ci --no-audit --no-fund | Out-Host   # to the screen, so the function returns only the process
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { Fail "npm ci failed" }
  }
  $traveller = Start-Process -FilePath node -ArgumentList "node_modules/vite/bin/vite.js", "--strictPort" `
    -WorkingDirectory apps/traveller -PassThru -NoNewWindow -RedirectStandardOutput $TravellerLog -RedirectStandardError "$TravellerLog.err"
  Wait-Url $TravellerUrl "the traveller app (log in $TravellerLog)"
  return $traveller
}

$CognitoIds = if ($Cognito) { Get-CognitoIds } else { @{} }
Initialize-Cluster
Run kubectl @("config", "use-context", "kind-$Cluster")
if ($Setup) { Build-Images }
Initialize-Secret
Start-Stack
# 127.0.0.1, not localhost: Windows PowerShell tries ::1 first, kind only listens on IPv4, and every check
# would time out before falling back
foreach ($port in $Health.Keys) { Wait-Url "http://127.0.0.1:$port/health" $Health[$port] }
Set-AppLogins
$app = Start-App
$traveller = Start-TravellerApp

try {
  if ($Scenario) {
    & $Python -m tools.simulator live --scenario $Scenario --caregiver $Caregiver --interval $Interval
  }
  Write-Host ""
  if ($Cognito) {
    Write-Host "logins on: user pool $($CognitoIds['UserPoolId'])"
    Write-Host "caregiver app: $AppUrl  create an account or sign in"
    Write-Host "traveller app: $TravellerUrl  set up with a traveller's ID and setup code (repo README, Logins (Cognito))"
  } else {
    Write-Host "caregiver app: $AppUrl  act as $Caregiver"
    Write-Host "traveller app: $TravellerUrl  open a trip's link from the caregiver app's Trips tab"
  }
  Write-Host "aggregator dashboard: http://localhost:8002/dashboard"
  if (-not $Cognito) { Write-Host "another trip: $Python -m tools.simulator live --scenario off_route --caregiver $Caregiver" }
  # keep running while either app started here runs, so stopping the script stops both
  # only real processes: anything else a function printed would give Wait-Process a null id. @() + @() flattens,
  # so a process inside such an array is still found
  $started = @(@($app) + @($traveller) | Where-Object { $_ -is [System.Diagnostics.Process] })
  if ($started) {
    Write-Host "press Ctrl+C to stop the apps, the cluster keeps running"
    Wait-Process -Id ($started | ForEach-Object { $_.Id })
  }
} finally {
  foreach ($proc in @(@($app) + @($traveller) | Where-Object { $_ -is [System.Diagnostics.Process] })) {
    if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
  }
}
