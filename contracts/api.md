# API endpoints

**Rules for every service**
- Every service has `GET /health` → `200 {"status": "ok"}`
- `/api/...` endpoints are public (through the load balancer) and need a login token: `Authorization: Bearer <token>`
- `/internal/...` endpoints are only called by other services inside the cluster, never from the browser
- Errors always look like: `{"error": {"code": "not_found", "message": "Trip T-001 not found"}}`
- Inside the cluster, call services by name, e.g. `http://alert-service:8000`

---

## Heartbeat Monitor (Member 1)

No endpoints apart from `/health`. It runs on a timer and sends `problem-report-no-response.json` to the Alert Service.

## Check-in Service (Member 2)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/checkins` | `checkin.json` | `202 {"accepted": 1, "duplicates": 0}` |
| POST | `/api/checkins/batch` | `checkin-batch.json` | `202 {"accepted": 2, "duplicates": 0}` |

Duplicates (same `tripId` + `seq`) are accepted but ignored, so the app can safely retry.

## Coordinator and Aggregator (Member 3)

The Coordinator has no public endpoint; it reads from the `careroute-checkins` queue.

| Service | Method | Path | Body | Returns |
|---|---|---|---|---|
| Aggregator | POST | `/internal/votes` | `{"requestId": "...", "checkin": {...}, "votes": [detector-vote, ...]}` | `{"decision": "problem" \| "normal", "problemType": "...", "confidence": 0.81}` |
| Coordinator | GET | `/api/status/travellers` | — | `[{"travellerId": "U-042", "tripId": "T-001", "riskLevel": "elevated", "detectorsUsed": 4}]` |

## Detectors (Member 4)

Each detector is its own service: `detector-route`, `detector-heading`, `detector-dwell`, `detector-progress`.
(`detector-ml` / `ml-anomaly` is not planned; see `problem-definitions.md`.)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/internal/detect` | `detector-request.json` | `detector-vote.json` |

Must answer within **500 ms**. If a detector can't decide, it returns `"verdict": "abstain"`.

## Simulator (Member 4)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/sim/start` | `{"scenario": "wrong_direction", "travellers": 50}` | `{"runId": "..."}` |
| POST | `/api/sim/stop` | — | `200` |
| POST | `/api/sim/faults` | `{"detectorId": "detector-dwell", "mode": "wrong"}` | `200` |

Scenarios: `normal`, `missed_stop`, `wrong_direction`, `long_dwell`, `silence`, `tunnel`, `out_of_order`.


## Alert Service (Member 5)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/internal/problems` | `problem-report.json` or `problem-report-no-response.json` | `{"incidentId": "...", "created": true}` (`created: false` if it already existed) |
| GET | `/api/incidents?travellerId=U-042` | — | list of `incident.json` |
| POST | `/api/incidents/{incidentId}/ack` | — | updated `incident.json` |
| POST | `/api/incidents/{incidentId}/resolve` | — | updated `incident.json` |
| GET | `/api/stream` | — | Live events (SSE), each one is `alert-event.json` |
| POST | `/api/help` | `{"tripId": "T-001", "lat": 1.40, "lng": 103.90}` | `{"incidentId": "..."}` |
| POST | `/api/travellers/{travellerId}/where-are-you` | — | `202` |
| POST | `/api/devices` | `{"token": "<firebase token>"}` | `201` |

## Profiles (Member 5)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/travellers` | — | caregiver's linked travellers |
| POST | `/api/travellers` | `{"name": "Alex", "preferences": {...}}` | traveller |
| GET / POST | `/api/travellers/{travellerId}/safe-locations` | `{"name": "Home", "lat": ..., "lng": ...}` | safe locations |
| POST | `/api/links` | `{"travellerId": "U-042"}` | link caregiver to traveller |

## Journey Service (Member 6)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/routes/plan` | `{"from": {lat,lng}, "to": {lat,lng}}` | route options from OneMap |
| POST | `/api/routes` | route without `routeId` | `route.json` |
| GET / PUT | `/api/routes/{routeId}` | `route.json` | `route.json` (version +1 on every change) |
| POST | `/api/routes/{routeId}/approve` | — | `route.json` with `approved: true` |
| POST | `/api/trips` | `{"routeId": "R-007", "date": "2026-10-12"}` | `trip.json` |
| POST | `/api/trips/{tripId}/start` | — | `trip.json` (`status: active`) |
| POST | `/api/trips/{tripId}/end` | — | `trip.json` (`status: completed`) |
| GET | `/api/trips/{tripId}/pack` | — | `{"packUrl": "<temporary S3 link>", "packVersion": "R-007-v3"}` |

## Easy-read Service (Member 6)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/internal/easyread` | `{"steps": [{"stepIndex": 0, "instruction": "..."}]}` | `{"steps": [{"stepIndex": 0, "easyRead": "..."}], "provider": "groq"}` |

Only route steps are sent. Never names, addresses or labels like "Home".
