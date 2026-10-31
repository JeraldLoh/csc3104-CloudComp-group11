# Alert Service (Member 5)

Turns detected problems into **incidents** for caregivers: one incident per problem, however many
times it's reported, with the state `open` → `acknowledged` → `resolved`.

Inside the cluster: `http://alert-service:8000`.

## What it receives

| From | How | Body |
|---|---|---|
| Aggregator (M3) | `POST /internal/problems` | `contracts/examples/problem-report.json` |
| Heartbeat Monitor (M1) | `POST /internal/problems` | `contracts/examples/problem-report-no-response.json` |
| Disruption Lambda (M6) | SQS queue `careroute-disruptions` | `contracts/examples/disruption.json` |

`POST /internal/problems` returns `{"incidentId": "...", "created": true}`, or `"created": false` if
that problem already has an incident. **Retrying is safe**: send again if a request times out.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | Process is alive |
| GET | `/ready` | Redis and DynamoDB reachable |
| POST | `/internal/problems` | See above |
| POST | `/api/help` | The traveller pressed Help: an urgent incident. No login until Cognito, because a refused request for help is worse than a false alarm |
| POST | `/api/arrived` | The traveller pressed "I've arrived": tells their caregivers once. Not an incident |
| GET | `/api/incidents?travellerId=U-042` | Newest first |
| POST | `/api/incidents/{incidentId}/ack` | URL-encode the ID: `U-042%23T-001%23off_route%232` |
| POST | `/api/incidents/{incidentId}/resolve` | |
| GET | `/api/stream` | Live alerts (SSE) for the caller's travellers |
| GET / POST | `/api/travellers` | The caller's travellers / add one (you're linked to them) |
| GET | `/api/travellers/live` | Their current step and location, from the Check-in Service's Redis keys (`app/live.py`) |
| GET | `/api/travellers/{travellerId}/caregivers` | Who looks after them (you must be linked already) |
| POST | `/api/links` | `{"travellerId", "caregiverId"}`: add another caregiver (you must be linked already) |
| GET / POST | `/api/travellers/{travellerId}/safe-locations` | Home, School… |
| GET | `/internal/links?caregiverId=&travellerId=` | `{"linked": true/false}` for other services |

## Who sees what

Same login as the Journey Service, chosen with `AUTH_MODE`:

| `AUTH_MODE` | Caller is | Use |
|---|---|---|
| `off` (default) | nobody; nothing is checked (profiles still need a caregiver) | AWS until Cognito exists |
| `dev` | whoever the `X-Dev-User: G-003` header says (`?devUser=` on `/api/stream`) | local only (`dev.ps1`, kind) |
| `cognito` | verified from `Authorization: Bearer <JWT>` (`?token=` on `/api/stream`) | after issue 1.5 |

With a login, a caregiver only reaches travellers linked to them in `caregiver-links`: their profiles,
safe locations, incidents and live alerts. Anyone else gets `404 not_found`.

## How duplicates are prevented

The incident ID is `travellerId#tripId#problemType#stepIndex`. The first report creates the incident
with a DynamoDB conditional write (`attribute_not_exists(incidentId)`), so when several replicas,
retries or re-delivered queue messages handle the same problem at once, exactly one write succeeds and
the rest add to `detectionCount`. A problem detected again after the incident was resolved reopens it;
a late copy of a report from before the resolve doesn't.

Queue messages are deleted only after the incident is saved. If a replica crashes mid-way, SQS offers
the message again after 60 s; after 5 failed attempts it moves to `careroute-disruptions-dlq`.

## Run locally

Needs the local cluster from the repo README, and both port-forwards (Redis 6379, LocalStack 4566).

```powershell
cd services\alert-service
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
.\dev.ps1                        # http://127.0.0.1:8005/docs
```

```powershell
pytest -v                                  # tests, against LocalStack
python tools\send_disruption.py U-042      # put the example disruption on the queue
```

In the kind cluster (2 replicas). `k8s/local/alert-service.yaml` is in `kustomization.yaml`, so
`kubectl apply -k k8s/local` runs it once the image is loaded:

```powershell
docker build --provenance=false -t alert-service:local services/alert-service
kind load docker-image alert-service:local --name csc3104-local
kubectl apply -k k8s/local
```

After changing the code, build and load again, then restart. The tag stays the same, so applying doesn't
replace the pods:

```powershell
kubectl rollout restart deployment/alert-service -n csc3104
```

## Deploy to AWS

With the environment created (`scripts/README.md`, step 1):

```powershell
.\scripts\windows\deploy.ps1 -Service alert-service
```

Uses `services/alert-service/Dockerfile` and `k8s/eks/services/alert-service.yaml`.
