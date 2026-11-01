<#
.SYNOPSIS
  Deploys the Journey Service onto the platform's EKS cluster (the stack the platform owner's deploy.ps1 creates).

.DESCRIPTION
  Run the platform's deploy.ps1 first; this script only adds our service to what it built:
    stack outputs -> kubeconfig -> pack bucket CORS -> Secret for the LTA/OneMap keys -> build and push our image
    to the stack's ECR repository (tag journey-<ImageTag>) -> render and apply deploy\eks\journey-service.yaml
    -> wait for the rollout -> /ready check through a port-forward.

  Names: tables are <prefix>-routes and <prefix>-trips, the pack bucket is <prefix>-files-<account id>. The prefix is
  read from the platform stack's ResourcePrefix parameter (the team decided on 'careroute'; this warns if the stack
  was built with anything else). -ResourcePrefix and -Bucket override.

  Safe to re-run. It never touches the platform's own manifests, and the API keys are read from .env and go
  straight into a Kubernetes Secret (a temporary file that is deleted at the end), never into Git or the image.

  NOT TESTED AGAINST REAL AWS. -RenderOnly checks the manifest without any AWS call.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File services\journey-service\deploy\windows\deploy-eks.ps1
.EXAMPLE
  .\deploy-eks.ps1 -RenderOnly -Image 123.dkr.ecr.ap-southeast-1.amazonaws.com/x:journey-v1 -RedisEndpoint r.cache.amazonaws.com -Bucket careroute-files-123 -OutFile out.yaml
#>
param(
  [string]$StackName = 'careroute-stack-1',
  [string]$Region = 'ap-southeast-1',
  [string]$ResourcePrefix,
  [string]$ImageTag = 'v1',
  [string]$AppNamespace = 'careroute',
  [string]$EnvFile,
  [string]$CorsOrigin = '*',
  [switch]$RenderOnly,
  [string]$Image,
  [string]$RedisEndpoint,
  [string]$Bucket,
  [string]$OutFile
)

$ErrorActionPreference = 'Stop'
$ServiceDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$RepoRoot = (Resolve-Path (Join-Path $ServiceDir '..\..')).Path
$Manifest = Join-Path $PSScriptRoot '..\eks\journey-service.yaml'
if (-not $EnvFile) { $EnvFile = Join-Path $RepoRoot '.env' }
$WorkDir = Join-Path ([IO.Path]::GetTempPath()) ("journey-deploy-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $WorkDir | Out-Null
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
. (Join-Path $PSScriptRoot 'resource-names.ps1')

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Info([string]$Message) { Write-Host "    $Message" }
function Invoke-Checked([scriptblock]$Command) {
  & $Command
  if ($LASTEXITCODE -ne 0) { throw "Command failed (exit code $LASTEXITCODE): $Command" }
}
# Runs a command whose failure is acceptable; check $LASTEXITCODE afterwards (Windows PowerShell 5.1 turns
# redirected stderr into terminating errors under 'Stop', hence the temporary preference change).
function Invoke-Quiet([scriptblock]$Command) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'SilentlyContinue'
  try { & $Command 2>$null } finally { $ErrorActionPreference = $prev }
}
# Logs Docker in to ECR. Not `aws ecr get-login-password | docker login --password-stdin`: Windows PowerShell 5.1
# re-encodes what it pipes into a native command, and ECR rejects the token (400 Bad Request). cmd's `<` hands
# docker the file's bytes unchanged. The file is deleted straight after.
function Connect-Ecr([string]$Registry) {
  $password = aws ecr get-login-password --region $Region
  if ($LASTEXITCODE -ne 0) { throw "Command failed (exit code $LASTEXITCODE): aws ecr get-login-password" }
  $file = [IO.Path]::GetTempFileName()
  try {
    [IO.File]::WriteAllText($file, $password)
    Invoke-Checked { cmd /c "docker login --username AWS --password-stdin $Registry < `"$file`"" }
  } finally { Remove-Item $file -Force -ErrorAction SilentlyContinue }
}

# The committed manifest keeps its placeholders; this fills them in and refuses to continue if any is left.
function Get-RenderedManifest([string]$Img, [string]$Redis, [string]$Bkt, [string]$Prefix) {
  $text = [IO.File]::ReadAllText($Manifest)
  $text = $text.Replace('<JOURNEY_IMAGE>', $Img).Replace('<REDIS_ENDPOINT>', $Redis).Replace('<PACKS_BUCKET>', $Bkt).Replace('<RESOURCE_PREFIX>', $Prefix)
  $left = [regex]::Match($text, '<[A-Z_]+>')
  if ($left.Success) { throw "Manifest still has an unfilled placeholder: $($left.Value)" }
  return $text
}

# Reads KEY=VALUE pairs from a .env file without printing them.
function Get-EnvValues([string]$Path, [string[]]$Names) {
  $found = @{}
  if (-not (Test-Path $Path)) { return $found }
  foreach ($line in [IO.File]::ReadAllLines($Path)) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$' -and $Names -contains $Matches[1]) {
      $value = $Matches[2].Trim('"').Trim("'")
      if ($value) { $found[$Matches[1]] = $value }
    }
  }
  return $found
}

try {
  if ($RenderOnly) {
    if (-not ($Image -and $RedisEndpoint -and $Bucket -and $OutFile)) { throw '-RenderOnly needs -Image, -RedisEndpoint, -Bucket and -OutFile' }
    $Prefix = if ($ResourcePrefix) { $ResourcePrefix } else { $TeamPrefix }
    [IO.File]::WriteAllText($OutFile, (Get-RenderedManifest $Image $RedisEndpoint $Bucket $Prefix), $Utf8NoBom)
    Write-Info "Rendered manifest written to $OutFile"
    return
  }

  # ---------------------------------------------------------------------------
  Write-Step 'Checking prerequisites'
  foreach ($cmd in 'aws', 'kubectl', 'docker') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "'$cmd' is not installed or not on PATH" }
  }
  Invoke-Quiet { docker info } | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Docker is not running. Start Docker Desktop and retry.' }
  $AccountId = Invoke-Quiet { aws sts get-caller-identity --query Account --output text }
  if ($LASTEXITCODE -ne 0) { throw "AWS credentials are not configured (run 'aws configure')" }
  $Status = Invoke-Quiet { aws cloudformation describe-stacks --stack-name $StackName --region $Region --query 'Stacks[0].StackStatus' --output text }
  if ($LASTEXITCODE -ne 0 -or $Status -notmatch '_COMPLETE$') {
    throw "Stack $StackName is not deployed (status: $Status). Run the platform's deploy script first."
  }
  Write-Info "Account $AccountId, region $Region, stack $StackName ($Status)"

  # ---------------------------------------------------------------------------
  Write-Step 'Reading the stack outputs'
  $OutputsJson = Invoke-Checked { aws cloudformation describe-stacks --stack-name $StackName --region $Region --query 'Stacks[0].Outputs' --output json }
  $Outputs = @{}
  foreach ($o in ($OutputsJson | Out-String | ConvertFrom-Json)) { $Outputs[$o.OutputKey] = $o.OutputValue }
  $EcrUri = $Outputs['EcrRepositoryUri']
  $RedisEndpoint = $Outputs['RedisEndpoint']
  $ClusterName = $Outputs['EksClusterName']
  foreach ($name in 'EcrRepositoryUri', 'RedisEndpoint', 'EksClusterName') {
    if (-not $Outputs[$name]) { throw "The stack has no output named $name" }
  }
  $ResourcePrefix = Resolve-ResourcePrefix $StackName $Region $ResourcePrefix
  $Bucket = Get-SharedBucketName $ResourcePrefix $AccountId $Bucket
  $Image = "${EcrUri}:journey-$ImageTag"
  Write-Info "Cluster $ClusterName | Redis $RedisEndpoint | image $Image | pack bucket $Bucket"

  # The tables belong to the platform stack. Checked here with YOUR credentials (the pods' role cannot DescribeTable),
  # so a wrong prefix is caught now instead of as a failing /ready later.
  foreach ($table in "$ResourcePrefix-routes", "$ResourcePrefix-trips") {
    Invoke-Quiet { aws dynamodb describe-table --table-name $table --region $Region --query 'Table.TableStatus' --output text } | Out-Null
    if ($LASTEXITCODE -ne 0) {
      throw "DynamoDB table $table does not exist. Stack $StackName was built with prefix '$ResourcePrefix'; if its tables use another prefix, pass -ResourcePrefix."
    }
    Write-Info "Table $table found"
  }

  Write-Step 'Connecting kubectl to the cluster'
  Invoke-Checked { aws eks update-kubeconfig --region $Region --name $ClusterName }

  # ---------------------------------------------------------------------------
  Write-Step "Pack bucket ${Bucket}: CORS for downloads from the traveller app"
  Invoke-Quiet { aws s3api head-bucket --bucket $Bucket } | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Bucket $Bucket does not exist. The platform's deploy script creates it (its name must be <prefix>-files-<account id>, with the stack's prefix, or the pods' role cannot reach it); run that first, or pass -Bucket." }
  # The pods' role cannot set this (it only has object read/write), so it is done here with your credentials.
  $CorsFile = Join-Path $WorkDir 'cors.json'
  $Cors = '{"CORSRules":[{"AllowedMethods":["GET"],"AllowedOrigins":["' + $CorsOrigin + '"],"AllowedHeaders":["*"],"MaxAgeSeconds":600}]}'
  [IO.File]::WriteAllText($CorsFile, $Cors, $Utf8NoBom)
  Invoke-Checked { aws s3api put-bucket-cors --bucket $Bucket --cors-configuration "file://$CorsFile" }
  Write-Info "Allowed origin: $CorsOrigin (tighten it to the traveller app's address with -CorsOrigin)"

  # ---------------------------------------------------------------------------
  Write-Step 'Creating the Secret with the LTA, OneMap and Google Maps keys'
  $Keys = Get-EnvValues $EnvFile @('LTA_API_KEY', 'ONEMAP_API_KEY', 'ONEMAP_API_EMAIL', 'ONEMAP_API_PASSWORD', 'GOOGLE_MAPS_API_KEY')
  foreach ($n in 'LTA_API_KEY', 'ONEMAP_API_EMAIL', 'ONEMAP_API_PASSWORD', 'ONEMAP_API_KEY', 'GOOGLE_MAPS_API_KEY') {
    if ($Keys.ContainsKey($n)) { Write-Info "$n found in $EnvFile" } else { Write-Info "NOTE: $n not found in $EnvFile" }
  }
  if (-not ($Keys.ContainsKey('ONEMAP_API_EMAIL') -and $Keys.ContainsKey('ONEMAP_API_PASSWORD')) -and -not $Keys.ContainsKey('ONEMAP_API_KEY')) {
    Write-Info 'WARNING: no OneMap account (ONEMAP_API_EMAIL + ONEMAP_API_PASSWORD) or token (ONEMAP_API_KEY): route planning will not work'
  }
  Invoke-Quiet { kubectl get namespace $AppNamespace } | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Namespace $AppNamespace does not exist in the cluster. The platform's deploy script creates it." }
  $SecretFile = Join-Path $WorkDir 'secret.env'
  [IO.File]::WriteAllLines($SecretFile, [string[]]($Keys.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }), $Utf8NoBom)
  $SecretYaml = Invoke-Checked { kubectl create secret generic journey-api-keys --from-env-file=$SecretFile -n $AppNamespace --dry-run=client -o yaml }
  $SecretYaml | kubectl apply -f -
  if ($LASTEXITCODE -ne 0) { throw 'Could not create the Secret' }

  # ---------------------------------------------------------------------------
  Write-Step "Building and pushing $Image"
  $EcrRegistry = $EcrUri.Split('/')[0]
  Connect-Ecr $EcrRegistry
  Invoke-Checked { docker build --platform linux/amd64 -t $Image $ServiceDir }
  Invoke-Checked { docker push $Image }

  # ---------------------------------------------------------------------------
  Write-Step 'Applying the manifest'
  $Rendered = Join-Path $WorkDir 'journey-service.yaml'
  [IO.File]::WriteAllText($Rendered, (Get-RenderedManifest $Image $RedisEndpoint $Bucket $ResourcePrefix), $Utf8NoBom)
  Invoke-Quiet { kubectl get deployment journey-service -n $AppNamespace } | Out-Null
  $Existed = ($LASTEXITCODE -eq 0)
  Invoke-Checked { kubectl apply -f $Rendered }
  # Re-pushing the same tag does not change the spec, so force new pods to pull it.
  if ($Existed) { Invoke-Checked { kubectl rollout restart deployment/journey-service -n $AppNamespace } }
  Invoke-Checked { kubectl rollout status deployment/journey-service -n $AppNamespace --timeout=10m }

  # ---------------------------------------------------------------------------
  Write-Step 'Checking /ready through a port-forward'
  $Forward = Start-Process kubectl -ArgumentList "port-forward svc/journey-service 18006:8000 -n $AppNamespace" -PassThru -WindowStyle Hidden
  try {
    $Ready = $false
    for ($i = 0; $i -lt 12 -and -not $Ready; $i++) {
      Start-Sleep -Seconds 5
      try {
        $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri 'http://localhost:18006/ready'
        Write-Info "GET /ready -> $($resp.StatusCode) $($resp.Content)"
        $Ready = $true
      } catch {
        Write-Info "not ready yet: $($_.Exception.Message)"
      }
    }
  } finally {
    Stop-Process -Id $Forward.Id -ErrorAction SilentlyContinue
  }

  Write-Host ''
  if ($Ready) {
    Write-Host 'Journey Service deployed and ready (inside the cluster; no Ingress).' -ForegroundColor Green
  } else {
    Write-Host '/ready did not answer 200. The 503 body names the failing store, e.g. "routes table: AccessDeniedException".' -ForegroundColor Yellow
    Write-Host "Check: kubectl logs deploy/journey-service -n $AppNamespace ; kubectl describe pod -l app=journey-service -n $AppNamespace"
  }
  Write-Host "Reach it with: kubectl port-forward svc/journey-service 8006:8000 -n $AppNamespace"
}
catch {
  Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
finally {
  Remove-Item -Recurse -Force $WorkDir -ErrorAction SilentlyContinue
}
