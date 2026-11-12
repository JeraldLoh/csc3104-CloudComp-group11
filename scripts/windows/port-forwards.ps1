# port-forwards from the kind cluster for the apps on this laptop, kept running until Ctrl+C
#   .\scripts\windows\port-forwards.ps1
# Alert Service on 8005, Journey Service on 8006, Check-in Service on 8001: the ports the apps' dev servers use.
# A port-forward stops whenever its pods are replaced (a deploy, a rollout restart, Docker restarting), so each one
# is started again by itself. A port already in use (say, a port-forward started by hand) is left alone.
# Not needed with a cluster made by local-sim.ps1 -Setup: that one serves these ports itself.
param(
  [string]$Namespace = "csc3104"
)

$Forwards = [ordered]@{ "alert-service" = 8005; "journey-service" = 8006; "checkin-service" = 8001 }

function Test-Health([int]$Port) {
  try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$Port/health").StatusCode -eq 200 }
  catch { return $false }
}

kubectl get namespace $Namespace 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Host "Can't reach the cluster (namespace $Namespace). Is Docker Desktop running?" -ForegroundColor Red
  exit 1
}

$jobs = @()
foreach ($svc in $Forwards.Keys) {
  $port = $Forwards[$svc]
  if (Test-Health $port) {
    Write-Host "$svc on $port already answers, leaving it alone"
    continue
  }
  # Each forward runs in a background job that starts kubectl again whenever it stops.
  $jobs += Start-Job -Name $svc -ArgumentList $svc, $port, $Namespace -ScriptBlock {
    param($svc, $port, $ns)
    while ($true) {
      kubectl port-forward "svc/$svc" "${port}:8000" -n $ns 2>&1 | Out-Null
      Start-Sleep -Seconds 3
    }
  }
  Write-Host "$svc -> http://127.0.0.1:$port"
}
if (-not $jobs) {
  Write-Host "Nothing to start."
  exit 0
}

Write-Host "Ctrl+C stops them all. Status changes are shown below."
$last = @{}
try {
  while ($true) {
    foreach ($svc in $Forwards.Keys) {
      $up = Test-Health $Forwards[$svc]
      if ($last[$svc] -ne $up) {
        $time = Get-Date -Format "HH:mm:ss"
        if ($up) { Write-Host "$time  $svc is up" -ForegroundColor Green }
        else { Write-Host "$time  $svc is down, starting it again" -ForegroundColor Yellow }
        $last[$svc] = $up
      }
    }
    Start-Sleep -Seconds 5
  }
}
finally {
  # Stop the jobs, then any kubectl they had started (stopping a job doesn't end its child process).
  $jobs | Stop-Job -ErrorAction SilentlyContinue
  $jobs | Remove-Job -Force -ErrorAction SilentlyContinue
  $mine = ($Forwards.Keys | ForEach-Object { "svc/$_ $($Forwards[$_]):8000" }) -join "|"
  Get-CimInstance Win32_Process -Filter "Name='kubectl.exe'" |
    Where-Object { $_.CommandLine -match "port-forward ($mine) -n $Namespace" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Write-Host "Port-forwards stopped."
}
