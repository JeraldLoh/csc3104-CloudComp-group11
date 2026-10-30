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

Each detector is its own service: `detector-route`, `detector-heading`, `detector-dwell`, `detector-ml`.

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
| POST | `/internal/easyread` | `{"steps": [{"stepIndex": 0, "mode": "bus", "text": "<template wording>", "keep": ["45", "Opposite Fengshan Primary School", "5"]}]}` | `{"steps": [{"stepIndex": 0, "easyRead": "...", "fallback": false}], "provider": "ollama", "model": "qwen2.5:1.5b"}` |

Only route steps are sent. Never names, addresses or labels like "Home": a place without a stop code is sent as
`[place 1]`, `[place 2]`, ... and the Journey Service puts the name back.

- `text` is the Journey Service's own template wording for the step; it is also the fallback.
- `keep` lists what the AI must not lose (bus/line number, stop names, stop count, `[place N]` tokens).
- `fallback: true` means the AI's text failed the checks (dropped a `keep` item, added a number, too long) and `easyRead` is `text`.
- `provider: "template"` means Ollama was unavailable and every step is `text`. The endpoint still answers `200`.
- `easyRead` (here and on route steps) stays one string, written as one short action per sentence ("Wait for bus
  number 45. Tap your card. Count 5 stops."). Apps may show each sentence as a bullet point.

The caregiver app reaches it through the Journey Service: `POST /api/routes/{routeId}/easy-read` (route owner only)
returns `{"steps": [{"stepIndex", "easyRead", "fallback"}], "provider"}` and saves nothing; the caregiver checks
the wording in the editor and saves the route as usual.

Step photos (Journey Service): saving a route adds a Google Street View photo of where each walk/bus step ends
(`photoKey`, an S3 key under `media/`; a recent panorama, Google's own first, else a recent upload close by, else an older one under `media/streetview/v2/rough/` for the caregiver to check; a walk to a station shows its nearest exit). `POST /api/routes/{routeId}/photos` (multipart field `photo`, JPEG/PNG/WebP
up to 5 MB) stores the caregiver's own photo and returns `{"photoKey", "photoUrl"}` for the editor to save in a step.
Route answers and packs carry `photoUrl`, a temporary link.
