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
| POST | `/api/checkins` | `checkin.json` | `202 {"accepted": 1, "duplicates": 0, "processed": 1}` |
| POST | `/api/checkins/batch` | `checkin-batch.json` | `202 {"accepted": 2, "duplicates": 0, "processed": 2}` |
| GET | `/metrics` | — | Prometheus metrics |

Duplicates (same `tripId` + `seq`) are accepted but ignored, so the app can safely retry.

`processed` (added, optional): how many of the request's check-ins, oldest first, were handled. A
busy server stops a batch after about 1 s; the sender removes those and sends the rest next.

`503` with `{"error": {"code": "overloaded"}}` and a `Retry-After` header (seconds): the replica is at
capacity and did nothing; retry after that long (plus random extra).

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
| GET | `/api/stream` | — | Live events (SSE), each one is `alert-event.json` or `arrival-event.json` |
| POST | `/api/arrived` | `{"tripId": "T-001", "travellerId": "U-042"}` | `{"tripId", "arrivedAt"}`: the traveller pressed "I've arrived". Safe to repeat; only the first call tells caregivers. No login needed until Cognito |
| POST | `/api/help` | `{"tripId": "T-001", "travellerId": "U-042", "stepIndex": 2, "lat": 1.40, "lng": 103.90}` | `{"incidentId": "..."}`: an urgent `help_requested` incident. Only `tripId` is required; the trip's check-in context fills in the rest. No login needed until Cognito |
| POST | `/api/travellers/{travellerId}/where-are-you` | — | `202` |
| POST | `/api/devices` | `{"token": "<firebase token>"}` | `201` |

## Profiles (Member 5)

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/travellers` | — | caregiver's linked travellers |
| GET | `/api/travellers/live` | — | `{"U-042": {"tripId", "stepIndex", "status", "lastSeenAt", "arrivedAt", "lat", "lng", "accuracyM", "batteryPct", "trail": [[lat, lng], ...]}}`: the caller's travellers with a check-in in the last 30 minutes |
| POST | `/api/travellers` | `{"name": "Alex", "preferences": {...}}` | `201` traveller (new `U-` id); the caller is linked to them |
| DELETE | `/api/travellers/{travellerId}` | — | `{"travellerId", "removed": true, "profileDeleted": bool}`: off the caller's list; profile and safe locations deleted if no caregiver is left |
| GET / POST | `/api/travellers/{travellerId}/safe-locations` | `{"name": "Home", "lat": ..., "lng": ...}` | safe locations / `201` the new one |
| GET | `/api/travellers/{travellerId}/caregivers` | — | list of caregiver IDs linked to the traveller, sorted; the caller must be one of them |
| POST | `/api/links` | `{"travellerId": "U-042", "caregiverId": "G-007"}` | `201` link: adds another caregiver to the traveller |
| GET | `/internal/links?caregiverId=G-003&travellerId=U-042` | — | `{"linked": true}`, for other services' access checks |

**Who sees what:** a caregiver only reaches travellers they're linked to (`caregiver-links`): profiles, safe
locations, the caregiver list, incidents, ack/resolve, and the events on `/api/stream`. Anyone else gets `404 not_found`, so they
can't tell the traveller exists. A link is made when a caregiver adds a traveller, or by a caregiver already
linked to them (`POST /api/links`), so nobody can attach themselves to a stranger. A traveller can read their
own safe locations.

**Login:** as for the Journey Service (`AUTH_MODE`): `X-Dev-User: G-003` locally, Cognito later. A browser's
`EventSource` can't send headers, so `/api/stream` also takes `?devUser=G-003` or `?token=<JWT>`.

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
| POST | `/internal/easyread` | `{"steps": [{"stepIndex": 0, "mode": "bus", "text": "<template wording>", "keep": ["45", "Opposite Fengshan Primary School", "5"]}], "maxChars": 300}` | `{"steps": [{"stepIndex": 0, "easyRead": "...", "fallback": false}], "provider": "ollama", "model": "qwen2.5:1.5b"}` |

Only route steps are sent. Never names, addresses or labels like "Home": a place without a stop code is sent as
`[place 1]`, `[place 2]`, ... and the Journey Service puts the name back.

- `text` is the Journey Service's own template wording for the step; it is also the fallback.
- `maxChars` (optional) is the longest wording a step may have: the Journey Service's `EASYREAD_MAX_CHARS`, the one
  setting for it. The caregiver app reads the same value from `GET /api/easy-read/limits` -> `{"maxChars": 300}`.
- `keep` lists what the AI must not lose (bus/line number, stop names, stop count, `[place N]` tokens).
- `fallback: true` means the AI's text failed the checks (dropped a `keep` item, added a number, too long) and `easyRead` is `text`.
- `provider: "template"` means Ollama was unavailable and every step is `text`. The endpoint still answers `200`.
- A step may also carry `spots`: `{"before_stop": [{"id": "L5", "name": "City Plaza", "kind": "mall", "metres": 60,
  "side": "left"}], ...}` (spots: `start`, `middle`, `end`, `before_stop`), the buildings along it. Names, kinds,
  sides and distances only, never coordinates, and none within 200 m of a place without a stop code. The answer's
  `landmarks` lists the ids of those its wording mentions (`[]` otherwise); every name is checked like the rest.
- `easyRead` (here and on route steps) stays one string, written as one short action per sentence ("Wait for bus
  number 45. Tap your card. Count 5 stops."). Apps may show each sentence as a bullet point.

The caregiver app reaches it through the Journey Service: `POST /api/routes/{routeId}/easy-read` (route owner only)
returns `{"steps": [{"stepIndex", "easyRead", "fallback", "landmarks": [{name, kind, lat, lng, seenFrom, photoKey, sameView, photoUrl}]}], "provider"}` and saves nothing; the caregiver checks
the wording in the editor and saves the route as usual.

Bus steps may also carry `stops` (every stop of the ride, as train steps do), `towards` (where the bus is going, as
on its front sign) and `every` ([low, high] minutes between buses in the daytime), all optional: the easy-read
wording uses them.

Route steps may carry `landmarks: [{name, kind, lat, lng, seenFrom, photoKey, sameView}]` (optional): what the wording tells
the traveller to look out for, each with a Street View photo of it taken from `seenFrom`, the traveller's spot on the
route (`sameView`: no photo of its own, as it would repeat the step's photo, `"step"`, or that landmark's). The
service keeps only those the step's `easyRead` names; answers add `photoUrl` to each, and the route pack
has `landmarks: [{name, photoUrl}]` per step.

Step photos (Journey Service): saving a route adds a Google Street View photo of where each walk/bus step ends
(`photoKey`, an S3 key under `media/`; a recent panorama, Google's own first, else a recent upload close by, else an older one under `media/streetview/v2/rough/` for the caregiver to check; a walk to a station shows its nearest exit). `POST /api/routes/{routeId}/photos` (multipart field `photo`, JPEG/PNG/WebP
up to 5 MB) stores the caregiver's own photo and returns `{"photoKey", "photoUrl"}` for the editor to save in a step.
Route answers and packs carry `photoUrl`, a temporary link.
