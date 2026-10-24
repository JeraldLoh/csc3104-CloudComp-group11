#!/usr/bin/env bash
# Tears down everything deploy.sh created (README "Teardown" section), in the
# order that avoids orphaned resources:
#   k8s resources (deletes the ALB) -> Load Balancer Controller + its IRSA role ->
#   leftover controller-made LBs/target groups/security groups ->
#   CloudFormation stack -> out-of-stack S3 bucket, careroute-* ECR repositories,
#   IAM policy and log groups.
#
# Nothing about the stack's contents is hardcoded: S3 buckets and ECR repos are
# looked up from the live stack and emptied before deletion, so resources added
# to the CloudFormation template later are torn down too.
#
# Usage: ./scripts/macos/teardown.sh [--yes] [--stack-name NAME] [--region REGION]

set -uo pipefail

STACK_NAME="${STACK_NAME:-careroute-stack-1}"
REGION="${REGION:-ap-southeast-1}"
CLUSTER_NAME="${CLUSTER_NAME:-careroute-cluster-1}"
APP_NAMESPACE="${APP_NAMESPACE:-careroute}"
LBC_POLICY_NAME="AWSLoadBalancerControllerIAMPolicy"
ASSUME_YES=false

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes)     ASSUME_YES=true; shift ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --region)     REGION="$2"; shift 2 ;;
    -h|--help)    sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m    WARNING: %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

stack_status() {
  aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo NONE
}

stack_output() {
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text 2>/dev/null
}

stack_resources_of_type() {
  aws cloudformation list-stack-resources --stack-name "$STACK_NAME" --region "$REGION" \
    --query "StackResourceSummaries[?ResourceType=='$1' && ResourceStatus!='DELETE_COMPLETE'].PhysicalResourceId" \
    --output text 2>/dev/null
}

# Deletes load balancers, target groups and security groups the AWS Load
# Balancer Controller created for this cluster. These live outside the stack
# and block VPC deletion if the controller did not clean them up.
cleanup_controller_leftovers() {
  local arns arn sgs sg rules
  arns="$(aws resourcegroupstaggingapi get-resources --region "$REGION" \
    --tag-filters "Key=elbv2.k8s.aws/cluster,Values=$CLUSTER_NAME" \
    --resource-type-filters elasticloadbalancing:loadbalancer \
    --query 'ResourceTagMappingList[].ResourceARN' --output text 2>/dev/null)"
  for arn in $arns; do
    info "Deleting leftover load balancer $arn"
    aws elbv2 delete-load-balancer --load-balancer-arn "$arn" --region "$REGION" || true
  done
  [ -n "$arns" ] && aws elbv2 wait load-balancers-deleted --load-balancer-arns $arns --region "$REGION" 2>/dev/null

  arns="$(aws resourcegroupstaggingapi get-resources --region "$REGION" \
    --tag-filters "Key=elbv2.k8s.aws/cluster,Values=$CLUSTER_NAME" \
    --resource-type-filters elasticloadbalancing:targetgroup \
    --query 'ResourceTagMappingList[].ResourceARN' --output text 2>/dev/null)"
  for arn in $arns; do
    info "Deleting leftover target group $arn"
    aws elbv2 delete-target-group --target-group-arn "$arn" --region "$REGION" || true
  done

  sgs="$(aws ec2 describe-security-groups --region "$REGION" \
    --filters "Name=tag:elbv2.k8s.aws/cluster,Values=$CLUSTER_NAME" \
    --query 'SecurityGroups[].GroupId' --output text 2>/dev/null)"
  for sg in $sgs; do
    # Remove rules in other groups that reference this one, then the group itself.
    rules="$(aws ec2 describe-security-group-rules --region "$REGION" \
      --filters "Name=referenced-group-info.group-id,Values=$sg" \
      --query 'SecurityGroupRules[?!IsEgress].[GroupId,SecurityGroupRuleId]' --output text 2>/dev/null)"
    printf '%s\n' "$rules" | while read -r gid rid; do
      [ -n "$rid" ] && aws ec2 revoke-security-group-ingress --group-id "$gid" \
        --security-group-rule-ids "$rid" --region "$REGION" >/dev/null 2>&1
    done
    info "Deleting leftover security group $sg"
    for _ in 1 2 3 4 5 6; do
      aws ec2 delete-security-group --group-id "$sg" --region "$REGION" 2>/dev/null && break
      sleep 20   # ENIs from a just-deleted ALB take a moment to detach
    done
  done
}

empty_stack_storage() {
  local b r
  for b in $(stack_resources_of_type AWS::S3::Bucket); do
    info "Emptying stack bucket s3://$b"
    aws s3 rm "s3://$b" --recursive --region "$REGION" >/dev/null || true
  done
  for r in $(stack_resources_of_type AWS::ECR::Repository); do
    local ids
    ids="$(aws ecr list-images --repository-name "$r" --region "$REGION" \
      --query 'imageIds[].imageDigest' --output text 2>/dev/null)"
    if [ -n "$ids" ]; then
      info "Deleting images in ECR repository $r"
      for d in $ids; do
        aws ecr batch-delete-image --repository-name "$r" --region "$REGION" \
          --image-ids "imageDigest=$d" >/dev/null || true
      done
    fi
  done
}

delete_stack() {
  aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION" 2>/dev/null
}

# ---------------------------------------------------------------------------
step "Checking prerequisites"
for cmd in aws kubectl helm eksctl; do
  command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' is not installed or not on PATH"
done
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" \
  || die "AWS credentials are not configured (run 'aws configure')"

STATUS="$(stack_status "$STACK_NAME")"
if [ "$STATUS" != "NONE" ]; then
  OUT="$(stack_output EksClusterName)"; [ -n "$OUT" ] && [ "$OUT" != "None" ] && CLUSTER_NAME="$OUT"
fi
info "Account $ACCOUNT_ID, region $REGION, stack $STACK_NAME ($STATUS), cluster $CLUSTER_NAME"

if [ "$ASSUME_YES" != true ]; then
  echo
  echo "This permanently deletes the stack, the EKS cluster, all DynamoDB tables,"
  echo "the ECR images, the careroute-* ECR repositories and the S3 bucket"
  echo "careroute-files-$ACCOUNT_ID."
  printf 'Type the stack name (%s) to continue: ' "$STACK_NAME"
  read -r CONFIRM
  [ "$CONFIRM" = "$STACK_NAME" ] || die "Aborted."
fi

# ---------------------------------------------------------------------------
CLUSTER_UP=false
if aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" >/dev/null 2>&1; then
  CLUSTER_UP=true
fi

if [ "$CLUSTER_UP" = true ]; then
  step "Connecting kubectl to $CLUSTER_NAME"
  aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER_NAME" >/dev/null

  # Ingresses and LoadBalancer services go first, while the controller is still
  # running to delete their ALBs/NLBs and clear the finalizers.
  step "Deleting ingresses and LoadBalancer services (removes the ALB)"
  kubectl delete ingress --all --all-namespaces --timeout=5m || warn "ingress deletion did not finish"
  LB_SVCS="$(kubectl get svc --all-namespaces \
    -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2>/dev/null)"
  printf '%s\n' "$LB_SVCS" | while IFS=/ read -r ns name; do
    [ -n "$name" ] && kubectl delete svc "$name" -n "$ns" --timeout=5m
  done

  step "Deleting application resources"
  kubectl delete -k "$REPO_ROOT/k8s/eks" --ignore-not-found --timeout=5m \
    || warn "kubectl delete -k did not finish cleanly; continuing"
  # Catches anything added to the namespaces outside k8s/eks.
  kubectl delete namespace "$APP_NAMESPACE" aws-observability --ignore-not-found --timeout=5m \
    || warn "namespace deletion did not finish; continuing"

  step "Uninstalling the AWS Load Balancer Controller"
  if helm status aws-load-balancer-controller -n kube-system >/dev/null 2>&1; then
    helm uninstall aws-load-balancer-controller -n kube-system --wait
  else
    info "Not installed"
  fi
fi

step "Deleting the controller's IAM service account"
if [ "$CLUSTER_UP" = true ]; then
  eksctl delete iamserviceaccount --cluster "$CLUSTER_NAME" --namespace kube-system \
    --name aws-load-balancer-controller --region "$REGION" --wait \
    || warn "eksctl could not delete the service account"
fi
# eksctl backs the role with its own CloudFormation stack; remove it directly
# in case the cluster is already gone or eksctl failed.
SA_STACK="eksctl-$CLUSTER_NAME-addon-iamserviceaccount-kube-system-aws-load-balancer-controller"
if [ "$(stack_status "$SA_STACK")" != "NONE" ]; then
  aws cloudformation delete-stack --stack-name "$SA_STACK" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$SA_STACK" --region "$REGION"
fi

step "Removing load balancer resources left behind by the controller"
cleanup_controller_leftovers

# ---------------------------------------------------------------------------
if [ "$STATUS" != "NONE" ]; then
  step "Emptying S3 buckets and ECR repositories owned by the stack"
  empty_stack_storage

  step "Deleting CloudFormation stack $STACK_NAME (10-20 minutes)"
  if ! delete_stack; then
    warn "First delete attempt failed. Resources that could not be deleted:"
    aws cloudformation describe-stack-events --stack-name "$STACK_NAME" --region "$REGION" \
      --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
      --output table
    step "Cleaning up and retrying"
    cleanup_controller_leftovers
    empty_stack_storage
    delete_stack || die "Stack deletion failed again. Fix the resources above in the console, then re-run this script."
  fi
  info "Stack deleted"
else
  info "Stack $STACK_NAME not found; skipping"
fi

# ---------------------------------------------------------------------------
step "Cleaning up resources created outside the stack"
BUCKET="careroute-files-$ACCOUNT_ID"
if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  aws s3 rb "s3://$BUCKET" --force --region "$REGION" && info "Deleted bucket $BUCKET"
else
  info "Bucket $BUCKET not found"
fi

# Per-service repositories created by deploy.sh --service.
for repo in $(aws ecr describe-repositories --region "$REGION" \
    --query "repositories[?starts_with(repositoryName, 'careroute-')].repositoryName" \
    --output text 2>/dev/null); do
  aws ecr delete-repository --repository-name "$repo" --force --region "$REGION" >/dev/null \
    && info "Deleted ECR repository $repo"
done

POLICY_ARN="arn:aws:iam::$ACCOUNT_ID:policy/$LBC_POLICY_NAME"
if aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  for role in $(aws iam list-entities-for-policy --policy-arn "$POLICY_ARN" \
      --query 'PolicyRoles[].RoleName' --output text); do
    aws iam detach-role-policy --role-name "$role" --policy-arn "$POLICY_ARN"
  done
  for v in $(aws iam list-policy-versions --policy-arn "$POLICY_ARN" \
      --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text); do
    aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$v"
  done
  aws iam delete-policy --policy-arn "$POLICY_ARN" && info "Deleted IAM policy $LBC_POLICY_NAME"
else
  info "IAM policy $LBC_POLICY_NAME not found"
fi

# Container Insights and the Fargate log router can create log groups the stack
# does not own.
for prefix in "/aws/containerinsights/$CLUSTER_NAME" "/aws/eks/$CLUSTER_NAME" "/eks/$CLUSTER_NAME"; do
  for lg in $(aws logs describe-log-groups --log-group-name-prefix "$prefix" --region "$REGION" \
      --query 'logGroups[].logGroupName' --output text 2>/dev/null); do
    aws logs delete-log-group --log-group-name "$lg" --region "$REGION" && info "Deleted log group $lg"
  done
done

kubectl config delete-context "arn:aws:eks:$REGION:$ACCOUNT_ID:cluster/$CLUSTER_NAME" >/dev/null 2>&1 \
  && info "Removed kubeconfig context for $CLUSTER_NAME"

# ---------------------------------------------------------------------------
step "Checking for anything still billing in $REGION"
REMAINING=0
check() {  # check <label> <aws args...>
  local label="$1"; shift
  local result
  result="$(aws "$@" --region "$REGION" --output text 2>/dev/null)"
  if [ -n "$result" ] && [ "$result" != "None" ]; then
    warn "$label still present: $result"; REMAINING=1
  else
    info "$label: none"
  fi
}
check "EKS clusters"          eks list-clusters --query 'clusters[]'
check "NAT gateways"          ec2 describe-nat-gateways --filter Name=state,Values=pending,available --query 'NatGateways[].NatGatewayId'
check "Elastic IPs"           ec2 describe-addresses --query 'Addresses[].PublicIp'
check "ElastiCache clusters"  elasticache describe-cache-clusters --query 'CacheClusters[].CacheClusterId'
check "Load balancers"        elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName'

echo
if [ "$REMAINING" -eq 0 ]; then
  printf '\033[1;32mTeardown complete.\033[0m\n'
else
  printf '\033[1;33mTeardown finished, but some resources above still exist in the region (they may belong to something else).\033[0m\n'
fi
