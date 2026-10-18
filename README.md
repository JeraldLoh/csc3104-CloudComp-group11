# csc3104-CloudComp-group11

## Run locally with Docker Compose (Onboarding)

The local stack consists of:

- **FastAPI** at `http://localhost:8000`
- **Redis** at `localhost:6379`
- **LocalStack** at `http://localhost:4566`, with S3 and SQS enabled

### Prerequisites

- Docker Desktop installed and running, with Docker Compose available.
- A LocalStack auth token. Obtain one from your LocalStack account.

### Configure the LocalStack token

From the project root, create a `.env` file next to `compose.yaml` containing:

```dotenv
LOCALSTACK_AUTH_TOKEN=your-localstack-auth-token
```

Replace the example value with your token. Compose will report an error if the token is
missing.

### Start the application

In PowerShell, open the project directory and run:

```powershell
docker compose up --build -d
```

The first run builds the FastAPI image and downloads the Redis and LocalStack
images. FastAPI waits for Redis and LocalStack to pass their healthchecks before
starting.

Check service status:

```powershell
docker compose ps
```

### Verify the services

Check the FastAPI health endpoint and root route:

```powershell
curl.exe http://localhost:8000/health
curl.exe http://localhost:8000/
```

The health endpoint should return `{"status":"ok"}`. Interactive API docs are
available at `http://localhost:8000/docs`.

Check Redis from inside its container:

```powershell
docker compose exec cloud-redis redis-cli ping
```

It should respond with `PONG`.

Check LocalStack from the host:

```powershell
curl.exe -f http://localhost:4566/_localstack/health
```

The endpoint should return an HTTP success response and a JSON health report.
You can also inspect startup logs for any service:

```powershell
docker compose logs -f cloud-fastapi
docker compose logs -f cloud-redis
docker compose logs -f cloud-localstack
```

Press `Ctrl+C` to stop following logs.

### Stop the application

Stop and remove the containers and Compose network:

```powershell
docker compose down
```

Redis data is stored in the `cloud-redis-data` named volume and remains when
containers are stopped. To also remove that volume and its data, run:

```powershell
docker compose down --volumes
```

## Run locally with Kubernetes (kind - k8s in docker for S3 & SQS testing before EKS deployment)

### Prerequisites

- Docker Desktop installed and running.
- `kubectl`(https://kubernetes.io/docs/tasks/tools/) and [kind](https://kind.sigs.k8s.io/docs/user/quick-start) installed.
- A LocalStack auth token. (https://app.localstack.cloud/getting-started)

Build the FastAPI image and create a local kind cluster. The cluster only needs
to be created once:

```powershell
docker build -t cloud-fastapi:local .
kind create cluster --name csc3104-local
kind load docker-image cloud-fastapi:local --name csc3104-local
```

Create `.env` in the project root with your LocalStack token. The `*.env`
pattern in `.gitignore` keeps this file out of Git:

```dotenv
LOCALSTACK_AUTH_TOKEN=your-localstack-auth-token
```

Create the namespace, then create the Kubernetes Secret directly from `.env`.
Do not put the token in a YAML manifest or commit the `.env` file:

```powershell
kubectl apply -f k8s/base/namespace.yaml
kubectl create secret generic localstack-auth --from-env-file=.env -n csc3104
kubectl apply -k k8s/base
```

The Kustomize base deploys FastAPI (2 replicas), Redis (with a 1 GiB
PersistentVolumeClaim), and LocalStack with S3 and SQS enabled. Wait for the
pods to become ready:

```powershell
kubectl get pods -n csc3104 -w
```

Press `Ctrl+C` after the pods are ready. In separate terminals, forward the
FastAPI and LocalStack services:

```powershell
kubectl port-forward svc/cloud-fastapi 8000:8000 -n csc3104
```

```powershell
kubectl port-forward svc/cloud-localstack 4566:4566 -n csc3104
```

Verify the API from another terminal:

```powershell
curl.exe http://localhost:8000/health
curl.exe http://localhost:8000/
```

Check Redis and LocalStack from their pods:

```powershell
kubectl exec -n csc3104 deploy/cloud-redis -- redis-cli ping
kubectl exec -n csc3104 deploy/cloud-localstack -- curl -s http://localhost:4566/_localstack/health
```

Redis should respond with `PONG`; LocalStack should return a JSON health
report. The LocalStack port-forward also makes its endpoint available at
`http://localhost:4566`.