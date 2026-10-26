# Check-in Service (Member 2, issue 2.3)

Receives check-ins from the traveller app, stores them, updates the live Redis keys that
the Detectors, Coordinator and Heartbeat Monitor read, and queues each check-in for the
Coordinator. Matches `contracts/` exactly.

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/checkins` | `checkin.json` | `202 {"accepted": 1, "duplicates": 0}` |
| POST | `/api/checkins/batch` | `checkin-batch.json` | `202 {"accepted": n, "duplicates": m}` |
| GET | `/health` | | `200 {"status": "ok"}` (liveness) |
| GET | `/ready` | | `200` if Redis, DynamoDB and SQS are reachable, else `503` (readiness) |
| GET | `/metrics` | | Prometheus metrics (see below) |

The batch answer also says how many check-ins it handled, oldest first:
`{"accepted": 18, "duplicates": 2, "processed": 20}` (see "Time-limited batches").

Errors use the contract format `{"error": {"code": ..., "message": ...}}`:
`422 invalid_request` (bad body, fix it, don't retry), `503 unavailable` (keep it in the outbox and retry),
`503 overloaded` with a `Retry-After` header (this replica is at capacity; retry after that many
seconds), `401 unauthorized` (only when `AUTH_REQUIRED=true`).

## Run it

**Docker Compose** (from the repo root). `compose.checkin.yaml` brings its own Redis and
LocalStack, and reads `LOCALSTACK_AUTH_TOKEN` from the `.env` file at the repo root:

```powershell
$env:COMPOSE_FILE = "services/checkin-service/compose.checkin.yaml"
docker compose up -d --build
curl.exe http://localhost:8001/ready
```

Set `COMPOSE_FILE` once per PowerShell window; then `docker compose stop checkin-service`,
`docker compose logs checkin-service`, `docker compose down` and so on all use this file.

**kind (local Kubernetes):** `k8s/local/checkin-service.yaml` is already listed in
`k8s/local/kustomization.yaml`. Follow the kind steps in the root README, plus:

```powershell
docker build -t checkin-service:local services/checkin-service
kind load docker-image checkin-service:local --name csc3104-local
kubectl apply -k k8s/local
kubectl port-forward svc/checkin-service 8001:8000 -n csc3104
```

**Send a test check-in** (PowerShell):

```powershell
$now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$body = @{tripId="T-001"; travellerId="U-042"; seq=1; timestamp=$now; lat=1.4052; lng=103.9024;
          accuracyM=12; speedMps=1.3; headingDeg=85; stepIndex=2; routeVersion=3; batteryPct=64;
          status="active"; bufferedOffline=$false} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://localhost:8001/api/checkins -ContentType "application/json" -Body $body
```

## Tests

```powershell
cd services/checkin-service
pip install -r requirements-dev.txt
pytest
```

No Docker needed: moto fakes DynamoDB and SQS, fakeredis fakes Redis. To check the Lua scripts
on a real Redis, set `TEST_REDIS_URL=redis://localhost:6379/15` (the tests wipe that database).

## How a check-in is processed

1. **DynamoDB conditional put** on `tripId` + `seq`. This one atomic step decides new vs duplicate,
   even when two replicas get the same retry at the same moment.
2. **Heartbeat** (`hb:*` keys), live check-ins only.
3. **Context and recent list** (`trip:*` keys). Done *before* the queue, so when the Coordinator
   takes the message, the detectors can already see this check-in in Redis.
4. **SQS send** to `careroute-checkins`.
5. **Mark `enqueued = true`** on the DynamoDB item.

If any step fails, the app gets `503` and retries. The retry finds the item with `enqueued = false`
and finishes steps 3 to 5, which are all safe to repeat. The trade-off is that the Coordinator can
occasionally receive a check-in twice, which `contracts/queues.md` already tells it to ignore.

If the phone never retries (battery died), the **background clean-up task** below finishes it.
Either way, no check-in that reached the server is lost, and nobody repairs anything by hand.

## Background clean-up task (leader-elected)

Every 15 s, one replica looks for check-ins stored more than 30 s ago that never reached the queue,
and finishes them (`app/reconciler.py`).

- **Finding them cheaply.** Unfinished items carry `pending = "P"`; marking one as queued removes it.
  The DynamoDB index `pending-index` (on `pending` + `receivedAt`) contains only items that have
  that attribute (a *sparse index*), so it stays tiny however large the table grows, and one query
  lists the stuck ones. The index lags the table slightly, so each item is re-read before acting.
- **One replica at a time: a leader lease in Redis.** Each round a replica takes or renews the key
  `lock:checkin-reconciler` (`SET NX PX 45000` to take it; a Lua compare-and-renew to keep it). The
  holder is the leader. If it crashes, the lease expires and another replica takes over within about
  45 s + one round; a normal shutdown deletes the key (Lua compare-and-delete) so the next replica
  takes over at once.
- **The lease is for efficiency, not correctness.** No lease can promise two leaders never overlap
  (a leader frozen past its lease wakes up still believing it leads). Rather than depend on that,
  finishing a check-in is idempotent: the Redis update only moves forward, marking it queued can be
  repeated, and the Coordinator ignores a repeated tripId + seq. Two leaders at once cost at most a
  duplicate queue message (Kleppmann, *How to do distributed locking*, 2016).
- **The grace period** (30 s) leaves in-flight requests and quick phone retries to finish their own work.

## Overload protection

Each replica handles at most `MAX_IN_FLIGHT` (32) `/api` requests at once. Beyond that it answers
`503 overloaded` with `Retry-After: 2` straight away, instead of queueing requests until the phone
gives up (which would make the server do work nobody is waiting for). The app keeps the check-in,
waits at least `Retry-After` plus a random extra, and retries. Shed requests were never stored, so
the retry is a normal first attempt.

## Time-limited batches

A batch stops starting new check-ins after `BATCH_BUDGET_MS` (1 s), always doing at least one, and
reports `processed`; the app removes exactly those and sends the rest next.

This was found by the evaluation (`evaluation/RESULTS.md`, E1): after a 5-minute outage every phone
sends its whole backlog at once. Without a limit, a busy server took longer than the phone's
timeout for those big batches; the phones resent the same batches while the server was still
working on the abandoned ones, so the server stayed busy but **no phone got an answer** and the
outboxes never emptied, a retry storm that sustains itself. With the limit, every request answers
within about a second and always makes progress.

**Heartbeats and batches.** Only single, live check-ins add to `hb:{tripId}:intervals`. A batch means
the phone was behind; it may also contain fresh check-ins made while it waited, all arriving at the
same instant. Counting those would record 0 ms gaps and distort the failure detector's history, so
a batch only refreshes `hb:{tripId}:lastseen`.

## Metrics (`GET /metrics`, Prometheus format)

| Metric | |
|---|---|
| `checkin_checkins_total{outcome}` | `accepted`, `recovered` (finished by a retry), `duplicate` |
| `checkin_delivery_lag_seconds{kind}` | phone timestamp to server arrival; `live` or `buffered` |
| `checkin_http_requests_total{path,status}`, `checkin_http_request_seconds{path}` | requests and their latency |
| `checkin_in_flight_requests`, `checkin_shed_total` | load and requests turned away with 503 |
| `checkin_reconciled_total`, `checkin_reconciler_leader` | clean-up work, and whether this replica leads |

## What this service writes to Redis (for M1, M3, M4)

| Key | Notes |
|---|---|
| `trip:{tripId}:context` | `travellerId`, `stepIndex`, `routeVersion`, `status`. **Only ever moves forward**: a Lua script compares `seq` with `lastseq` atomically, so an older check-in (from an offline batch or a slower replica) can't overwrite newer context. Unknown values are stored as `""` (hashes can't hold null). |
| `trip:{tripId}:lastseq` | Highest `seq` applied to context. |
| `lock:checkin-reconciler` | Leader lease for the clean-up task (holder = pod name). New key, internal to this service. |
| `trip:{tripId}:recent` | Last 10 check-ins as JSON, **sorted by `seq`, newest first**, even when they arrive out of order. No duplicates. |
| `hb:{tripId}:lastseen` | When the last check-in **arrived at the server** (ISO 8601 UTC with ms, e.g. `2026-10-16T08:15:30.123Z`). Server time, not the phone's, so a wrong phone clock can't fool the heartbeat. |
| `hb:{tripId}:intervals` | Gaps between **live** arrivals in ms, newest first, max 100. |
| `hb:{tripId}:status` | `active` or `backgrounded`, from the latest live check-in. |

All keys expire 24 h after their last update.

**Heartbeat rule (M1, please check this fits the phi accrual detector):** check-ins with
`bufferedOffline: true` don't add to `intervals`. They have old timestamps and arrive in a burst,
so they'd distort the gap distribution. When a buffered batch arrives, `lastseen` is refreshed
(the phone is clearly back) but the offline gap is not recorded as a normal interval.

## Configuration

| Variable | Default | |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `AWS_ENDPOINT_URL` | unset | LocalStack URL locally; leave unset on AWS |
| `AWS_DEFAULT_REGION` | `ap-southeast-1` | |
| `CHECKINS_TABLE` / `CHECKINS_QUEUE` | `careroute-checkins` | |
| `CHECKIN_TTL_DAYS` | `7` | DynamoDB TTL, counted from arrival time |
| `TRIP_KEY_TTL_S` | `86400` | Redis key expiry |
| `MAX_BATCH` | `500` | |
| `MAX_FUTURE_SKEW_S` | `300` | reject timestamps further in the future than this |
| `CREATE_RESOURCES` | `false` | create the table and queues on startup (local only) |
| `AUTH_REQUIRED` | `false` | require a Bearer token |
| `MAX_IN_FLIGHT` | `32` | requests handled at once per replica before shedding with 503 |
| `RETRY_AFTER_S` | `2` | `Retry-After` sent when shedding |
| `BATCH_BUDGET_MS` | `1000` | time limit for one batch request |
| `RECONCILER_ENABLED` | `true` | run the clean-up task |
| `RECONCILE_INTERVAL_S` / `RECONCILE_GRACE_S` / `RECONCILE_BATCH` | `15` / `30` / `100` | how often, how old, how many per query |
| `LEADER_LEASE_MS` | `45000` | lease length; takeover after a crash within about this + one interval |
| `LOG_LEVEL` | `INFO` | |
| `CORS_ORIGINS` | empty | web addresses allowed to call from a browser, comma-separated (e.g. the Amplify URL). Not needed locally: the app's dev server proxies `/api`. |

Locally the table and queues are created on startup (`CREATE_RESOURCES=true`). On AWS they
come from the team's platform stack (see "Deploy to AWS" below).

## Deploy to AWS (EKS)

Uses the team platform and scripts (`scripts/README.md`). The platform stack creates the
`careroute-checkins` table (with `pending-index`) and queue, the Redis cluster, and the shared
service account `cloud-fastapi` whose role the pods use.

```powershell
# 1. Create the environment (once per session, ~25-35 min; tear down when finished)
.\scripts\windows\deploy.ps1 -BudgetEmail you@gmail.com
# 2. Build, push and deploy this service (~2-3 min; run again after code changes)
.\scripts\windows\deploy.ps1 -Service checkin-service
# 3. Check
kubectl get pods -n careroute -l app=checkin-service     # 2 pods 1/1 Running
kubectl get ingress -n careroute                         # the shared ALB address
curl.exe -X POST http://<ALB_ADDRESS>/api/checkins -H "Content-Type: application/json" -d "{}"
# 4. Delete everything (the cluster, NAT gateway, ALB and Redis cost money every hour)
.\scripts\windows\teardown.ps1
```

The `curl` should return `422 invalid_request`, which proves the request reached this service
through the shared load balancer. The manifest is `k8s/eks/services/checkin-service.yaml`; the
script fills in its `__PLACEHOLDERS__`.

`infra/checkin-stack.yaml` is a standalone alternative for deploying this service without the team
platform: its own table, queue and a least-privilege role for this service only. Don't deploy it
next to the platform stack: both create `careroute-checkins`.

## Not done yet

- **Login (waits for issue 1.5).** Tokens aren't verified yet, and there's no check that the
  traveller owns the trip. `AUTH_REQUIRED=true` only checks that a Bearer token is present.
- **Throughput on AWS.** The evaluation runs against a local AWS emulator (about 45 check-ins/s),
  so it measures correctness and behaviour under failure, not AWS throughput. Measure that on EKS.
