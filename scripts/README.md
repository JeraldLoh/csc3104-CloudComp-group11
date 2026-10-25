# Deploy and teardown scripts

These scripts create our AWS environment, deploy services onto it, and delete everything
afterwards so we stop paying.

**AWS charges about $0.30/hour while the environment exists, even if nothing is running.**
Always run teardown when you finish.

## The three commands

| What you want | Mac | Windows (PowerShell) |
|---|---|---|
| **1. Create the environment** (first time in a session) | `bash scripts/macos/deploy.sh --email you@gmail.com` | `.\scripts\windows\deploy.ps1 -BudgetEmail you@gmail.com` |
| **2. Deploy your service** (as often as you like) | `bash scripts/macos/deploy.sh --service coordinator` | `.\scripts\windows\deploy.ps1 -Service coordinator` |
| **3. Delete everything** (end of session) | `bash scripts/macos/teardown.sh` | `.\scripts\windows\teardown.ps1` |

Run them from the repo root. On Windows, if scripts are blocked, run them as
`powershell -ExecutionPolicy Bypass -File scripts\windows\deploy.ps1 ...`.

A typical session:

```
deploy.sh --email you@gmail.com     # ~25-35 min the first time. Go get a coffee.
deploy.sh --service coordinator     # ~2-3 min
  ...change code...
deploy.sh --service coordinator     # ~2-3 min, redeploys your new code
teardown.sh                         # ~15-25 min, back to $0
```

## One-time setup

1. Install the tools:
   - **Mac:** `brew install awscli kubectl helm eksctl`, plus [Docker Desktop](https://www.docker.com/products/docker-desktop/)
   - **Windows:** `winget install -e --id Amazon.AWSCLI`, `winget install -e --id Kubernetes.kubectl`,
     `winget install -e --id Helm.Helm`, `winget install -e --id eksctl.eksctl`, plus Docker Desktop
2. In the AWS console, create an IAM user for yourself with `AdministratorAccess` and create an
   access key for it. Don't use your root login.
3. Connect the CLI to your account:
   ```
   aws configure                  # paste the access key and secret; region: ap-southeast-1
   aws sts get-caller-identity    # should show YOUR account ID
   ```
4. Start Docker Desktop before running any script.

## Step 1: Create the environment

```
bash scripts/macos/deploy.sh --email you@gmail.com
```

This creates the VPC, EKS cluster, Redis, SQS queues, DynamoDB tables and load balancer, then
deploys the template app (`cloud-fastapi`) to check that everything works. It finishes when
`http://<load balancer>/health` answers.

- **Check the account ID** printed at the start. It's the account you're about to deploy into.
- **`--email` is optional** but use it the first time in each account. It sets up a warning email
  when spending reaches $64 and $80. AWS sends you **two confirmation emails: click both**, or the
  warnings won't arrive.
- If you run it again while the environment exists, it only updates what changed (a few minutes).

## Step 2: Deploy your service

`--service NAME` builds and deploys one service onto the environment from step 1. It doesn't
touch the infrastructure, so it's quick.

It expects two files, named after your service:

```
services/<name>/Dockerfile          your service's code and Dockerfile
k8s/eks/services/<name>.yaml        your Kubernetes manifest
```

The Deployment inside the manifest must be called `<name>` too. Use lowercase letters, digits and
dashes only (e.g. `coordinator`, `detector-route`).

**Don't paste AWS values into your manifest.** Write these placeholders instead; the script fills
them in from the environment (and never saves the filled-in version to Git):

| Placeholder | Becomes |
|---|---|
| `__IMAGE__` | Your image in ECR (repository `careroute-<name>`, created for you) |
| `__NAMESPACE__` | `careroute` |
| `__REGION__` | `ap-southeast-1` |
| `__REDIS_ENDPOINT__` | The Redis address |
| `__CHECKINS_QUEUE_URL__` | URL of the `careroute-checkins` queue |
| `__DISRUPTIONS_QUEUE_URL__` | URL of the `careroute-disruptions` queue |

Example `k8s/eks/services/coordinator.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: coordinator                  # must match --service
  namespace: __NAMESPACE__
spec:
  replicas: 1                        # use 2 for demos and failover tests
  selector:
    matchLabels: { app: coordinator }
  template:
    metadata:
      labels: { app: coordinator }
    spec:
      serviceAccountName: cloud-fastapi   # shared AWS permissions for now
      containers:
        - name: coordinator
          image: __IMAGE__
          ports: [{ containerPort: 8000 }]
          env:
            - { name: REDIS_URL, value: "redis://__REDIS_ENDPOINT__:6379/0" }
            - { name: CHECKINS_QUEUE_URL, value: "__CHECKINS_QUEUE_URL__" }
            - { name: AWS_DEFAULT_REGION, value: "__REGION__" }
          resources:
            requests: { cpu: 250m, memory: 512Mi }
            limits: { cpu: 250m, memory: 512Mi }
          readinessProbe:
            httpGet: { path: /health, port: 8000 }
---
apiVersion: v1
kind: Service
metadata:
  name: coordinator                  # other services call http://coordinator:8000
  namespace: __NAMESPACE__
spec:
  selector: { app: coordinator }
  ports: [{ port: 8000, targetPort: 8000 }]
```

Useful commands afterwards:

```
kubectl get pods -n careroute                    # is it running?
kubectl logs -n careroute deploy/coordinator -f  # see its logs
kubectl delete deployment,service coordinator -n careroute   # remove one service
```

## Step 3: Delete everything

```
bash scripts/macos/teardown.sh
```

It asks you to type the stack name (`careroute-stack-1`) to confirm, then deletes all services,
the cluster, Redis, queues, tables, the S3 bucket and the ECR images. At the end it lists anything
still costing money in the region; there should be nothing.

**Teardown deletes all data.** Download anything you need first (experiment results, logs,
DynamoDB data). It can't be recovered.

## Shared account rules

If several people use the same AWS account:

- Post in the group chat **before** creating or tearing down the environment.
- Only tear down after everyone using it has said they're done.
- Use your own account for testing your own service, and the shared one for integration tests,
  the experiment and the demo.

## Common problems

| Problem | Fix |
|---|---|
| `'aws' is not installed` (or kubectl, helm, eksctl) | Install it (see one-time setup) and open a new terminal |
| `AWS credentials are not configured` | Run `aws configure` |
| `Docker is not running` | Start Docker Desktop and wait until it's ready |
| `Stack careroute-stack-1 is NONE. Run this script without --service first` | The environment doesn't exist yet: run step 1 |
| `No Dockerfile at services/<name>/Dockerfile` or `No manifest at ...` | Check the folder and file names match `--service` exactly |
| Pods stuck in `Pending` | Fargate pods take 1-2 minutes to start. If it's longer, check the manifest's namespace is `__NAMESPACE__` |
| Pods in `CrashLoopBackOff` | Your app is crashing: `kubectl logs -n careroute deploy/<name>` |
| The first deploy failed half-way | Just run it again; it carries on from where it stopped |
| Not sure if you're still being charged | Run teardown; it's safe to run even if things are already deleted |
