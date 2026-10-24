<#
.SYNOPSIS
  Tears down everything deploy.ps1 created (README "Teardown" section), in the
  order that avoids orphaned resources:
    k8s resources (deletes the ALB) -> Load Balancer Controller + its IRSA role ->
    leftover controller-made LBs/target groups/security groups ->
    CloudFormation stack -> out-of-stack S3 bucket, IAM policy and log groups.

.DESCRIPTION
  Nothing about the stack's contents is hardcoded: S3 buckets and ECR repos are
  looked up from the live stack and emptied before deletion, so resources added
  to the CloudFormation template later are torn down too.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\teardown.ps1
.EXAMPLE
  .\scripts\windows\teardown.ps1 -Yes
#>
param(
  [switch]$Yes,
  [string]$StackName = 'careroute-stack-1',
  [string]$Region = 'ap-southeast-1',
  [string]$ClusterName = 'careroute-cluster-1',
  [string]$AppNamespace = 'careroute'
)

$ErrorActionPreference = 'Stop'
$LbcPolicyName = 'AWSLoadBalancerControllerIAMPolicy'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Info([string]$Message) { Write-Host "    $Message" }
function Write-Warn([string]$Message) { Write-Host "    WARNING: $Message" -ForegroundColor Yellow }

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

function Get-StackStatus([string]$Name) {
  $s = Invoke-Quiet { aws cloudformation describe-stacks --stack-name $Name --region $Region --query 'Stacks[0].StackStatus' --output text }
  if ($LASTEXITCODE -ne 0 -or -not $s) { return 'NONE' }
  return $s
}

function Get-StackResourceIds([string]$Type) {
  $ids = Invoke-Quiet { aws cloudformation list-stack-resources --stack-name $StackName --region $Region --query "StackResourceSummaries[?ResourceType=='$Type' && ResourceStatus!='DELETE_COMPLETE'].PhysicalResourceId" --output text }
  return Split-Words $ids
}

# Deletes load balancers, target groups and security groups the AWS Load
# Balancer Controller created for this cluster. These live outside the stack
# and block VPC deletion if the controller did not clean them up.
function Remove-ControllerLeftovers {
  $lbs = Split-Words "$(Invoke-Quiet { aws resourcegroupstaggingapi get-resources --region $Region --tag-filters "Key=elbv2.k8s.aws/cluster,Values=$ClusterName" --resource-type-filters elasticloadbalancing:loadbalancer --query 'ResourceTagMappingList[].ResourceARN' --output text })"
  foreach ($arn in $lbs) {
    Write-Info "Deleting leftover load balancer $arn"
    Invoke-Quiet { aws elbv2 delete-load-balancer --load-balancer-arn $arn --region $Region } | Out-Null
  }
  if ($lbs) { Invoke-Quiet { aws elbv2 wait load-balancers-deleted --load-balancer-arns $lbs --region $Region } | Out-Null }

  $tgs = Split-Words "$(Invoke-Quiet { aws resourcegroupstaggingapi get-resources --region $Region --tag-filters "Key=elbv2.k8s.aws/cluster,Values=$ClusterName" --resource-type-filters elasticloadbalancing:targetgroup --query 'ResourceTagMappingList[].ResourceARN' --output text })"
  foreach ($arn in $tgs) {
    Write-Info "Deleting leftover target group $arn"
    Invoke-Quiet { aws elbv2 delete-target-group --target-group-arn $arn --region $Region } | Out-Null
  }

  $sgs = Split-Words "$(Invoke-Quiet { aws ec2 describe-security-groups --region $Region --filters "Name=tag:elbv2.k8s.aws/cluster,Values=$ClusterName" --query 'SecurityGroups[].GroupId' --output text })"
  foreach ($sg in $sgs) {
    # Remove rules in other groups that reference this one, then the group itself.
    $rulesJson = Invoke-Quiet { aws ec2 describe-security-group-rules --region $Region --filters "Name=referenced-group-info.group-id,Values=$sg" --query 'SecurityGroupRules[?!IsEgress]' --output json }
    if ($rulesJson) {
      foreach ($rule in ($rulesJson | Out-String | ConvertFrom-Json)) {
        Invoke-Quiet { aws ec2 revoke-security-group-ingress --group-id $rule.GroupId --security-group-rule-ids $rule.SecurityGroupRuleId --region $Region } | Out-Null
      }
    }
    Write-Info "Deleting leftover security group $sg"
    for ($i = 0; $i -lt 6; $i++) {
      Invoke-Quiet { aws ec2 delete-security-group --group-id $sg --region $Region } | Out-Null
      if ($LASTEXITCODE -eq 0) { break }
      Start-Sleep -Seconds 20   # ENIs from a just-deleted ALB take a moment to detach
    }
  }
}

function Clear-StackStorage {
  foreach ($b in (Get-StackResourceIds 'AWS::S3::Bucket')) {
    Write-Info "Emptying stack bucket s3://$b"
    Invoke-Quiet { aws s3 rm "s3://$b" --recursive --region $Region } | Out-Null
  }
  foreach ($r in (Get-StackResourceIds 'AWS::ECR::Repository')) {
    $digests = Split-Words "$(Invoke-Quiet { aws ecr list-images --repository-name $r --region $Region --query 'imageIds[].imageDigest' --output text })"
    if ($digests) { Write-Info "Deleting images in ECR repository $r" }
    foreach ($d in $digests) {
      Invoke-Quiet { aws ecr batch-delete-image --repository-name $r --region $Region --image-ids "imageDigest=$d" } | Out-Null
    }
  }
}

function Remove-Stack {
  Invoke-Checked { aws cloudformation delete-stack --stack-name $StackName --region $Region }
  Invoke-Quiet { aws cloudformation wait stack-delete-complete --stack-name $StackName --region $Region } | Out-Null
  return ($LASTEXITCODE -eq 0)
}

try {
  # ---------------------------------------------------------------------------
  Write-Step 'Checking prerequisites'
  foreach ($cmd in 'aws', 'kubectl', 'helm', 'eksctl') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "'$cmd' is not installed or not on PATH" }
  }
  $AccountId = Invoke-Quiet { aws sts get-caller-identity --query Account --output text }
  if ($LASTEXITCODE -ne 0) { throw "AWS credentials are not configured (run 'aws configure')" }

  $Status = Get-StackStatus $StackName
  if ($Status -ne 'NONE') {
    $out = Invoke-Quiet { aws cloudformation describe-stacks --stack-name $StackName --region $Region --query "Stacks[0].Outputs[?OutputKey=='EksClusterName'].OutputValue" --output text }
    if ($out -and $out -ne 'None') { $ClusterName = $out }
  }
  Write-Info "Account $AccountId, region $Region, stack $StackName ($Status), cluster $ClusterName"

  if (-not $Yes) {
    Write-Host ''
    Write-Host 'This permanently deletes the stack, the EKS cluster, all DynamoDB tables,'
    Write-Host "the ECR images and the S3 bucket careroute-files-$AccountId."
    $confirm = Read-Host "Type the stack name ($StackName) to continue"
    if ($confirm -ne $StackName) { throw 'Aborted.' }
  }

  # ---------------------------------------------------------------------------
  Invoke-Quiet { aws eks describe-cluster --name $ClusterName --region $Region } | Out-Null
  $ClusterUp = ($LASTEXITCODE -eq 0)

  if ($ClusterUp) {
    Write-Step "Connecting kubectl to $ClusterName"
    Invoke-Checked { aws eks update-kubeconfig --region $Region --name $ClusterName } | Out-Null

    # Ingresses and LoadBalancer services go first, while the controller is still
    # running to delete their ALBs/NLBs and clear the finalizers.
    Write-Step 'Deleting ingresses and LoadBalancer services (removes the ALB)'
    Invoke-Quiet { kubectl delete ingress --all --all-namespaces --timeout=5m } | Write-Host
    if ($LASTEXITCODE -ne 0) { Write-Warn 'ingress deletion did not finish' }
    $svcJson = Invoke-Quiet { kubectl get svc --all-namespaces -o json }
    if ($svcJson) {
      $lbSvcs = ($svcJson | Out-String | ConvertFrom-Json).items | Where-Object { $_.spec.type -eq 'LoadBalancer' }
      foreach ($svc in $lbSvcs) {
        Invoke-Quiet { kubectl delete svc $svc.metadata.name -n $svc.metadata.namespace --timeout=5m } | Write-Host
      }
    }

    Write-Step 'Deleting application resources'
    Invoke-Quiet { kubectl delete -k (Join-Path $RepoRoot 'k8s\eks') --ignore-not-found --timeout=5m } | Write-Host
    if ($LASTEXITCODE -ne 0) { Write-Warn 'kubectl delete -k did not finish cleanly; continuing' }
    # Catches anything added to the namespaces outside k8s/eks.
    Invoke-Quiet { kubectl delete namespace $AppNamespace aws-observability --ignore-not-found --timeout=5m } | Write-Host
    if ($LASTEXITCODE -ne 0) { Write-Warn 'namespace deletion did not finish; continuing' }

    Write-Step 'Uninstalling the AWS Load Balancer Controller'
    Invoke-Quiet { helm status aws-load-balancer-controller -n kube-system } | Out-Null
    if ($LASTEXITCODE -eq 0) {
      Invoke-Checked { helm uninstall aws-load-balancer-controller -n kube-system --wait }
    } else {
      Write-Info 'Not installed'
    }
  }

  Write-Step "Deleting the controller's IAM service account"
  if ($ClusterUp) {
    Invoke-Quiet { eksctl delete iamserviceaccount --cluster $ClusterName --namespace kube-system --name aws-load-balancer-controller --region $Region --wait } | Write-Host
    if ($LASTEXITCODE -ne 0) { Write-Warn 'eksctl could not delete the service account' }
  }
  # eksctl backs the role with its own CloudFormation stack; remove it directly
  # in case the cluster is already gone or eksctl failed.
  $SaStack = "eksctl-$ClusterName-addon-iamserviceaccount-kube-system-aws-load-balancer-controller"
  if ((Get-StackStatus $SaStack) -ne 'NONE') {
    Invoke-Checked { aws cloudformation delete-stack --stack-name $SaStack --region $Region }
    Invoke-Checked { aws cloudformation wait stack-delete-complete --stack-name $SaStack --region $Region }
  }

  Write-Step 'Removing load balancer resources left behind by the controller'
  Remove-ControllerLeftovers

  # ---------------------------------------------------------------------------
  if ($Status -ne 'NONE') {
    Write-Step 'Emptying S3 buckets and ECR repositories owned by the stack'
    Clear-StackStorage

    Write-Step "Deleting CloudFormation stack $StackName (10-20 minutes)"
    if (-not (Remove-Stack)) {
      Write-Warn 'First delete attempt failed. Resources that could not be deleted:'
      aws cloudformation describe-stack-events --stack-name $StackName --region $Region `
        --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" `
        --output table
      Write-Step 'Cleaning up and retrying'
      Remove-ControllerLeftovers
      Clear-StackStorage
      if (-not (Remove-Stack)) {
        throw 'Stack deletion failed again. Fix the resources above in the console, then re-run this script.'
      }
    }
    Write-Info 'Stack deleted'
  } else {
    Write-Info "Stack $StackName not found; skipping"
  }

  # ---------------------------------------------------------------------------
  Write-Step 'Cleaning up resources created outside the stack'
  $Bucket = "careroute-files-$AccountId"
  Invoke-Quiet { aws s3api head-bucket --bucket $Bucket } | Out-Null
  if ($LASTEXITCODE -eq 0) {
    Invoke-Checked { aws s3 rb "s3://$Bucket" --force --region $Region }
    Write-Info "Deleted bucket $Bucket"
  } else {
    Write-Info "Bucket $Bucket not found"
  }

  $PolicyArn = "arn:aws:iam::${AccountId}:policy/$LbcPolicyName"
  Invoke-Quiet { aws iam get-policy --policy-arn $PolicyArn } | Out-Null
  if ($LASTEXITCODE -eq 0) {
    $roles = Invoke-Checked { aws iam list-entities-for-policy --policy-arn $PolicyArn --query 'PolicyRoles[].RoleName' --output text }
    foreach ($role in (Split-Words $roles)) {
      Invoke-Checked { aws iam detach-role-policy --role-name $role --policy-arn $PolicyArn }
    }
    $versions = Invoke-Checked { aws iam list-policy-versions --policy-arn $PolicyArn --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text }
    foreach ($v in (Split-Words $versions)) {
      Invoke-Checked { aws iam delete-policy-version --policy-arn $PolicyArn --version-id $v }
    }
    Invoke-Checked { aws iam delete-policy --policy-arn $PolicyArn }
    Write-Info "Deleted IAM policy $LbcPolicyName"
  } else {
    Write-Info "IAM policy $LbcPolicyName not found"
  }

  # Container Insights and the Fargate log router can create log groups the stack
  # does not own.
  foreach ($prefix in "/aws/containerinsights/$ClusterName", "/aws/eks/$ClusterName", "/eks/$ClusterName") {
    $groups = Invoke-Quiet { aws logs describe-log-groups --log-group-name-prefix $prefix --region $Region --query 'logGroups[].logGroupName' --output text }
    foreach ($lg in (Split-Words $groups)) {
      Invoke-Quiet { aws logs delete-log-group --log-group-name $lg --region $Region } | Out-Null
      if ($LASTEXITCODE -eq 0) { Write-Info "Deleted log group $lg" }
    }
  }

  Invoke-Quiet { kubectl config delete-context "arn:aws:eks:${Region}:${AccountId}:cluster/$ClusterName" } | Out-Null
  if ($LASTEXITCODE -eq 0) { Write-Info "Removed kubeconfig context for $ClusterName" }

  # ---------------------------------------------------------------------------
  Write-Step "Checking for anything still billing in $Region"
  $Remaining = $false
  $checks = [ordered]@{
    'EKS clusters'         = { aws eks list-clusters --query 'clusters[]' --region $Region --output text }
    'NAT gateways'         = { aws ec2 describe-nat-gateways --filter Name=state,Values=pending,available --query 'NatGateways[].NatGatewayId' --region $Region --output text }
    'Elastic IPs'          = { aws ec2 describe-addresses --query 'Addresses[].PublicIp' --region $Region --output text }
    'ElastiCache clusters' = { aws elasticache describe-cache-clusters --query 'CacheClusters[].CacheClusterId' --region $Region --output text }
    'Load balancers'       = { aws elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName' --region $Region --output text }
  }
  foreach ($label in $checks.Keys) {
    $result = "$(Invoke-Quiet $checks[$label])".Trim()
    if ($result -and $result -ne 'None') {
      Write-Warn "$label still present: $result"
      $Remaining = $true
    } else {
      Write-Info "${label}: none"
    }
  }

  Write-Host ''
  if (-not $Remaining) {
    Write-Host 'Teardown complete.' -ForegroundColor Green
  } else {
    Write-Host 'Teardown finished, but some resources above still exist in the region (they may belong to something else).' -ForegroundColor Yellow
  }
}
catch {
  Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
