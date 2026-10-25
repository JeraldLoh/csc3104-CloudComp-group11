# Deploying the Journey Service to the platform's AWS stack

These files add our service to the stack the platform owner's scripts build (`scripts/windows/deploy.ps1` or
`scripts/macos/deploy.sh`, stack
`careroute-stack-1`, namespace `careroute`). They only add things; they never edit the platform's template, manifests or
scripts, and they only need what the stack already outputs.

> **Status: checked offline, never run against real AWS.** Deploying costs money and uses your AWS credentials, so
> nobody has done it yet. See "What has and hasn't been checked" before the first real run.

## Order

The scripts come in two sets that do the same thing, like the platform's `scripts/windows` and `scripts/macos`:
`windows/` (PowerShell) and `macos/` (bash; also works on Linux). Commands from the repo root:

| | Windows | macOS / Linux |
|---|---|---|
| 1. Platform (platform owner's script) | `.\scripts\windows\deploy.ps1 -BudgetEmail you@example.com` | `./scripts/macos/deploy.sh --email you@example.com` |
| 2. Journey Service on the cluster | `powershell -ExecutionPolicy Bypass -File services\journey-service\deploy\windows\deploy-eks.ps1` | `bash services/journey-service/deploy/macos/deploy-eks.sh` |
| 3. Disruption Lambda | `powershell -ExecutionPolicy Bypass -File services\journey-service\deploy\windows\deploy-lambda.ps1` | `bash services/journey-service/deploy/macos/deploy-lambda.sh` |

Tear down in **reverse**: `teardown-lambda`, then `teardown-eks` (both in `windows/` as `.ps1` and `macos/` as
`.sh`), then the platform's `teardown`. (A Lambda inside a VPC holds network interfaces in the platform's subnets and
security group; the platform's teardown can fail on them until AWS releases them, a few minutes after the function is
deleted. Re-run it if so.) The teardowns ask before deleting; `-Yes` / `--yes` skips the question.

The options have the same meaning in both sets: PowerShell `-Region`, `-StackName`, `-ResourcePrefix`, `-Bucket`, ...
are bash `--region`, `--stack-name`, `--resource-prefix`, `--bucket`, ... (`--help` lists them). Every script
defaults to region `ap-southeast-1`.

Your `.env` at the repo root needs `LTA_API_KEY` (and `ONEMAP_API_KEY` for planning). The scripts read them without
printing them.

## What each part does

| File | What it does |
|---|---|
| `eks/journey-service.yaml` | Deployment (2 replicas) and a ClusterIP Service in namespace `careroute`. Runs as the service account `cloud-fastapi`, the only one the platform's IAM role trusts. No AWS keys, no `AWS_ENDPOINT_URL`, no `CREATE_RESOURCES`. **No Ingress** (decided to leave exposure for later; use `kubectl port-forward svc/journey-service 8006:8000 -n careroute`). |
| `lambda/disruption-lambda.yaml` | Our own CloudFormation stack (`careroute-journey-lambda`): the function, its role (SQS send + VPC/logs), a 14-day log group, an EventBridge rule `rate(1 minute)`. |
| `lambda/make_zip.py` | Lists our 8 app files and writes the zip (run inside the build container). The zip goes to `lambda/dist/` (gitignored). |
| `windows/` and `macos/`, one script each (`.ps1` / `.sh`): | |
| `deploy-eks` | Reads the stack outputs, sets the pack bucket's CORS rule, creates the Secret `journey-api-keys` from `.env`, builds and pushes our image to the stack's ECR repository as tag `journey-<tag>`, fills in the manifest's four placeholders (image, Redis, bucket, prefix) in a temporary copy, applies it, waits for the rollout and checks `/ready` through a port-forward. `-RenderOnly` / `--render-only` just renders the manifest (no AWS). |
| `teardown-eks` | Deletes our Deployment, Service and Secret, our `journey-*` ECR tags and the `packs/` objects. Nothing of the platform's. |
| `build-lambda` | Builds `lambda/dist/disruption-lambda.zip` in a `python:3.12` Linux container: our 8 files plus httpx and redis (about 1 MB). `-Verify` / `--verify` imports it in a clean container and fails if FastAPI or PyJWT came along. |
| `deploy-lambda` | Reads the platform outputs, builds the zip (`build-lambda`), uploads it to the shared bucket (`lambda/disruption-checker-<hash>.zip`), deploys the stack, invokes the function once. |
| `teardown-lambda` | Deletes the Lambda stack and the uploaded zips. |
| `resource-names` | Shared by the others (dot-sourced / `source`d): finds the stack's resource prefix (warns if it isn't `careroute`) and derives the shared bucket name. |

What each set needs: Windows: `aws`, `kubectl`, Docker Desktop. macOS: the same plus `curl` and `shasum` (both come
with macOS). On Apple Silicon, Docker builds the `linux/amd64` image and zip by emulation, which is what Fargate and
the Lambda run.

## Names: the team prefix is `careroute`

The team decided on the resource prefix **`careroute`** (`careroute-routes`, `careroute-trips`, `careroute-disruptions`,
...), the same as the contract and this service's defaults, so locally and on AWS the names match:

| Setting | Locally (compose / LocalStack) | On the platform stack |
|---|---|---|
| `ROUTES_TABLE` | `careroute-routes` | `careroute-routes` |
| `TRIPS_TABLE` | `careroute-trips` | `careroute-trips` |
| `PACKS_BUCKET` | `careroute-packs` | `careroute-files-<account id>` (shared bucket; packs go under `packs/`) |
| `DISRUPTIONS_QUEUE` | `careroute-disruptions` | `careroute-disruptions` (the Lambda gets it from the stack's `DisruptionsQueueUrl` output) |

The platform stack builds every name from its `ResourcePrefix` parameter, and its IAM role only allows resources whose
names start with it. So our scripts do **not** hardcode a prefix: they read the stack's `ResourcePrefix` (override with
`-ResourcePrefix`), print it, fill it into the manifest's `<RESOURCE_PREFIX>` placeholder, derive the shared bucket as
`<prefix>-files-<account id>` (override with `-Bucket`), and `deploy-eks` checks that `<prefix>-routes` and
`<prefix>-trips` exist before applying anything. The stack's prefix defaults to `careroute`; if it is built with
anything else they print a warning but still work with it. The logic is in `windows/resource-names.ps1` and its bash
twin `macos/resource-names.sh`.

The pods' role only reaches buckets named `<ResourcePrefix>-*`, so the prefix and the bucket name must match. The
stack and namespace are parameters (`-StackName`, default `careroute-stack-1`; `-AppNamespace`, default `careroute`).

## How the stack's permissions shaped the design

- The pods' role allows only GetItem/PutItem/UpdateItem/DeleteItem/Query/Scan on the tables, object read/write and
  ListBucket on the bucket, and SQS send/receive. It cannot `DescribeTable`, set bucket CORS or create anything.
  So `/ready` checks the tables with a `GetItem` on a key that doesn't exist (and the bucket with `HeadBucket`), the
  service never creates resources on AWS, and the CORS rule is set by `deploy-eks.ps1` with **your** credentials.
- `/ready` failing now says which store and the AWS error, e.g. `routes table: AccessDeniedException`
  (a wrong name is `ResourceNotFoundException`, a missing bucket is `404`).
- Redis accepts connections only from the cluster's security group, so the Lambda is given that same group instead of
  asking for a change to the platform's template.

## What has and hasn't been checked

Checked (offline or locally): all unit tests (`pytest`); the PowerShell scripts parse and the bash scripts pass
`bash -n` (both only on Windows; the bash set has not been run on a Mac); both `build-lambda` scripts build and verify
the zip; both `deploy-eks` scripts render the same manifest; the manifest renders with no
placeholder left and is structurally consistent (labels, ports, probes, service account, env names the service really
reads); the Lambda template's references resolve; the Lambda zip builds in Linux, imports in a clean container with
no API packages, and **runs end to end** against local Redis and SQS with the real LTA key; the readiness check works
against LocalStack, fails with the right messages for a wrong table or bucket, and doesn't call `DescribeTable`.
(The automated tests were trimmed to the essentials and no longer cover the DynamoDB and S3 stores or `/ready`; those
were checked against LocalStack by hand, so re-check them after changing `route_store.py`, `trip_store.py`, `pack_store.py` or `/ready`.)

**Not checked**: any real AWS behaviour. A first real deploy should watch:
1. the pods use the service account `cloud-fastapi` and get AWS access (otherwise `/ready` says `AccessDeniedException`);
2. `/ready` returns 200 and the rollout finishes;
3. the Lambda's network interfaces reach Redis and LTA (private subnets through the NAT gateway) and a run returns
   `{"activeTrips": 0, ...}` on a normal day;
4. a message arrives on `careroute-disruptions` when a trip is active and LTA reports a disruption
   (`python -m app.demo_tools` only works with `DEMO_UI=true`: `kubectl set env deployment/journey-service -n careroute
   DEMO_UI=true`, then `kubectl exec ... python -m app.demo_tools on`; see `apps/caregiver/DEMO.md` section 2b);
5. a pack downloads from a browser through the signed link (the CORS rule);
6. `aws cloudformation validate-template` on `lambda/disruption-lambda.yaml`;
7. the LTA key is in the Lambda's environment variables (visible to anyone who can read its configuration): move it
   to Secrets Manager before this is more than a demo.

## Not covered here (needs the team)

Cognito logins (user pool and the token claim names; the service runs with `AUTH_MODE=off` until then), how our API is
exposed on the load balancer (no Ingress yet), whether M2's "pack builder" or our pack endpoint owns the route pack,
and folding these steps into the platform's own script.
