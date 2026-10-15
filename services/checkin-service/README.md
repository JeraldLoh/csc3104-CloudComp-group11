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

Errors use the contract format `{"error": {"code": ..., "message": ...}}`:
`422 invalid_request` (bad body, fix it, don't retry), `503 unavailable` (keep it in the outbox and retry),
`401 unauthorized` (only when `AUTH_REQUIRED=true`).

## Run it

**Docker Compose** (from the repo root, alongside Jerald's stack; adds DynamoDB to LocalStack):

```powershell
docker compose -f compose.yaml -f services/checkin-service/compose.checkin.yaml up --build -d
curl.exe http://localhost:8001/ready
```

**kind / Kubernetes:**

```powershell
docker build -t checkin-service:local services/checkin-service
kind load docker-image checkin-service:local --name csc3104-local
# add "- checkin-service.yaml" to k8s/base/kustomization.yaml, and "dynamodb" to SERVICES in k8s/base/localstack.yaml
kubectl apply -k k8s/base
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
and finishes steps 3 to 5, which are all safe to repeat. So no check-in is lost, and no one has to
repair anything by hand. The trade-off is that the Coordinator can occasionally receive a check-in
twice, which `contracts/queues.md` already tells it to ignore.

## What this service writes to Redis (for M1, M3, M4)

| Key | Notes |
|---|---|
| `trip:{tripId}:context` | `travellerId`, `stepIndex`, `routeVersion`, `status`. **Only ever moves forward**: a Lua script compares `seq` with `lastseq` atomically, so an older check-in (from an offline batch or a slower replica) can't overwrite newer context. Unknown values are stored as `""` (hashes can't hold null). |
| `trip:{tripId}:lastseq` | Highest `seq` applied to context. |
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
| `CORS_ORIGINS` | empty | web addresses allowed to call from a browser, comma-separated (e.g. the Amplify URL). Not needed locally: the app's dev server proxies `/api`. |

For real AWS, create the table and queues once: `python -m infra.create_resources`.

## Not done yet

- **Login (waits for issue 1.5).** Tokens aren't verified yet, and there's no check that the
  traveller owns the trip. `AUTH_REQUIRED=true` only checks that a Bearer token is present.
- **Metrics for 2.5.** Each response has an `X-Process-Time-Ms` header and each request is logged
  with its duration. Load tests can read these.
