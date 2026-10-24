<#
.SYNOPSIS
  Deploys the full CareRoute stack end to end (README "Deploy the infrastructure"
  and "Deploy the application" sections):
    CloudFormation stack -> kubeconfig -> AWS Load Balancer Controller ->
    image build/push -> S3 bucket -> rendered k8s manifests -> health check.

.DESCRIPTION
  Safe to re-run. The stack is created or updated from the current
  cloudformation\*.yaml, so edits to the template are picked up on the next run.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\deploy.ps1 -BudgetEmail you@example.com
.EXAMPLE
  .\scripts\windows\deploy.ps1 -ImageTag v2
#>
param(
  [string]$BudgetEmail = $env:BUDGET_EMAIL,
  [string]$ImageTag = 'v1',
  [string]$StackName = 'careroute-stack-1',
  [string]$Region = 'ap-southeast-1',
  [string]$ClusterName = 'careroute-cluster-1',
  [ValidateSet('fargate', 'nodegroup')][string]$ComputeMode = 'fargate',
  [int]$AppPort = 8000,
  [string]$AppNamespace = 'careroute'
)

$ErrorActionPreference = 'Stop'
$LbcPolicyName = 'AWSLoadBalancerControllerIAMPolicy'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Template = Join-Path $RepoRoot 'cloudformation\careroute-vpc-eks-standard.yaml'
$WorkDir = Join-Path ([IO.Path]::GetTempPath()) ("careroute-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $WorkDir | Out-Null
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Info([string]$Message) { Write-Host "    $Message" }

# Splits whitespace-separated CLI output (e.g. --output text lists) into words,
# dropping the empty entry that -split returns for empty output.
function Split-Words($Text) { @(-split "$Text" | Where-Object { $_ }) }
# Runs a native command and throws if it exits non-zero.
function Invoke-Checked([scriptblock]$Command) {
  & $Command
  if ($LASTEXITCODE -ne 0) { throw "Command failed (exit code $LASTEXITCODE): $Command" }
}

# Runs a native command whose failure is expected/acceptable. Returns its stdout;
# check $LASTEXITCODE afterwards. Windows PowerShell 5.1 turns redirected stderr
# into terminating errors under 'Stop', hence the temporary preference change.
function Invoke-Quiet([scriptblock]$Command) {
  $prev = $ErrorActionPreference
  $ErrorActionPreference = 'SilentlyContinue'
  try { & $Command 2>$null } finally { $ErrorActionPreference = $prev }
}

try {
  # ---------------------------------------------------------------------------
  Write-Step 'Checking prerequisites'
  foreach ($cmd in 'aws', 'kubectl', 'helm', 'eksctl', 'docker') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "'$cmd' is not installed or not on PATH" }
  }
  Invoke-Quiet { docker info } | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Docker is not running. Start Docker Desktop and retry.' }
  if (-not (Test-Path $Template)) { throw "Template not found: $Template" }
  $AccountId = Invoke-Quiet { aws sts get-caller-identity --query Account --output text }
  if ($LASTEXITCODE -ne 0) { throw "AWS credentials are not configured (run 'aws configure')" }
  Write-Info "Account $AccountId, region $Region, stack $StackName"

  # ---------------------------------------------------------------------------
  Write-Step 'Validating CloudFormation template'
  Invoke-Checked { aws cloudformation validate-template --template-body "file://$Template" --region $Region } | Out-Null

  # A stack stuck in ROLLBACK_COMPLETE cannot be updated, only deleted.
  $Status = Invoke-Quiet { aws cloudformation describe-stacks --stack-name $StackName --region $Region --query 'Stacks[0].StackStatus' --output text }
  if ($LASTEXITCODE -ne 0 -or -not $Status) { $Status = 'NONE' }
  if ($Status -eq 'ROLLBACK_COMPLETE') {
    Write-Step 'Stack is in ROLLBACK_COMPLETE from a failed create; deleting it first'
    Invoke-Checked { aws cloudformation delete-stack --stack-name $StackName --region $Region }
    Invoke-Checked { aws cloudformation wait stack-delete-complete --stack-name $StackName --region $Region }
  }

  # ---------------------------------------------------------------------------
  Write-Step 'Deploying CloudFormation stack (15-25 minutes on first create)'
  $Params = @("ClusterName=$ClusterName", "ComputeMode=$ComputeMode", "AppPort=$AppPort", "AppNamespace=$AppNamespace")
  if ($BudgetEmail) {
    $Params += "BudgetAlertEmail=$BudgetEmail"
  } elseif ($Status -eq 'NONE' -or $Status -eq 'ROLLBACK_COMPLETE') {
    Write-Info 'No -BudgetEmail given: the budget alert and DLQ alarms will not be created.'
  }
  # Unlisted parameters keep their previous values on update.
  Invoke-Checked {
    aws cloudformation deploy `
      --stack-name $StackName `
      --template-file $Template `
      --parameter-overrides $Params `
      --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM `
      --region $Region `
      --no-fail-on-empty-changeset
  }

  $OutputsJson = Invoke-Checked { aws cloudformation describe-stacks --stack-name $StackName --region $Region --query 'Stacks[0].Outputs' --output json }
  $Outputs = @{}
  foreach ($o in ($OutputsJson | Out-String | ConvertFrom-Json)) { $Outputs[$o.OutputKey] = $o.OutputValue }
  $EcrUri = $Outputs['EcrRepositoryUri']
  $ApiRoleArn = $Outputs['ApiServiceRoleArn']
  $RedisEndpoint = $Outputs['RedisEndpoint']
  $AlbSgId = $Outputs['AlbSecurityGroupId']
  $VpcId = $Outputs['VpcId']
  $ClusterName = $Outputs['EksClusterName']
  Write-Info "Cluster:  $ClusterName"
  Write-Info "VPC:      $VpcId"
  Write-Info "ECR:      $EcrUri"
  Write-Info "Redis:    $RedisEndpoint"
  Write-Info "ALB SG:   $AlbSgId"
  Write-Info "API role: $ApiRoleArn"

  # ---------------------------------------------------------------------------
  Write-Step 'Connecting kubectl to the cluster'
  Invoke-Checked { aws eks update-kubeconfig --region $Region --name $ClusterName }

  # ---------------------------------------------------------------------------
  Write-Step 'Installing the AWS Load Balancer Controller'
  Invoke-Quiet { helm repo add eks https://aws.github.io/eks-charts } | Out-Null
  Invoke-Checked { helm repo update eks }
  $ChartInfo = Invoke-Checked { helm show chart eks/aws-load-balancer-controller } | Out-String
  $ChartVersion = [regex]::Match($ChartInfo, '(?m)^version:\s*(\S+)').Groups[1].Value
  $AppVersion = [regex]::Match($ChartInfo, '(?m)^appVersion:\s*(\S+)').Groups[1].Value
  Write-Info "Chart $ChartVersion (controller $AppVersion)"

  $PolicyFile = Join-Path $WorkDir 'iam_policy.json'
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -UseBasicParsing -OutFile $PolicyFile `
    -Uri "https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/$AppVersion/docs/install/iam_policy.json"

  $PolicyArn = "arn:aws:iam::${AccountId}:policy/$LbcPolicyName"
  Invoke-Quiet { aws iam get-policy --policy-arn $PolicyArn } | Out-Null
  if ($LASTEXITCODE -eq 0) {
    # Keep the policy in step with the controller version. IAM allows 5 versions,
    # so drop the old non-default ones before adding the new default.
    $OldVersions = Invoke-Checked { aws iam list-policy-versions --policy-arn $PolicyArn --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text }
    foreach ($v in (Split-Words $OldVersions)) {
      Invoke-Checked { aws iam delete-policy-version --policy-arn $PolicyArn --version-id $v }
    }
    Invoke-Checked { aws iam create-policy-version --policy-arn $PolicyArn --policy-document "file://$PolicyFile" --set-as-default } | Out-Null
    Write-Info "Updated IAM policy $LbcPolicyName"
  } else {
    Invoke-Checked { aws iam create-policy --policy-name $LbcPolicyName --policy-document "file://$PolicyFile" } | Out-Null
    Write-Info "Created IAM policy $LbcPolicyName"
  }

  Invoke-Checked {
    eksctl create iamserviceaccount `
      --cluster $ClusterName `
      --namespace kube-system `
      --name aws-load-balancer-controller `
      --attach-policy-arn $PolicyArn `
      --override-existing-serviceaccounts `
      --region $Region `
      --approve
  }

  Invoke-Checked {
    helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller `
      -n kube-system `
      --set clusterName=$ClusterName `
      --set serviceAccount.create=false `
      --set serviceAccount.name=aws-load-balancer-controller `
      --set region=$Region `
      --set vpcId=$VpcId `
      --version $ChartVersion
  }

  Write-Info 'Waiting for the controller to become ready (Fargate pods take 1-2 minutes)'
  Invoke-Checked { kubectl rollout status deployment/aws-load-balancer-controller -n kube-system --timeout=10m }

  # ---------------------------------------------------------------------------
  Write-Step "Building and pushing image ${EcrUri}:$ImageTag"
  $EcrRegistry = $EcrUri.Split('/')[0]
  Invoke-Checked { aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $EcrRegistry }
  Invoke-Checked { docker build --platform linux/amd64 -t "${EcrUri}:$ImageTag" $RepoRoot }
  Invoke-Checked { docker push "${EcrUri}:$ImageTag" }

  # ---------------------------------------------------------------------------
  $Bucket = "careroute-files-$AccountId"
  Write-Step "Ensuring S3 bucket $Bucket exists"
  Invoke-Quiet { aws s3api head-bucket --bucket $Bucket } | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Write-Info 'Bucket already exists'
  } else {
    Invoke-Checked { aws s3 mb "s3://$Bucket" --region $Region }
  }

  # ---------------------------------------------------------------------------
  # The committed manifests keep their placeholders; a filled-in copy is applied
  # from a temp directory so account-specific values never end up in Git.
  Write-Step 'Rendering k8s/eks manifests with stack outputs'
  $Manifests = Join-Path $WorkDir 'k8s-eks'
  Copy-Item -Recurse (Join-Path $RepoRoot 'k8s\eks') $Manifests

  function Update-Manifest([string]$File, [string]$Pattern, [string]$Replacement) {
    $path = Join-Path $Manifests $File
    $text = [IO.File]::ReadAllText($path) -replace $Pattern, $Replacement
    [IO.File]::WriteAllText($path, $text, $Utf8NoBom)
  }
  Update-Manifest 'kustomization.yaml'  '(?m)^(\s*newName:)[^\r\n]*' ('${1} ' + $EcrUri)
  Update-Manifest 'kustomization.yaml'  '(?m)^(\s*newTag:)[^\r\n]*' ('${1} ' + $ImageTag)
  Update-Manifest 'serviceaccount.yaml' '(eks\.amazonaws\.com/role-arn:)[^\r\n]*' ('${1} ' + $ApiRoleArn)
  Update-Manifest 'fastapi.yaml'        'redis://[^:/"]*:6379' ("redis://${RedisEndpoint}:6379")
  Update-Manifest 'ingress.yaml'        '(alb\.ingress\.kubernetes\.io/security-groups:)[^\r\n]*' ('${1} ' + $AlbSgId)
  Update-Manifest 'aws-logging.yaml'    '(log_group_name\s*)[^\r\n]*' ('${1}' + "/eks/$ClusterName/$AppNamespace")

  # ---------------------------------------------------------------------------
  Write-Step 'Applying manifests'
  Invoke-Quiet { kubectl get deployment cloud-fastapi -n $AppNamespace } | Out-Null
  $DeploymentExisted = ($LASTEXITCODE -eq 0)
  Invoke-Checked { kubectl apply -k $Manifests }
  # Re-pushing the same tag does not change the spec, so force new pods to pull it.
  if ($DeploymentExisted) {
    Invoke-Checked { kubectl rollout restart deployment/cloud-fastapi -n $AppNamespace }
  }
  Invoke-Checked { kubectl rollout status deployment/cloud-fastapi -n $AppNamespace --timeout=10m }

  # ---------------------------------------------------------------------------
  Write-Step 'Waiting for the ALB address'
  $AlbHost = $null
  for ($i = 0; $i -lt 60 -and -not $AlbHost; $i++) {
    $AlbHost = Invoke-Quiet { kubectl get ingress cloud-fastapi -n $AppNamespace -o 'jsonpath={.status.loadBalancer.ingress[0].hostname}' }
    if (-not $AlbHost) { Start-Sleep -Seconds 10 }
  }
  if (-not $AlbHost) {
    throw "Ingress has no address after 10 minutes. Check: kubectl describe ingress cloud-fastapi -n $AppNamespace"
  }
  Write-Info "ALB: $AlbHost"

  Write-Step "Waiting for http://$AlbHost/health (DNS and target registration take a few minutes)"
  $Healthy = $false
  for ($i = 0; $i -lt 60 -and -not $Healthy; $i++) {
    try {
      $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "http://$AlbHost/health"
      Write-Info "Response: $($resp.Content)"
      $Healthy = $true
    } catch {
      Start-Sleep -Seconds 10
    }
  }

  Write-Host ''
  if ($Healthy) {
    Write-Host "Deployment complete. API: http://$AlbHost" -ForegroundColor Green
  } else {
    Write-Host 'Deployed, but /health did not respond within 10 minutes.' -ForegroundColor Yellow
    Write-Host "Check: kubectl get pods -n $AppNamespace ; kubectl describe ingress cloud-fastapi -n $AppNamespace"
  }
  if ($BudgetEmail) { Write-Host "Remember to confirm the AWS budget/SNS subscription emails sent to $BudgetEmail." }
}
catch {
  Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
finally {
  Remove-Item -Recurse -Force $WorkDir -ErrorAction SilentlyContinue
}
