<#
.SYNOPSIS
  Removes what deploy-eks.ps1 added: our Deployment, Service and Secret, our image tags in ECR, and the packs
  stored in the shared S3 bucket. It does not touch the platform's own resources.

.DESCRIPTION
  Order when tearing everything down:  1. lambda\teardown-lambda.ps1   2. this script   3. the platform's teardown.ps1
  (The platform's teardown deletes the whole stack, the cluster and the bucket, and fails if our Lambda still holds
  network interfaces in its subnets.)

  NOT TESTED AGAINST REAL AWS.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File services\journey-service\deploy\teardown-eks.ps1
#>
param(
  [switch]$Yes,
  [string]$StackName = 'careroute-stack-1',
  [string]$Region = 'ap-southeast-1',
  [string]$AppNamespace = 'careroute',
  [string]$ResourcePrefix,
  [string]$Bucket
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'resource-names.ps1')
function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Info([string]$Message) { Write-Host "    $Message" }
function Invoke-Quiet([scriptblock]$Command) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'SilentlyContinue'
  try { & $Command 2>$null } finally { $ErrorActionPreference = $prev }
}

try {
  foreach ($cmd in 'aws', 'kubectl') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "'$cmd' is not installed or not on PATH" }
  }
  $AccountId = Invoke-Quiet { aws sts get-caller-identity --query Account --output text }
  if ($LASTEXITCODE -ne 0) { throw "AWS credentials are not configured (run 'aws configure')" }
  # The shared bucket is <prefix>-files-<account id>; the prefix is read from the stack (or -ResourcePrefix / -Bucket).
  $ResourcePrefix = Resolve-ResourcePrefix $StackName $Region $ResourcePrefix
  $Bucket = Get-SharedBucketName $ResourcePrefix $AccountId $Bucket

  if (-not $Yes) {
    Write-Host ''
    Write-Host "This deletes the journey-service Deployment, Service and Secret in namespace $AppNamespace,"
    Write-Host "our journey-* images in ECR, and everything under s3://$Bucket/packs/ (saved routes and trips in"
    Write-Host 'DynamoDB are NOT deleted here; they go with the platform stack).'
    $confirm = Read-Host 'Type journey-service to continue'
    if ($confirm -ne 'journey-service') { throw 'Aborted.' }
  }

  Write-Step 'Deleting our Kubernetes resources'
  $ClusterName = Invoke-Quiet { aws cloudformation describe-stacks --stack-name $StackName --region $Region --query "Stacks[0].Outputs[?OutputKey=='EksClusterName'].OutputValue" --output text }
  if ($LASTEXITCODE -eq 0 -and $ClusterName -and $ClusterName -ne 'None') {
    Invoke-Quiet { aws eks update-kubeconfig --region $Region --name $ClusterName } | Out-Null
    Invoke-Quiet { kubectl delete deployment,service journey-service -n $AppNamespace --ignore-not-found --timeout=5m } | Write-Host
    Invoke-Quiet { kubectl delete secret journey-api-keys -n $AppNamespace --ignore-not-found } | Write-Host
  } else {
    Write-Info "Stack $StackName not found; nothing to delete in the cluster"
  }

  Write-Step 'Deleting our images from ECR (tags starting with journey-)'
  $EcrUri = Invoke-Quiet { aws cloudformation describe-stacks --stack-name $StackName --region $Region --query "Stacks[0].Outputs[?OutputKey=='EcrRepositoryUri'].OutputValue" --output text }
  if ($LASTEXITCODE -eq 0 -and $EcrUri -and $EcrUri -ne 'None') {
    $Repo = $EcrUri.Split('/')[-1]
    $images = Invoke-Quiet { aws ecr list-images --repository-name $Repo --region $Region --output json } | Out-String | ConvertFrom-Json
    foreach ($img in @($images.imageIds | Where-Object { $_.imageTag -like 'journey-*' })) {
      Invoke-Quiet { aws ecr batch-delete-image --repository-name $Repo --region $Region --image-ids "imageTag=$($img.imageTag)" } | Out-Null
      Write-Info "Deleted $Repo`:$($img.imageTag)"
    }
  }

  Write-Step "Deleting packs from s3://$Bucket/packs/"
  Invoke-Quiet { aws s3api head-bucket --bucket $Bucket } | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Invoke-Quiet { aws s3 rm "s3://$Bucket/packs/" --recursive --region $Region } | Write-Host
  } else {
    Write-Info "Bucket $Bucket not found"
  }

  Write-Host ''
  Write-Host 'Journey Service removed.' -ForegroundColor Green
}
catch {
  Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
