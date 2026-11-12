# csc3104-CloudComp-group11

## Architecture

The system runs on AWS in `ap-southeast-1` (Singapore). A VPC spans two Availability Zones with public and private subnets. The public subnets hold the Application Load Balancer and a NAT gateway. The private subnets hold an EKS Fargate cluster running the microservices and an ElastiCache Redis instance for low-latency caching.

Infrastructure is defined as code in `cloudformation/careroute-vpc-eks-standard.yaml`. It creates the VPC, subnets, internet gateway, NAT gateway, S3 and DynamoDB gateway endpoints, security groups, ElastiCache Redis, SQS queues (checkins and disruptions, each with a dead-letter queue), 8 DynamoDB tables (on-demand billing), IAM roles, the EKS cluster with Fargate profiles, an ECR repository, and an optional monthly cost budget alert.

## Prerequisites

- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) configured with credentials (`aws configure`)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/docs/intro/install/) (`winget install -e --id Helm.Helm`)
- [eksctl](https://eksctl.io/installation/) (`winget install -e --id eksctl.eksctl`)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running

## Automated deploy and teardown

The scripts in `scripts/` run every step in the sections below, from the
CloudFormation stack to the `/health` check, and the full teardown. Both are
safe to re-run. `deploy` creates or updates the stack from the current template,
so changes to the YAML are applied on the next run. `teardown` looks up S3
buckets and ECR repositories from the live stack, so resources added to the
template later are removed too.

| | Windows (PowerShell) | macOS (bash) |
|---|---|---|
| Deploy | `powershell -ExecutionPolicy Bypass -File scripts\windows\deploy.ps1 -BudgetEmail you@example.com` | `bash scripts/macos/deploy.sh --email you@example.com` |
| Teardown | `powershell -ExecutionPolicy Bypass -File scripts\windows\teardown.ps1` | `bash scripts/macos/teardown.sh` |

Options: `-ImageTag` / `--tag` (default `v1`), `-StackName` / `--stack-name`,
`-Region` / `--region`, and `-Yes` / `--yes` to skip the teardown confirmation.
The deploy scripts fill in the `k8s/eks` placeholders in a temporary copy, so
the committed manifests stay unchanged.

## Automatic deployment (GitHub Actions)

[`.github/workflows/services.yml`](.github/workflows/services.yml) runs for every change under
`services/` or `k8s/eks/services/`, and only for the services that changed:

- **Pull request:** runs the service's tests and builds its Docker image.
- **Merge to `main`:** does the same, then pushes the image to ECR (tagged with the commit SHA) and
  rolls it out on EKS. If the new pods never become ready, it rolls back to the previous version.
- **Manual run:** Actions > Services > Run workflow, with one service name or `all`. Use it after
  creating the stack, since merges made while the stack was torn down skip the deploy.

A service is picked up when it has both `services/<name>/Dockerfile` and `k8s/eks/services/<name>.yaml`,
with the same placeholders as `deploy --service` (see [scripts/README.md](scripts/README.md)).
The frontends are not part of this workflow: Amplify builds them from `amplify.yml`.

### One-time setup

The workflow signs in to AWS through GitHub's OIDC provider, so no access keys are stored in GitHub.

1. Create the deploy role, once per AWS account. It lives in its own stack, so teardown leaves it
   in place, and it costs nothing:

   ```bash
   aws cloudformation deploy --stack-name careroute-github-deploy \
     --template-file cloudformation/github-actions-deploy-role.yaml \
     --capabilities CAPABILITY_NAMED_IAM --region ap-southeast-1
   aws cloudformation describe-stacks --stack-name careroute-github-deploy --region ap-southeast-1 \
     --query "Stacks[0].Outputs[?OutputKey=='DeployRoleArn'].OutputValue" --output text
   ```

   If the account already has a `token.actions.githubusercontent.com` identity provider, add
   `--parameter-overrides CreateOidcProvider=false`.
2. In GitHub, go to Settings > Secrets and variables > Actions > **Variables:** `AWS_DEPLOY_ROLE_ARN` =
   the ARN printed above. Without it, merges still test and build but don't deploy. No secrets are
   needed: the tests fake AWS with moto.
3. Create the Cognito stack (next section). The Alert, Check-in and Journey Services need it.

The role can only push to `careroute-*` ECR repositories and deploy into the `careroute` namespace.
On each new cluster it adds itself as an EKS access entry with namespace-scoped edit rights, so
nothing needs to be re-run after a teardown.

## Logins (Cognito)

Issue 1.5. [`cloudformation/careroute-cognito.yaml`](cloudformation/careroute-cognito.yaml) is a Cognito
user pool with `caregiver` and `traveller` groups. It is its own stack, so teardown keeps everyone's
accounts, and it costs nothing at this size.

- **Caregivers** sign up in the caregiver app with their email. Once the emailed code is confirmed, a
  Lambda gives them a `G-` ID and the `caregiver` group.
- **Travellers** can't sign up. When a caregiver adds one, the Alert Service creates their account
  (username = their `U-` ID) and shows the caregiver a **setup code**. The traveller app asks for the
  ID and code once. "New setup code" on the traveller's page replaces it, for a new or lost phone.
- **Services** (Alert, Check-in, Journey) run with `AUTH_MODE=cognito` on EKS and check every request's
  ID token: signature, issuer, expiry, app client, the `custom:userId` claim and the group.

Deploy it once per AWS account:

```bash
aws cloudformation deploy --stack-name careroute-cognito \
  --template-file cloudformation/careroute-cognito.yaml \
  --capabilities CAPABILITY_IAM --region ap-southeast-1
aws cloudformation describe-stacks --stack-name careroute-cognito --region ap-southeast-1 \
  --query "Stacks[0].Outputs" --output table
```

The service deploys (`deploy --service`, GitHub Actions) read the pool ID and client ID from this
stack, and refuse to deploy a service that needs them if it doesn't exist. For the **apps**, set
these environment variables (in Amplify: App settings > Environment variables, then redeploy; locally,
in `apps/<app>/.env.local`):

| Variable | Value |
|---|---|
| `VITE_COGNITO_REGION` | `ap-southeast-1` |
| `VITE_COGNITO_CLIENT_ID` | the `UserPoolClientId` output |

Without them the apps keep their local stand-ins: the caregiver app's "Act as" box and the
traveller app's trip link (services with `AUTH_MODE=dev` or `off`). The pool has deletion protection
on; to delete the stack, turn it off on the pool first.

To try logins on the local kind cluster, run `local-sim` with `-Cognito` (`--cognito` on Mac/Linux, see "Everything
in one command" below). It reads the pool's IDs from this stack, switches the Alert, Check-in and Journey Services to
`AUTH_MODE=cognito` and starts both apps with their login pages, so no `.env.local` is needed. A run without it
switches them back. Locally, adding a traveller can't make their setup code (the pods only have LocalStack's keys,
not the AWS role), so the caregiver app says so; on EKS it works.

## HTTPS for the apps (CloudFront)

Amplify serves the apps over HTTPS, but the ALB only has plain HTTP (HTTPS on it needs a certificate, which needs a
domain). A browser blocks an HTTPS page's calls to an HTTP address (mixed content), so the apps can't call the ALB
directly. `cloudformation/careroute-api-https.yaml` puts CloudFront in front of the ALB: an
`https://dxxxx.cloudfront.net` address with its own certificate that only passes requests on. Nothing is cached,
every header goes through (the login's `Authorization` included), and `/api/stream` stays open (the Alert Service
pings every 15 s, inside the 60 s timeout).

The full `deploy` (not `--service`) creates or updates it at the end, pointed at the current ALB, and prints its
address. It is its own stack, and `teardown` keeps it like the Cognito one, so the address stays the same from one
deploy to the next and an idle distribution costs nothing. `teardown -DeleteHttps` (`--delete-https`) removes it,
`deploy -NoHttps` (`--no-https`) skips it. When the ALB has changed, the update takes 5-15 minutes.

The apps then call the API through their own address, so no CORS is needed. Once per Amplify app, add this rewrite
(App settings > Rewrites and redirects), above any other rule:

| Source address | Target address | Type |
|---|---|---|
| `/api/<*>` | `https://<ApiDomain>/api/<*>` | 200 (Rewrite) |

`<ApiDomain>` is the address `deploy` prints, or:

```bash
aws cloudformation describe-stacks --stack-name careroute-api-https --region ap-southeast-1 \
  --query "Stacks[0].Outputs[?OutputKey=='ApiDomain'].OutputValue" --output text
```

The same setup, made by hand on 10 Nov, carried a whole trip from the traveller app on Amplify (`team_trip.py`, login
off). Not yet run with logins on: the first run should check a signed-in request, such as the traveller app loading
its trip.

## Deploy the infrastructure

### 1. Deploy the CloudFormation stack

The same command creates the stack and, after a template change, updates it. It waits until it's done (15–25
minutes the first time, a few minutes for an update). Commands below are PowerShell; in bash, end the lines with
`\` instead of `` ` ``.

```powershell
aws cloudformation deploy `
  --stack-name careroute-stack-1 `
  --template-file cloudformation/careroute-vpc-eks-standard.yaml `
  --parameter-overrides ClusterName=careroute-cluster-1 ComputeMode=fargate AppPort=8000 AppNamespace=careroute `
                        BudgetAlertEmail=you@example.com `
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM `
  --region ap-southeast-1 `
  --no-fail-on-empty-changeset
```

- `BudgetAlertEmail` creates a monthly budget with alerts at 80% and 100% of $80. AWS sends two confirmation
  emails: click both. Leave it out on an update and the stack keeps the earlier value.
- If a first create failed, the stack is stuck in `ROLLBACK_COMPLETE` and can only be deleted. Delete it, then run
  the command again:

  ```powershell
  aws cloudformation describe-stacks --stack-name careroute-stack-1 --region ap-southeast-1 --query "Stacks[0].StackStatus"
  aws cloudformation delete-stack --stack-name careroute-stack-1 --region ap-southeast-1
  aws cloudformation wait stack-delete-complete --stack-name careroute-stack-1 --region ap-southeast-1
  ```
- The services also need the Cognito stack ("Logins (Cognito)" above). It's separate and stays between teardowns.

### 2. Collect the stack outputs

```powershell
aws cloudformation describe-stacks --stack-name careroute-stack-1 --region ap-southeast-1 --query "Stacks[0].Outputs" --output table
```

Note the following values for later steps:

| Output | Used in |
|---|---|
| `EcrRepositoryUri` | `k8s/eks/kustomization.yaml` |
| `ApiServiceRoleArn` | `k8s/eks/serviceaccount.yaml` |
| `RedisEndpoint` | `k8s/eks/fastapi.yaml` |
| `AlbSecurityGroupId` | `k8s/eks/ingress.yaml` |
| `VpcId` | Helm install of the load balancer controller |
| `CheckinsQueueUrl` | `k8s/eks/fastapi.yaml` (env var) |
| `DisruptionsQueueUrl` | `k8s/eks/fastapi.yaml` (env var) |

### 3. Connect kubectl to the cluster

```powershell
aws eks update-kubeconfig --region ap-southeast-1 --name careroute-cluster-1
kubectl get pods -A
```

### 4. Install the AWS Load Balancer Controller

The controller turns the Kubernetes ingresses into the ALB. Without it, no service gets a public address. These
steps are safe to repeat, for example after a teardown or to move to a newer controller.

Look up the chart version (`version:`) and controller version (`appVersion:`):

```powershell
helm repo add eks https://aws.github.io/eks-charts
helm repo update eks
helm show chart eks/aws-load-balancer-controller
```

Download the IAM policy for that controller version, and create it. Run `iam_policy.json` commands from a scratch
folder: the file is in `.gitignore`, but it doesn't belong in the repo.

```powershell
curl.exe -o iam_policy.json https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/<APP_VERSION>/docs/install/iam_policy.json
aws iam create-policy `
  --policy-name AWSLoadBalancerControllerIAMPolicy `
  --policy-document file://iam_policy.json
```

If it says `EntityAlreadyExists` (a teardown missed it, or you're upgrading the controller), update it instead.
IAM keeps at most 5 versions, so if that fails with `LimitExceeded`, delete an old version first
(`aws iam list-policy-versions`, then `aws iam delete-policy-version --version-id v1 ...`):

```powershell
aws iam create-policy-version `
  --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/AWSLoadBalancerControllerIAMPolicy `
  --policy-document file://iam_policy.json `
  --set-as-default
```

Create the controller's service account and IAM role:

```powershell
eksctl create iamserviceaccount `
  --cluster careroute-cluster-1 `
  --namespace kube-system `
  --name aws-load-balancer-controller `
  --attach-policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/AWSLoadBalancerControllerIAMPolicy `
  --override-existing-serviceaccounts `
  --region ap-southeast-1 `
  --approve
```

Install the Helm chart (or upgrade it, if it's already there) with the `VpcId` output from step 2:

```powershell
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller `
  -n kube-system `
  --set clusterName=careroute-cluster-1 `
  --set serviceAccount.create=false `
  --set serviceAccount.name=aws-load-balancer-controller `
  --set region=ap-southeast-1 `
  --set vpcId=<VPC_ID> `
  --version <CHART_VERSION>
```

**If the controller was already installed**, restart it. Every upgrade makes a new webhook certificate, and until
the pods restart, every ingress or service change fails with `x509: certificate signed by unknown authority`:

```powershell
kubectl rollout restart deployment/aws-load-balancer-controller -n kube-system
```

Wait until it's ready (Fargate pods take 1–2 minutes):

```powershell
kubectl rollout status deployment/aws-load-balancer-controller -n kube-system --timeout=10m
```

## Deploy the application

### 1. Build and push the image

```powershell
$ECR = "<ACCOUNT_ID>.dkr.ecr.ap-southeast-1.amazonaws.com"
aws ecr get-login-password --region ap-southeast-1 | docker login --username AWS --password-stdin $ECR
docker build --platform linux/amd64 -t "$ECR/cloud-fastapi:v1" .
docker push "$ECR/cloud-fastapi:v1"
```

### 2. Create the S3 bucket

SQS queues and DynamoDB tables are created by the CloudFormation stack. The S3
bucket is the only resource created manually:

```powershell
aws s3 mb s3://careroute-files-<ACCOUNT_ID> --region ap-southeast-1
```

### 3. Fill in the placeholders

Update the following files in `k8s/eks/` with the stack outputs:

| File | Placeholder | Value |
|---|---|---|
| `kustomization.yaml` | `<ACCOUNT_ID>...` under `newName` | `EcrRepositoryUri` output |
| `serviceaccount.yaml` | role ARN | `ApiServiceRoleArn` output |
| `fastapi.yaml` | `<REDIS_ENDPOINT>` | `RedisEndpoint` output |
| `ingress.yaml` | `sg-REPLACE_ME` | `AlbSecurityGroupId` output |

### 4. Apply the manifests

```powershell
kubectl apply -k k8s/eks
kubectl get pods -n careroute -w
```

Fargate pods take 1–2 minutes to start. Press `Ctrl+C` once both replicas show `1/1 Running`.

### 5. Verify

Test through the ALB:

```powershell
kubectl get ingress -n careroute
curl.exe http://<ALB_ADDRESS>/health
```

The ALB address appears within a few minutes. The health endpoint should return
`{"status":"ok"}`.

To confirm the IRSA role is wired up:

```powershell
kubectl exec -n careroute deploy/cloud-fastapi -- env | findstr AWS_
```

You should see `AWS_ROLE_ARN` and `AWS_WEB_IDENTITY_TOKEN_FILE`.

## Updating the application

Push a new image tag, update `newTag` in `k8s/eks/kustomization.yaml`, and reapply:

```powershell
docker build --platform linux/amd64 -t "$ECR/cloud-fastapi:v2" .
docker push "$ECR/cloud-fastapi:v2"
kubectl apply -k k8s/eks
```

## Teardown

The environment costs about $0.30/hour while it exists, so delete it at the end of each session. **The order
matters:** the load balancer must go while its controller still runs, or it keeps billing and blocks the stack
delete. The whole teardown takes 15–25 minutes. Commands are PowerShell; bash versions follow where they differ.

The Cognito stack (`careroute-cognito`) and the GitHub deploy role (`careroute-github-deploy`) are separate stacks:
**don't delete them**. They cost nothing and hold everyone's accounts.

### 1. Before you start

If the Journey Service's disruption Lambda is deployed, remove it first: it holds network interfaces in the VPC
that block the stack delete. See [services/journey-service/deploy/README.md](services/journey-service/deploy/README.md)
(`teardown-lambda`).

```powershell
aws eks update-kubeconfig --region ap-southeast-1 --name careroute-cluster-1
```

### 2. Delete the load balancer (while the controller still runs)

Delete **every** ingress, not just `k8s/eks`: the services' ingresses (e.g. the Check-in Service's) share the same
ALB, and it only goes when all of them are gone.

```powershell
kubectl delete ingress --all --all-namespaces --timeout=5m
kubectl get svc --all-namespaces | findstr LoadBalancer      # bash: | grep LoadBalancer
```

If the second command lists anything, delete those services too (`kubectl delete svc <name> -n <namespace>`).

### 3. Delete the application and the controller

```powershell
kubectl delete namespace careroute aws-observability --ignore-not-found --timeout=5m
helm uninstall aws-load-balancer-controller -n kube-system --wait
eksctl delete iamserviceaccount --cluster careroute-cluster-1 --namespace kube-system --name aws-load-balancer-controller --region ap-southeast-1 --wait
```

Deleting the namespaces removes everything in them, including services GitHub Actions deployed. If `eksctl` fails
(e.g. the cluster is already gone), delete the stack it made instead:

```powershell
aws cloudformation delete-stack --stack-name eksctl-careroute-cluster-1-addon-iamserviceaccount-kube-system-aws-load-balancer-controller --region ap-southeast-1
```

### 4. Check for anything the controller left behind

These should all print nothing (`[]`). The controller tags what it makes with the cluster name:

```powershell
aws resourcegroupstaggingapi get-resources --region ap-southeast-1 --tag-filters Key=elbv2.k8s.aws/cluster,Values=careroute-cluster-1 --query "ResourceTagMappingList[].ResourceARN"
aws ec2 describe-security-groups --region ap-southeast-1 --filters Name=tag:elbv2.k8s.aws/cluster,Values=careroute-cluster-1 --query "SecurityGroups[].GroupId"
```

If they list anything, delete it: load balancers first (`aws elbv2 delete-load-balancer --load-balancer-arn ...`),
then target groups (`aws elbv2 delete-target-group --target-group-arn ...`), then security groups
(`aws ec2 delete-security-group --group-id ...`). A security group can take a minute to free up after its load
balancer goes; if it's still in use by another group's rule, remove that rule in the EC2 console first.

### 5. Delete the stack

This removes the VPC, EKS, Redis, queues, tables, IAM roles and the stack's ECR repository (emptied
automatically). 10–20 minutes:

```powershell
aws cloudformation delete-stack --stack-name careroute-stack-1 --region ap-southeast-1
aws cloudformation wait stack-delete-complete --stack-name careroute-stack-1 --region ap-southeast-1
```

If the wait reports a failure, see what couldn't be deleted, fix it (usually step 4 again), and run both commands
again:

```powershell
aws cloudformation describe-stack-events --stack-name careroute-stack-1 --region ap-southeast-1 --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" --output table
```

### 6. Delete what was made outside the stack

The S3 bucket, the services' ECR repositories (made by GitHub Actions and the service deploys), the controller's IAM
policy and the log groups aren't part of the stack, and the repositories and bucket keep costing money.

```powershell
aws s3 rb s3://careroute-files-<ACCOUNT_ID> --force --region ap-southeast-1

# Every careroute-* ECR repository
$repos = aws ecr describe-repositories --region ap-southeast-1 --query "repositories[?starts_with(repositoryName, 'careroute-')].repositoryName" --output text
foreach ($r in -split $repos) { aws ecr delete-repository --repository-name $r --force --region ap-southeast-1 }

# The controller's IAM policy: old versions first, then the policy
$policy = "arn:aws:iam::<ACCOUNT_ID>:policy/AWSLoadBalancerControllerIAMPolicy"
$old = aws iam list-policy-versions --policy-arn $policy --query "Versions[?IsDefaultVersion==``false``].VersionId" --output text
foreach ($v in -split $old) { aws iam delete-policy-version --policy-arn $policy --version-id $v }
aws iam delete-policy --policy-arn $policy

# Log groups from Container Insights and the Fargate log router
foreach ($prefix in "/aws/containerinsights/careroute-cluster-1", "/aws/eks/careroute-cluster-1", "/eks/careroute-cluster-1") {
  $groups = aws logs describe-log-groups --log-group-name-prefix $prefix --region ap-southeast-1 --query "logGroups[].logGroupName" --output text
  foreach ($g in -split $groups) { aws logs delete-log-group --log-group-name $g --region ap-southeast-1 }
}

kubectl config delete-context arn:aws:eks:ap-southeast-1:<ACCOUNT_ID>:cluster/careroute-cluster-1
```

The same in bash:

```bash
aws s3 rb s3://careroute-files-<ACCOUNT_ID> --force --region ap-southeast-1

for r in $(aws ecr describe-repositories --region ap-southeast-1 --query "repositories[?starts_with(repositoryName, 'careroute-')].repositoryName" --output text); do
  aws ecr delete-repository --repository-name "$r" --force --region ap-southeast-1
done

policy="arn:aws:iam::<ACCOUNT_ID>:policy/AWSLoadBalancerControllerIAMPolicy"
for v in $(aws iam list-policy-versions --policy-arn "$policy" --query 'Versions[?IsDefaultVersion==`false`].VersionId' --output text); do
  aws iam delete-policy-version --policy-arn "$policy" --version-id "$v"
done
aws iam delete-policy --policy-arn "$policy"

for prefix in /aws/containerinsights/careroute-cluster-1 /aws/eks/careroute-cluster-1 /eks/careroute-cluster-1; do
  for g in $(aws logs describe-log-groups --log-group-name-prefix "$prefix" --region ap-southeast-1 --query 'logGroups[].logGroupName' --output text); do
    aws logs delete-log-group --log-group-name "$g" --region ap-southeast-1
  done
done

kubectl config delete-context arn:aws:eks:ap-southeast-1:<ACCOUNT_ID>:cluster/careroute-cluster-1
```

A "not found" error here just means it was already gone.

### 7. Check nothing is still billing

Each of these should print nothing (or `None`):

```powershell
aws eks list-clusters --region ap-southeast-1 --query "clusters[]" --output text
aws ec2 describe-nat-gateways --region ap-southeast-1 --filter Name=state,Values=pending,available --query "NatGateways[].NatGatewayId" --output text
aws ec2 describe-addresses --region ap-southeast-1 --query "Addresses[].PublicIp" --output text
aws elasticache describe-cache-clusters --region ap-southeast-1 --query "CacheClusters[].CacheClusterId" --output text
aws elbv2 describe-load-balancers --region ap-southeast-1 --query "LoadBalancers[].LoadBalancerName" --output text
```

## Run locally with kind

For local development without AWS costs, use the manifests in `k8s/local/`.
These use LocalStack to emulate S3, SQS and DynamoDB, and run Redis as a pod
instead of ElastiCache.

### Prerequisites

- Docker Desktop installed and running
- [kubectl](https://kubernetes.io/docs/tasks/tools/) and [kind](https://kind.sigs.k8s.io/docs/user/quick-start) installed
- A [LocalStack auth token](https://app.localstack.cloud/getting-started)

### Setup

```powershell
docker build -t cloud-fastapi:local .
docker build -t checkin-service:local services/checkin-service
docker build -t alert-service:local services/alert-service
docker build -t journey-service:local services/journey-service
docker build -t easyread-service:local services/easyread-service
docker build -t heartbeat-monitor:local services/heartbeat-monitor
kind create cluster --name csc3104-local --config k8s/local/kind-config.yaml
kind load docker-image cloud-fastapi:local checkin-service:local alert-service:local journey-service:local easyread-service:local heartbeat-monitor:local --name csc3104-local
```

`k8s/local` also runs Ollama (for the caregiver app's "Simplify with AI"); it downloads its model (~1 GB) when its
pod starts, and asks for 2 GB of memory.

Create `.env` in the project root with your LocalStack token (the `*.env`
pattern in `.gitignore` keeps it out of Git):

```dotenv
LOCALSTACK_AUTH_TOKEN=your-localstack-auth-token
```

### Deploy

```powershell
kubectl apply -f k8s/local/namespace.yaml
kubectl create secret generic localstack-auth --from-env-file=.env -n csc3104
kubectl apply -k k8s/local
kubectl get pods -n csc3104 -w
```

### Verify

In separate terminals:

```powershell
kubectl port-forward svc/cloud-fastapi 8000:8000 -n csc3104
```

```powershell
curl.exe http://localhost:8000/health
```

### Check-in analysis (Coordinator, Aggregator, detectors)

Not part of `kubectl apply -k k8s/local`, so teammates without these images aren't affected. With them, the
local cluster runs the whole chain: app -> Check-in Service -> Redis and SQS -> Coordinator -> detectors ->
Aggregator -> Alert Service -> caregiver app.

```powershell
docker build -t cloud-detectors:local services/detectors
docker build -t coordinator:local services/coordinator
docker build -t aggregator:local services/aggregator
kind load docker-image cloud-detectors:local coordinator:local aggregator:local --name csc3104-local
kubectl apply -k k8s/local/detectors
kubectl apply -k k8s/local/analysis
kubectl get pods -n csc3104 -w
```

With Podman instead of Docker, set `KIND_EXPERIMENTAL_PROVIDER=podman`, and load each image by its full name
(`podman tag localhost/coordinator:local docker.io/library/coordinator:local`, then
`kind load docker-image docker.io/library/coordinator:local --name csc3104-local`). Podman names a built image
`localhost/...`, which the pods' `coordinator:local` doesn't match, so they would fail with `ErrImageNeverPull`.

The Coordinator reads the `careroute-checkins` queue the Check-in Service creates on its start, so it reports
not ready until that pod is up.

### Reach the services from your machine

A cluster made with `k8s/local/kind-config.yaml` opens these on localhost once you apply `k8s/local/expose`, so no
`kubectl port-forward` is needed. A cluster made without the config can't; recreate it.

```powershell
kubectl apply -k k8s/local/expose
```

| Address | Service |
|---|---|
| <http://localhost:8001> | Check-in Service |
| <http://localhost:8002/dashboard> | Aggregator dashboard |
| <http://localhost:8005> | Alert Service |
| <http://localhost:8006> | Journey Service |
| <http://localhost:4566> | LocalStack: route pack and photo links, opened by the browser |

The caregiver app's dev server (`apps/caregiver`, `npm run dev`) and the traveller app's (`apps/traveller`,
`npm run dev`, port 5173) already call these addresses.

### Everything in one command, with a simulated trip

`local-sim` does all of the above: it creates the cluster with the config, builds and loads every image, makes the
LocalStack secret from `.env`, applies everything, waits until it's ready, starts the caregiver app and the traveller
app (<http://localhost:5173>, where the caregiver app's trip links lead) and can send a simulated trip through the
whole chain. Run it from the repo root.

| | Windows (PowerShell) | Mac or Linux |
|---|---|---|
| First time, and after changing service code | `.\scripts\windows\local-sim.ps1 -Setup` | `bash scripts/macos/local-sim.sh --setup` |
| Start everything and send a trip | `.\scripts\windows\local-sim.ps1 -Scenario off_route -Caregiver G-777` | `bash scripts/macos/local-sim.sh --scenario off_route --caregiver G-777` |
| Start everything with real logins (Cognito) | `.\scripts\windows\local-sim.ps1 -Cognito` | `bash scripts/macos/local-sim.sh --cognito` |

Then open <http://localhost:5174>, type the caregiver in **Act as** and open the traveller on the Dashboard. For a
real trip instead of a simulated one, make a trip in the caregiver app and open its link: the traveller app follows
it (`apps/traveller/README.md`, "With the whole team's system"). The script keeps the apps running until Ctrl+C; the cluster keeps running after that. `-Setup` on a cluster made
without the config deletes and recreates it, so its data is lost. Scenarios and the `live` command:
`services/detectors/README.md`, "Watch a scenario in the caregiver app".

### Stop

```powershell
kind delete cluster --name csc3104-local
```
