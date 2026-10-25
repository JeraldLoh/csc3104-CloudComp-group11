<#
.SYNOPSIS
  Removes the disruption checker: our CloudFormation stack (function, role, schedule, log group) and the uploaded zips.

.DESCRIPTION
  Run this BEFORE the platform's teardown.ps1. A Lambda inside a VPC holds network interfaces in the platform's
  subnets and uses its security group; until they are released, deleting the platform stack can fail. AWS releases
  them some minutes after the function is deleted (it can take longer than this script waits), so if the platform
  teardown complains about a subnet or security group, wait and re-run it.

  NOT TESTED AGAINST REAL AWS.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File services\journey-service\deploy\windows\teardown-lambda.ps1
#>
param(
  [switch]$Yes,
  [string]$LambdaStack = 'careroute-journey-lambda',
  [string]$PlatformStack = 'careroute-stack-1',
  [string]$Region = 'ap-southeast-1',
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
  if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw "'aws' is not installed or not on PATH" }
  $AccountId = Invoke-Quiet { aws sts get-caller-identity --query Account --output text }
  if ($LASTEXITCODE -ne 0) { throw "AWS credentials are not configured (run 'aws configure')" }
  $ResourcePrefix = Resolve-ResourcePrefix $PlatformStack $Region $ResourcePrefix
  $Bucket = Get-SharedBucketName $ResourcePrefix $AccountId $Bucket

  if (-not $Yes) {
    $confirm = Read-Host "This deletes stack $LambdaStack and s3://$Bucket/lambda/. Type the stack name to continue"
    if ($confirm -ne $LambdaStack) { throw 'Aborted.' }
  }

  Write-Step "Deleting stack $LambdaStack"
  $Status = Invoke-Quiet { aws cloudformation describe-stacks --stack-name $LambdaStack --region $Region --query 'Stacks[0].StackStatus' --output text }
  if ($LASTEXITCODE -ne 0 -or -not $Status) {
    Write-Info 'Stack not found; skipping'
  } else {
    aws cloudformation delete-stack --stack-name $LambdaStack --region $Region
    if ($LASTEXITCODE -ne 0) { throw 'delete-stack failed' }
    aws cloudformation wait stack-delete-complete --stack-name $LambdaStack --region $Region
    if ($LASTEXITCODE -ne 0) { throw "The stack did not delete cleanly. Check: aws cloudformation describe-stack-events --stack-name $LambdaStack" }
    Write-Info 'Stack deleted'
  }

  Write-Step "Deleting the uploaded packages from s3://$Bucket/lambda/"
  Invoke-Quiet { aws s3api head-bucket --bucket $Bucket } | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Invoke-Quiet { aws s3 rm "s3://$Bucket/lambda/" --recursive --region $Region } | Write-Host
  } else {
    Write-Info "Bucket $Bucket not found"
  }

  Write-Host ''
  Write-Host 'Disruption checker removed. Network interfaces may take a few more minutes to be released.' -ForegroundColor Green
}
catch {
  Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
