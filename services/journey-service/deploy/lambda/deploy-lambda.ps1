<#
.SYNOPSIS
  Deploys the disruption checker Lambda (builds the package, uploads it, creates or updates our own CloudFormation
  stack) next to the platform's stack, using that stack's outputs.

.DESCRIPTION
  Run the platform's deploy.ps1 first. This script then:
    stack outputs -> build the zip (build.ps1) -> upload it to the shared bucket under lambda/ -> deploy
    deploy\lambda\disruption-lambda.yaml as stack careroute-journey-lambda -> invoke the function once.
  The LTA key is read from .env and passed to CloudFormation as a NoEcho parameter (it will show briefly in your
  own process list while the command runs, and it ends up in the function's environment variables).

  Safe to re-run: the zip's key contains its content hash, so only a changed package is redeployed.
  NOT TESTED AGAINST REAL AWS.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File services\journey-service\deploy\lambda\deploy-lambda.ps1
#>
param(
  [string]$PlatformStack = 'careroute-stack-1',
  [string]$LambdaStack = 'careroute-journey-lambda',
  [string]$Region = 'ap-southeast-1',
  [string]$ResourcePrefix,
  [string]$Bucket,
  [string]$EnvFile,
  [ValidateSet('ENABLED', 'DISABLED')][string]$ScheduleState = 'ENABLED'
)

$ErrorActionPreference = 'Stop'
$ServiceDir = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$RepoRoot = (Resolve-Path (Join-Path $ServiceDir '..\..')).Path
$Template = Join-Path $PSScriptRoot 'disruption-lambda.yaml'
$Zip = Join-Path $PSScriptRoot 'dist\disruption-lambda.zip'
. (Join-Path $PSScriptRoot '..esource-names.ps1')
if (-not $EnvFile) { $EnvFile = Join-Path $RepoRoot '.env' }

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Info([string]$Message) { Write-Host "    $Message" }
function Invoke-Checked([scriptblock]$Command) {
  & $Command
  if ($LASTEXITCODE -ne 0) { throw "Command failed (exit code $LASTEXITCODE): $Command" }
}
function Invoke-Quiet([scriptblock]$Command) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'SilentlyContinue'
  try { & $Command 2>$null } finally { $ErrorActionPreference = $prev }
}

try {
  Write-Step 'Checking prerequisites'
  foreach ($cmd in 'aws', 'docker') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "'$cmd' is not installed or not on PATH" }
  }
  $AccountId = Invoke-Quiet { aws sts get-caller-identity --query Account --output text }
  if ($LASTEXITCODE -ne 0) { throw "AWS credentials are not configured (run 'aws configure')" }
  $Status = Invoke-Quiet { aws cloudformation describe-stacks --stack-name $PlatformStack --region $Region --query 'Stacks[0].StackStatus' --output text }
  if ($LASTEXITCODE -ne 0 -or $Status -notmatch '_COMPLETE$') {
    throw "Stack $PlatformStack is not deployed (status: $Status). Run the platform's deploy script first."
  }

  # The LTA key: read from .env, never printed.
  $LtaKey = $null
  if (Test-Path $EnvFile) {
    foreach ($line in [IO.File]::ReadAllLines($EnvFile)) {
      if ($line -match '^\s*LTA_API_KEY\s*=\s*(.*?)\s*$') { $LtaKey = $Matches[1].Trim('"').Trim("'") }
    }
  }
  if (-not $LtaKey) { throw "LTA_API_KEY not found in $EnvFile (the checker cannot read train alerts without it)" }

  Write-Step 'Reading the platform stack outputs'
  $OutputsJson = Invoke-Checked { aws cloudformation describe-stacks --stack-name $PlatformStack --region $Region --query 'Stacks[0].Outputs' --output json }
  $Outputs = @{}
  foreach ($o in ($OutputsJson | Out-String | ConvertFrom-Json)) { $Outputs[$o.OutputKey] = $o.OutputValue }
  foreach ($name in 'PrivateSubnetIds', 'ClusterSecurityGroupId', 'RedisEndpoint', 'DisruptionsQueueArn', 'DisruptionsQueueUrl') {
    if (-not $Outputs[$name]) { throw "The platform stack has no output named $name" }
  }
  $QueueName = $Outputs['DisruptionsQueueUrl'].Split('/')[-1]
  $ResourcePrefix = Resolve-ResourcePrefix $PlatformStack $Region $ResourcePrefix
  $Bucket = Get-SharedBucketName $ResourcePrefix $AccountId $Bucket
  Write-Info "Subnets $($Outputs['PrivateSubnetIds']) | security group $($Outputs['ClusterSecurityGroupId'])"
  Write-Info "Redis $($Outputs['RedisEndpoint']) | queue $QueueName | code bucket $Bucket"
  Invoke-Quiet { aws s3api head-bucket --bucket $Bucket } | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Bucket $Bucket does not exist. The platform's deploy script creates it (named <prefix>-files-<account id>); run that first, or pass -Bucket." }

  Write-Step 'Building the package'
  Invoke-Checked { & (Join-Path $PSScriptRoot 'build.ps1') -Verify }

  Write-Step 'Uploading the package'
  $Hash = (Get-FileHash -Algorithm SHA256 $Zip).Hash.Substring(0, 12).ToLower()
  $CodeKey = "lambda/disruption-checker-$Hash.zip"
  Invoke-Checked { aws s3 cp $Zip "s3://$Bucket/$CodeKey" --region $Region --only-show-errors }
  Write-Info "s3://$Bucket/$CodeKey"

  Write-Step "Deploying stack $LambdaStack"
  Invoke-Checked {
    aws cloudformation deploy `
      --stack-name $LambdaStack `
      --template-file $Template `
      --capabilities CAPABILITY_IAM `
      --region $Region `
      --no-fail-on-empty-changeset `
      --parameter-overrides `
        "CodeBucket=$Bucket" "CodeKey=$CodeKey" `
        "PrivateSubnetIds=$($Outputs['PrivateSubnetIds'])" "SecurityGroupId=$($Outputs['ClusterSecurityGroupId'])" `
        "RedisEndpoint=$($Outputs['RedisEndpoint'])" "DisruptionsQueueArn=$($Outputs['DisruptionsQueueArn'])" `
        "DisruptionsQueueName=$QueueName" "LtaApiKey=$LtaKey" "ScheduleState=$ScheduleState"
  }
  $Function = Invoke-Checked { aws cloudformation describe-stacks --stack-name $LambdaStack --region $Region --query "Stacks[0].Outputs[?OutputKey=='FunctionName'].OutputValue" --output text }

  Write-Step "Invoking $Function once"
  $Result = Join-Path ([IO.Path]::GetTempPath()) "journey-lambda-result-$([guid]::NewGuid()).json"
  try {
    Invoke-Checked { aws lambda invoke --function-name $Function --region $Region --cli-binary-format raw-in-base64-out --payload '{}' $Result }
    Write-Info ("Result: " + [IO.File]::ReadAllText($Result))
  } finally {
    Remove-Item $Result -ErrorAction SilentlyContinue
  }

  Write-Host ''
  Write-Host "Disruption checker deployed; EventBridge runs it every minute ($ScheduleState)." -ForegroundColor Green
  Write-Host "Logs: aws logs tail /aws/lambda/$Function --follow --region $Region"
  Write-Host 'A result like {"activeTrips": 0, ...} on a normal day is correct: the checker does nothing until LTA reports a disruption.'
}
catch {
  Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
