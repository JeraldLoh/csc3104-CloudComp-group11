# Journey Service

Plans public-transport routes (OneMap, checked against LTA DataMall's live bus, train and traffic data), and owns
saved routes, trips and the traveller's offline route pack. During an MRT breakdown it finds LTA's free bridging buses
for trips under way and lets the caregiver re-route them.

- **How it works, and why:** [DESIGN.md](DESIGN.md) (read it before changing the service).
- **Demo script** with every step: [apps/caregiver/DEMO.md](../../apps/caregiver/DEMO.md).
- **API contract:** [contracts/api.md](../../contracts/api.md).

**Status:** proof of concept, working locally against LocalStack (DynamoDB, S3, SQS) and Redis. Spoken instructions
(Amazon Polly) are on for AWS only and not yet tried against real Polly. Not built: the Profiles lookups (traveller and
caregiver names, and checking a caregiver is linked to the traveller).

## Run it

Commands are PowerShell (Windows) first, then bash (macOS/Linux) where they differ. Blocks with only `docker`,
`kubectl`, `pip`, `pytest` and `cd` lines are the same in both. Run them from the repo root.

**Data:** the service answers from **saved API responses** by default (`app/saved_data/`, in Git), so it needs no keys,
internet or OneMap token. To refresh them from the real APIs (needs `LTA_API_KEY` and the OneMap account in `.env`):

```powershell
python services/journey-service/scripts/capture_data.py --target both
```

```bash
python3 services/journey-service/scripts/capture_data.py --target both
```

Switch to live data with the caregiver app's **Data** box (no restart). AWS always runs on live data.

**Start it** with Docker Compose (needs `LOCALSTACK_AUTH_TOKEN` in `.env`; it starts its own Redis and LocalStack, so
stop `services/detectors/compose.yaml` first, as both use port 6379):

```powershell
docker compose --env-file .env -f services/journey-service/compose.journey.yaml up --build -d
curl.exe http://localhost:8006/health
```

```bash
docker compose --env-file .env -f services/journey-service/compose.journey.yaml up --build -d
curl http://localhost:8006/health
```

**Tests** (no Docker, network or keys):

```powershell
cd services/journey-service
pip install -r requirements-dev.txt
pytest
```

Each area has its own file in `tests/` (`test_planning.py`, `test_routes_trips.py`, `test_bridge.py`, ...), so you can
run just the one you changed.

**kind:**

```powershell
docker build -t journey-service:local services/journey-service
kind load docker-image journey-service:local --name csc3104-local
kubectl create secret generic journey-api-keys --from-env-file=.env -n csc3104
kubectl apply -k k8s/local
kubectl port-forward svc/journey-service 8006:8000 -n csc3104
```

**AWS:** see `deploy/README.md`.

## Code

| Where | What |
|---|---|
| `app/main.py`, `models.py`, `auth.py` | the FastAPI endpoints, request bodies, who is calling |
| `app/journey_client.py` | what the API calls: saved routes, trips and packs, using the parts below |
| `app/onemap.py`, `app/planning.py` | OneMap client; route options with their live LTA warnings |
| `app/photos.py`, `app/trip_changes.py` | step photos and their links; bridging buses and re-routes for a trip under way |
| `app/lta.py`, `app/bridge.py` | LTA DataMall client; working out a bridging bus past a breakdown |
| `app/easy_read.py`, `easy_read_ai.py`, `landmarks.py`, `streetview.py` | the traveller's wording, AI wording, landmarks, finding a Street View photo |
| `app/speech.py` | spoken instructions: each step read aloud by Amazon Polly, for the pack's `audioUrl` |
| `app/route_store.py`, `trip_store.py`, `pack_store.py` | storage: DynamoDB or Redis, S3 |
| `app/disruption.py`, `lambda_handler.py` | the disruption checker (a Lambda; it imports none of the API code) |
| `app/demo_tools.py` | pretend disruptions and crowding for demos (`DEMO_UI=true` only) |
| `app/replay.py`, `app/saved_data/` | saved-data mode |

## Endpoints

Errors are `{"error":{"code","message"}}`. Who may call what: see DESIGN.md, "Who is calling".

| Method | Path | What |
|---|---|---|
| POST | `/api/routes/plan` | route options from OneMap, with live data and warnings (during a breakdown also `affected` and `bridges`) |
| POST | `/api/routes/check-disruptions` | re-check steps against LTA now |
| GET | `/api/places/search?q=` | place search (OneMap) |
| POST, GET, PUT, DELETE | `/api/routes`, `/api/routes/{id}` | save, list (`?travellerId=`), load, edit (with `version`), delete a route |
| POST | `/api/routes/{id}/approve` | approve a route |
| POST | `/api/routes/{id}/easy-read`, `/api/routes/{id}/photos` | AI wording to review; upload a step photo |
| GET | `/api/easy-read/limits` | the wording length limit, for the caregiver app's editor |
| POST, GET | `/api/trips`, `/api/trips/{id}` | make a trip on an approved route; look trips up (`?travellerId=`) |
| POST | `/api/trips/{id}/start`, `/end` | start (writes the Redis keys the rest of the system reads) or end a trip |
| GET | `/api/trips/{id}/pack` | the offline route pack (a temporary S3 link) |
| GET | `/api/trips/{id}/location`, `/plan` | where the traveller is; the steps they follow now, with map lines |
| GET, POST | `/api/trips/{id}/bridge/preview`, `/use`, `/send`, `/dismiss` | the free bridging bus: look, use now, send, don't send |
| POST | `/api/trips/{id}/replan`, `/replan/apply` | new routes from where the traveller is; send one (`{routeId}` saved for the trip, or `{steps}`) |
| GET | `/health`, `/ready` | liveness; readiness (Redis and storage reachable) |
| GET | `/internal/lta/bus-arrival/{code}`, `/train-alerts`, `/traffic-incidents` | LTA data for other services (cluster-only) |
| GET, PUT | `/api/dev/data-mode` | saved or live data (`DEMO_UI=true` only) |

## Demo scenarios

MRT breakdowns are rare and the saved data is a normal day, so `demo_tools` makes LTA *look* disrupted (for live and
saved data alike). A pretend breakdown lasts 30 minutes unless you pass `--minutes`.

```powershell
$t = "docker exec careroute-journey-service-1 python -m app.demo_tools"
iex "$t on"                          # the whole North East Line is down (or: on --stations NE10,NE11,NE12)
iex "$t check"                       # the disruption checker: a message for each active trip on those stations
iex "$t check --dry-run"             # show what it would send
iex "$t crowd on --stations NE17"    # Punggol's platform is very crowded (crowd off to stop)
iex "$t off"                         # back to the normal LTA feed
```

```bash
t="docker exec careroute-journey-service-1 python -m app.demo_tools"
$t on                                # the whole North East Line is down (or: $t on --stations NE10,NE11,NE12)
$t check                             # the disruption checker: a message for each active trip on those stations
$t check --dry-run                   # show what it would send
$t crowd on --stations NE17          # Punggol's platform is very crowded ($t crowd off to stop)
$t off                               # back to the normal LTA feed
```

**Bridging buses and re-routing** (plan Punggol to Orchard, save, approve, make a trip and start it first):

```powershell
iex "$t on --stations NE13,NE14,NE15 --shuttle NE12,NE13,NE14,NE15,NE16 --shuttle-dir Both"
iex "$t bridge"                      # look for bridges now (the API does every 30 s); bridge --send-now sends at once
```

```bash
$t on --stations NE13,NE14,NE15 --shuttle NE12,NE13,NE14,NE15,NE16 --shuttle-dir Both
$t bridge                            # look for bridges now (the API does every 30 s); $t bridge --send-now sends at once
```

Then, on the caregiver app's Trips tab:
- **A trip under way:** the bridging bus is proposed, and sent by itself after 3 minutes unless you send it sooner or
  choose not to.
- **A trip not started yet:** **Use the free bridging bus** (the new steps are shown first) or **Plan a new route**.
- **A new route:** planned on the Plan tab, saved as "Re-route (T-…)", checked on Saved routes, then sent from the trip.

To see a traveller moving without a phone, run a simulated trip: `tools/simulator`, see the root README's "Everything
in one command, with a simulated trip".

## Main settings

Set in the root `.env` (see `.env.example`); the full list with defaults is in `app/config.py`.

| Variable | Default | Purpose |
|---|---|---|
| `LTA_API_KEY` | (none) | LTA DataMall key |
| `ONEMAP_API_EMAIL`, `ONEMAP_API_PASSWORD` | (none) | OneMap account; the service logs in and renews its token (live data only) |
| `ONEMAP_MODE`, `LTA_MODE` | `recorded` (AWS: `live`) | saved or live API data |
| `GOOGLE_MAPS_API_KEY` | (none) | Street View step photos (off without it) |
| `ROUTES_STORE` | `redis` | `dynamodb` (compose and AWS) or `redis` |
| `PACKS_BUCKET` | (none: packs off) | S3 bucket for packs and photos |
| `AUTH_MODE` | `off` | `off`, `dev` (trusts an `X-Dev-User` header) or `cognito` |
| `DEMO_UI` | `false` (compose: `true`) | the live/saved switch and `demo_tools`; local demos only |
| `EASYREAD_URL` | (none) | the Easy-read Service, for AI wording |
| `TTS_MODE` | `off` (AWS: `polly`) | spoken instructions; `off` locally, as LocalStack has no Polly (the phone uses its own voice) |
| `TTS_VOICE`, `TTS_ENGINE`, `TTS_RATE` | `Joanna`, `neural`, `90%` | the Polly voice and how fast it reads |
| `BRIDGE_WATCH_S`, `BRIDGE_AUTO_SEND_S` | 30, 180 | how often bridging buses are looked for; how long one waits before it is sent |
