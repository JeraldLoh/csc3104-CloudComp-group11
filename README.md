# csc3104-CloudComp-group11

## Run locally with Docker Compose

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
