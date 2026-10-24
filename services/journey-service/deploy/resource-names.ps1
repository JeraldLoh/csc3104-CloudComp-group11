# Dot-sourced by the deploy and teardown scripts:   . (Join-Path $PSScriptRoot 'resource-names.ps1')
#
# The team decided on the resource prefix "careroute" (careroute-routes, careroute-trips, careroute-disruptions, ...).
# The platform stack builds every name from its ResourcePrefix parameter, and its IAM role only allows resources whose
# names start with it. So instead of hardcoding a prefix, our scripts read the one the stack was really built with:
# they work before and after the platform owner changes his default, and warn while it is not "careroute".

$TeamPrefix = 'careroute'

# The prefix to use: -ResourcePrefix if given, else the stack's ResourcePrefix parameter, else the team's.
function Resolve-ResourcePrefix([string]$StackName, [string]$Region, [string]$Override) {
  if ($Override) {
    $prefix, $source = $Override, 'the -ResourcePrefix parameter'
  } else {
    $found = $null
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
      $found = & aws cloudformation describe-stacks --stack-name $StackName --region $Region `
        --query "Stacks[0].Parameters[?ParameterKey=='ResourcePrefix'].ParameterValue | [0]" --output text 2>$null
      if ($LASTEXITCODE -ne 0) { $found = $null }
    } finally { $ErrorActionPreference = $prev }
    if ($found -and $found.Trim() -and $found.Trim() -ne 'None') {
      $prefix, $source = $found.Trim(), "stack $StackName"
    } else {
      $prefix, $source = $TeamPrefix, 'the team default (the stack has no ResourcePrefix parameter, or could not be read)'
    }
  }
  Write-Host "    Resource prefix: $prefix (from $source)"
  if ($prefix -ne $TeamPrefix) {
    Write-Host "    WARNING: the team decided on '$TeamPrefix'. Stack $StackName names its tables, queues and buckets with '$prefix'," -ForegroundColor Yellow
    Write-Host "    so this script follows that. The platform owner should set ResourcePrefix=$TeamPrefix (see deploy\README.md)." -ForegroundColor Yellow
  }
  return $prefix
}

# The shared bucket: <prefix>-files-<account id> (the platform scripts' naming), unless -PacksBucket says otherwise.
function Get-SharedBucketName([string]$Prefix, [string]$AccountId, [string]$Override) {
  if ($Override) { return $Override }
  return "$Prefix-files-$AccountId"
}
