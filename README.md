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

## Deploy the infrastructure

### 1. Deploy the CloudFormation stack

Open the AWS Console, go to CloudFormation, and create a stack from
`cloudformation/careroute-vpc-eks-standard.yaml`. Set `ClusterName` to `careroute-cluster-1`,
`ComputeMode` to `fargate`, `AppPort` to `8000`, and `BudgetAlertEmail` to your
email address (this creates a monthly cost budget with alerts at 80% and 100% of
$80 USD — confirm the subscription email AWS sends after stack creation). Tick
the IAM acknowledgement box.

Alternatively, deploy from the CLI:

```powershell
aws cloudformation create-stack `
  --stack-name careroute-stack-1 `
  --template-body file://cloudformation/careroute-vpc-eks-standard.yaml `
  --parameters ParameterKey=ClusterName,ParameterValue=careroute-cluster-1 `
               ParameterKey=ComputeMode,ParameterValue=fargate `
               ParameterKey=AppPort,ParameterValue=8000 `
               ParameterKey=BudgetAlertEmail,ParameterValue=your-email@example.com `
  --capabilities CAPABILITY_IAM `
  --region ap-southeast-1
```

Wait for the stack to reach `CREATE_COMPLETE` (15–25 minutes):

```powershell
aws cloudformation wait stack-create-complete --stack-name careroute-stack-1 --region ap-southeast-1
```

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

Look up the current chart and app version:

```powershell
helm repo add eks https://aws.github.io/eks-charts
helm repo update eks
helm search repo eks/aws-load-balancer-controller
```

Download the IAM policy matching the app version shown above:

```powershell
curl.exe -o iam_policy.json https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/<APP_VERSION>/docs/install/iam_policy.json
aws iam create-policy `
  --policy-name AWSLoadBalancerControllerIAMPolicy `
  --policy-document file://iam_policy.json
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

Install the Helm chart with the `VpcId` output from step 2:

```powershell
helm install aws-load-balancer-controller eks/aws-load-balancer-controller `
  -n kube-system `
  --set clusterName=careroute-cluster-1 `
  --set serviceAccount.create=false `
  --set serviceAccount.name=aws-load-balancer-controller `
  --set region=ap-southeast-1 `
  --set vpcId=<VPC_ID> `
  --version <CHART_VERSION>
```

Verify the controller is running (should show `2/2` ready after 1–2 minutes):

```powershell
kubectl get deployment -n kube-system aws-load-balancer-controller
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

Delete resources in this order to avoid orphaned infrastructure:

```powershell
# 1. Remove Kubernetes resources (deletes the ALB)
kubectl delete -k k8s/eks

# 2. Uninstall the load balancer controller
helm uninstall aws-load-balancer-controller -n kube-system
eksctl delete iamserviceaccount --cluster careroute-cluster-1 --namespace kube-system --name aws-load-balancer-controller --region ap-southeast-1

# 3. Wait a few minutes for the ALB and its network interfaces to be deleted

# 4. Delete the CloudFormation stack (removes VPC, EKS, Redis, SQS, DynamoDB, IAM roles, ECR)
aws cloudformation delete-stack --stack-name careroute-stack-1 --region ap-southeast-1
aws cloudformation wait stack-delete-complete --stack-name careroute-stack-1 --region ap-southeast-1

# 5. Clean up resources created outside the stack
aws s3 rb s3://careroute-files-<ACCOUNT_ID> --force --region ap-southeast-1
aws iam delete-policy --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/AWSLoadBalancerControllerIAMPolicy
```

Verify nothing is still billing by checking the EC2, EKS, ElastiCache, NAT
gateways, and Elastic IPs pages in the `ap-southeast-1` region.

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
kind create cluster --name csc3104-local --config k8s/local/kind-config.yaml
kind load docker-image cloud-fastapi:local checkin-service:local alert-service:local journey-service:local easyread-service:local --name csc3104-local
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

The caregiver app's dev server (`apps/caregiver`, `npm run dev`) already calls these addresses.

### Everything in one command, with a simulated trip

`local-sim` does all of the above: it creates the cluster with the config, builds and loads every image, makes the
LocalStack secret from `.env`, applies everything, waits until it's ready, starts the caregiver app and can send a
simulated trip through the whole chain. Run it from the repo root.

| | Windows (PowerShell) | Mac or Linux |
|---|---|---|
| First time, and after changing service code | `.\scripts\windows\local-sim.ps1 -Setup` | `bash scripts/macos/local-sim.sh --setup` |
| Start everything and send a trip | `.\scripts\windows\local-sim.ps1 -Scenario off_route -Caregiver G-777` | `bash scripts/macos/local-sim.sh --scenario off_route --caregiver G-777` |

Then open <http://localhost:5174>, type the caregiver in **Act as** and open the traveller on the Dashboard. The
script keeps the app running until Ctrl+C; the cluster keeps running after that. `-Setup` on a cluster made
without the config deletes and recreates it, so its data is lost. Scenarios and the `live` command:
`services/detectors/README.md`, "Watch a scenario in the caregiver app".

### Stop

```powershell
kind delete cluster --name csc3104-local
```
