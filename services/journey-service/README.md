# Journey Service (Member 6)

Plans routes (OneMap), serves live bus/train/traffic data (LTA DataMall), and will own routes, trips and route packs (see `contracts/api.md`).

**Status:** proof of concept. Route planning (OneMap + LTA cross-check), saved and approved routes, trips, route packs and the disruption checker all work, against LocalStack (DynamoDB, S3, SQS) and Redis locally. Not built: AI easy-read, the Profiles lookups (traveller and caregiver names), photos and audio.

**Demoing it:** see [`DEMO.md`](DEMO.md) (a step-by-step script with every command).
The service runs on **saved API responses by default** (see "Saved-data mode"), so it needs no keys, no internet
and no OneMap token. The saved data is gitignored: on a fresh clone run `python scripts/capture_data.py --target both`
once (needs the keys in `.env`). Switch to live data with the caregiver app's **Data** box, without a restart. With live
data the service logs in to OneMap itself (see "OneMap login"). AWS always runs on live data.

## Code layout

The service has two deployable parts that share a little code, so the files are split by who needs them:

| File | API container | Lambda (disruption checker) |
|---|---|---|
| `main.py` (FastAPI endpoints), `models.py`, `auth.py` | yes | no |
| `demo_tools.py` (pretend disruption / crowding, live/saved switch; `DEMO_UI` only) | local demos | no |
| `journey_client.py` (the API's logic in one class: OneMap, planning, routes, trips, packs) | yes | no |
| `easy_read.py` (plain-language wording for route steps, no imports from the rest of `app/`) | yes | no |
| `route_store.py`, `trip_store.py`, `pack_store.py` (DynamoDB / Redis / S3 storage) | yes | no |
| `lta.py` (LTA DataMall client), `cache.py` (Redis JSON cache), `config.py` (settings), `data_mode.py` (live/saved switch) | yes | **yes** (shared) |
| `disruption.py` (`DisruptionChecker`), `lambda_handler.py` (entry point) | not used | **yes** |
| `aws_resources.py` (creates tables, bucket, queue for local runs) | local use | local use |
| `replay.py` + `saved_data/` (saved API responses, see "Saved-data mode") | on saved data (the default) | never: the stack sets `LTA_MODE=live` |

`JourneyClient` uses the LTA client as `client.lta` and keeps thin methods (`bus_arrival()` and so on) that call it.
The Lambda imports none of the API code: a test (`test_the_lambda_does_not_import_the_api_code`) fails if it ever does,
so its package needs only `httpx` and `redis` (boto3 is already in the Lambda runtime). The Dockerfile copies all of
`app/` into the container (including `saved_data/`); the Lambda package lists its 8 files explicitly (`deploy/lambda/make_zip.py`),
so saved data never ships to AWS. Packaging, schedule and permissions are in `deploy/` (see `deploy/README.md`).

Tests are split by area in `tests/` (`test_planning.py`, `test_routes_trips.py`, `test_disruption.py`, `test_lta.py`, `test_api.py`, `test_auth.py`, `test_replay.py`; shared setup in `helpers.py`), so you can run just the file you changed. `app/saved_data/fixtures/` holds the real LTA and OneMap responses they use
(made by `capture_data.py --target tests`; gitignored like the rest of `app/saved_data/`).

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/api/routes/plan` | `{"from":{lat,lng},"to":{lat,lng},"departAt"?,"mode"?,"maxWalkM"?,"maxOptions"?}` -> `{"options":[...]}`, see below |
| POST | `/api/routes/check-disruptions` | `{"steps": [...]}` (a plan option's or saved route's steps) -> `{"warnings", "checkedAt"}`: the `train_disruption` / `bus_notice` warnings as LTA reports things now. The caregiver app re-checks the selected plan option, the opened saved route and every unfinished trip's route with it every minute. Not in the contract yet |
| POST | `/api/routes` | save a route: `route.json` without `routeId` -> `201` with `routeId`, `version` 1, `approved` false |
| GET | `/api/routes/{routeId}` | load a saved route |
| PUT | `/api/routes/{routeId}` | edit: send the route plus the `version` you edited -> version + 1, approval cleared; `409` if someone saved first |
| DELETE | `/api/routes/{routeId}` | remove a saved route -> `204`; optional `?version=` refuses with `409` if it has changed; `409` too while one of its trips is active (**not in the contract yet**) |
| POST | `/api/routes/{routeId}/approve` | `approved: true` (idempotent; the version doesn't change) |
| GET | `/api/routes?travellerId=` | a traveller's saved routes, newest first (**not in the contract yet**) |
| POST | `/api/trips` | `{"routeId","date"}` -> `201` `trip.json` (`planned`); the route must be approved |
| POST | `/api/trips/{tripId}/start` | `planned` -> `active`; writes `active-trips` and `trip:{id}:route` in Redis |
| POST | `/api/trips/{tripId}/end` | `active` -> `completed`; removes them |
| GET | `/api/trips/{tripId}/pack` | `{packUrl, packVersion}`: the pack is stored in S3, the link is temporary |
| GET | `/api/trips/{tripId}`, `/api/trips?travellerId=&date=` | look trips up (**not in the contract yet**) |
| GET | `/api/places/search?q=` | place name, address or postal code -> `{"places":[{name,address,postalCode,lat,lng}]}` (OneMap, no token needed) |
| GET | `/health` | liveness (contract endpoint) |
| GET | `/ready` | readiness: Redis reachable |
| GET | `/internal/lta/bus-arrival/{busStopCode}?service_no=` | next buses at a stop; `services` is `[]` outside service hours |
| GET | `/internal/lta/train-alerts` | `status` 1 = normal/minor, 2 = disrupted, plus affected segments |
| GET | `/internal/lta/traffic-incidents` | current road incidents |

The `/internal/lta/*` endpoints are **not in `contracts/api.md`** yet (cluster-only helpers, also used by the planner later).

Errors use the contract format `{"error":{"code","message"}}`. LTA down -> `503 upstream_unavailable`; bad or missing key -> `502 upstream_misconfigured`.

## Route planning (`POST /api/routes/plan`)

Asks OneMap for up to 3 public-transport itineraries, then checks each against LTA DataMall.
The response shape is a proof-of-concept proposal and is **not yet in `contracts/api.md`**.

Each option has:
- `summary`: `durationS`, `startTime`, `endTime`, `walkDistanceM`, `transfers`, `fare`, `modes`, `crowd`
  (`low`/`moderate`/`high` for the busiest leg, `null` when LTA has no data)
- `steps`: one per OneMap leg, in the `contracts/examples/route.json` step format
  (`mode` is `walk`, `bus` or `train`; `easyRead`, `photoKey`, `audioKey`, `backup` are null for now;
  `underground` is null for train steps because OneMap doesn't say)
- `path`: `[lat, lng]` points for the whole journey
- `live`: for each bus step the next buses (LTA BusArrival; `nextBuses` is `[]` outside service hours), each with
  `load` (LTA's `SEA`/`SDA`/`LSD`) and `crowd`; for each train step the boarding platform's `crowd` and its `source`
  (`real-time`, or `forecast` when the train leaves more than 20 minutes from now)
- `warnings`: `train_disruption` (MRT leg stations match a TrainServiceAlerts segment),
  `bus_notice` (an LTA notice mentions the leg's bus service), `traffic_incident`
  (an LTA incident within `INCIDENT_RADIUS_M` of the bus leg's path), `long_walk` (one walk over the limit),
  `long_total_walk` (all walking over twice the limit), `crowded` (a bus or platform with `high` crowding) and
  `live_data_unavailable` (LTA was down; the options are still returned)

Request options: `mode` is `any` (default), `bus` (buses only) or `train` (MRT/LRT only); `maxOptions`
is 1-5; `maxWalkM` is 100-2000. OneMap does **not** enforce its walking limit (a 300 m limit still returned
1 km walks), so any walk longer than `maxWalkM` (default 500, `PLAN_MAX_WALK_M`) is flagged with a `long_walk`
warning, and a trip whose walking adds up to more than twice that with `long_total_walk`.

`sortBy` chooses the order of the options (the response says which it used in `sortedBy`; options are numbered
after sorting, ties keep OneMap's order):
- `walking` (default): least total walking first.
- `crowding`: least crowded first (the busiest leg, then how many legs are very busy), then least walking. An option
  with no crowd data counts as quiet.
- `time`: OneMap's own order.

How busy: buses use LTA's `Load` for the next bus (`SEA` seats available = `low`, `SDA` standing available =
`moderate`, `LSD` limited standing = `high`). Trains use LTA's platform crowding for the boarding station (`PCDRealTime`
now, `PCDForecast` for a train further ahead). That is how busy the *platform* is, not the carriage. If that dataset
is unavailable for the key, crowding is simply left out; planning is unaffected.

Train steps carry an extra `stops` list (`[{code, name}]` for every station on the leg). It is not in
`contracts/examples/route.json`; the disruption checker uses it.

Errors: OneMap down -> `503`; OneMap login or token refused or missing -> `502 upstream_misconfigured`
(check `ONEMAP_API_EMAIL` / `ONEMAP_API_PASSWORD`); coordinates outside Singapore -> `422`.

## Who is calling

`AUTH_MODE` decides how the service knows who is calling (`app/auth.py`):

| Mode | Behaviour |
|---|---|
| `off` (default) | Nobody is checked and every rule below is skipped. For local demos only. |
| `dev` | Trusts an `X-Dev-User: G-003` (caregiver) or `U-042` (traveller) header. **Local testing only**: anyone can claim to be anyone. |
| `cognito` | Verifies the `Authorization: Bearer <jwt>` header against the Cognito user pool: signature (RS256 only, keys from the pool's JWKS), `iss`, `exp`, token type and audience. |

A verified caller has a contract id (from `AUTH_ID_CLAIM`, default `custom:userId`, e.g. `G-003`) and one role
(from `AUTH_ROLE_CLAIM`, default `cognito:groups`, containing `caregiver` or `traveller`, not both). The id's prefix
must match the role (`G-` caregiver, `U-` traveller). **The claim names are not agreed with the team yet**: they are
settings, so the final choice is a config change, but the Cognito pool has to carry them (custom attributes
can't be renamed once the pool has them). A bad token gets `401` with no reason (the reason is only logged);
if the signing keys can't be fetched it is `503`, not `401`.

**Who may do what** (a route belongs to its caregiver `caregiverId` and its traveller `travellerId`):

| Action | Allowed |
|---|---|
| plan, place search | any logged-in user |
| save a route | a caregiver, as themselves (`caregiverId` must be their own id) |
| edit, approve or delete a route, create a trip | the route's caregiver (its traveller gets `403`) |
| read a route; read, start or end a trip; get its pack | the route's caregiver or traveller |
| route and trip lists | only what the caller may see (someone else's traveller gives an empty list) |
| anyone else, any route or trip | `404`, the same as for one that doesn't exist |

`/internal/lta/*`, `/health`, `/ready` and `/api/dev/*` are not user endpoints and are not checked.

Not done yet: checking that a caregiver is really *linked* to the traveller, which needs the Profiles service
(it would call Profiles' `GET /api/travellers` with the caller's token). Until then a caregiver can save a route for any
traveller id; they just can't see other caregivers' routes. Try it locally: `$env:AUTH_MODE = "dev"` before
`docker compose ... up`, then `curl.exe -H "X-Dev-User: G-DEMO" ...` (the caregiver app has an "Act as" box).

## Saved-data mode (demos without the live APIs)

OneMap access tokens last 3 days, so the service runs on responses captured earlier (and needs no login at all). This
is the **default** (`ONEMAP_MODE` and `LTA_MODE` default to `recorded`): the service answers from `app/saved_data/`
instead of the internet. Everything after the API call is the real code: parsing, planning, the plain-language wording,
the LTA cross-check, saving, trips, packs and the disruption demo. AWS sets both to `live` (the EKS manifests and the
Lambda stack).

`app/saved_data/` is **gitignored**, so a fresh clone has none: run `python scripts/capture_data.py --target both`
first. Without it, saved-data requests answer `404` saying exactly that.

**Switching while it runs** (with `DEMO_UI=true`, as in compose): the caregiver app's header has a **Data: Saved / Live**
box, or run `docker exec careroute-journey-service-1 python -m app.demo_tools mode live` (`mode saved` to go back,
`mode` to show it). Both use `GET/PUT /api/dev/data-mode` (`{"onemap"?, "lta"?}`: `live` or `recorded`), kept in Redis
(`journey:data-mode`) so every replica follows it; the environment values are only the starting point. Live and saved
answers are cached under different keys (`journey:cache:` vs `journey:cache:recorded:`), so they never mix. Without
`DEMO_UI` (AWS) the switch doesn't exist and the environment decides.

- **What is saved:** OneMap place search and routes for nine trips between MRT stations (bus-or-train and bus-only
  recordings; "train only" is filtered out of the first, as OneMap's `RAIL` means no bus):

  | Area | Trips |
  |---|---|
  | North | Ang Mo Kio to Toa Payoh |
  | North-east | Punggol to Orchard, Sengkang to Serangoon, Hougang to Dhoby Ghaut |
  | East | Tampines to Changi Airport, Pasir Ris to Bugis, Bedok to Paya Lebar |
  | South | HarbourFront to Marina Bay, Tiong Bahru to Paya Lebar |

  Also saved: LTA bus arrivals for every stop where those trips board a bus (about 40 stops), platform crowding for
  the lines they ride, bus stops, train alerts and road incidents. About 1 MB in all.
- **Times are moved to now:** bus arrivals count from the moment you ask, and trips start at the requested time, so
  nothing looks stale.
- **Other trips:** a start or end within about 150 m of a saved one matches; anything else answers `404 not_found`
  with the list of saved trips (`GET /api/dev/data-mode` lists them too, as `trips`).
- **Adding trips:** add the trip to `TRIPS` in `scripts/capture_data.py` (two `place(...)` calls: the station's search
  text and the text that picks the right result, e.g. `"(EW2)"`) and run
  `python scripts/capture_data.py --target demo --routes live`. It logs in to OneMap with the account in `.env`,
  records every trip's routes, looks up the places (no token needed), and refreshes the LTA data (needs `LTA_API_KEY`).
  A trip is listed only once it has a saved route. `--routes fixtures` rebuilds just the first two trips offline from
  `app/saved_data/fixtures`. Keys and the password are never written to the data files (a OneMap token it logs in for
  is saved to `.env`, see "OneMap login"). After recording, update the trip list expected in `tests/test_replay.py` if
  you added trips.
- `GET /api/dev/data-mode` (with `DEMO_UI`) says which data is in use and when it was saved. The service logs a
  `SAVED-DATA MODE` warning at startup.

## OneMap login

OneMap access tokens last 3 days. Put the OneMap account in the root `.env` as `ONEMAP_API_EMAIL` and
`ONEMAP_API_PASSWORD` and the service gets and renews its own token: it logs in
(`POST /api/auth/post/getToken` -> `access_token`, `expiry_timestamp`), reuses the token until 5 minutes before it
expires, and logs in again early if OneMap rejects it once. Redis holds the token, so replicas share one login. If the
login fails, or there is no account, it falls back to a pasted `ONEMAP_API_KEY`; with neither, planning answers
`502 upstream_misconfigured`. The password and the token are never logged or shown (`Settings` hides them from
`repr`). The code is `app/onemap_token.py`; saved-data mode never logs in. In Kubernetes the two values go in the
`journey-api-keys` Secret (`deploy-eks.ps1` copies them from `.env`), and the pods renew the token themselves: nothing
to run or update by hand on AWS.

Locally, each new token is also written back to the root `.env` as `ONEMAP_API_KEY` (only the token line changes; the
rest of the file is kept): by the service when `ONEMAP_TOKEN_ENV_FILE` is set (compose mounts the `.env` at
`/app/.env` and sets it), and by `scripts/capture_data.py` when it logs in. Unset on AWS, where nothing is written.

## Deploying to AWS: resource names

The team's resource prefix is **`careroute`**: `careroute-routes`, `careroute-trips`, queue `careroute-disruptions`, the
shared bucket `careroute-files-<account id>`. This service's defaults already use it, so the same names work locally and
on AWS. The platform stack builds its names from its `ResourcePrefix` parameter (default `careroute`) and its IAM role
only allows that prefix; `deploy/` reads whichever prefix the stack really uses (see `deploy/README.md`). Table keys,
index names and the TTL attribute already match the template.

Ready-made files for putting the service (and the disruption Lambda) on that stack are in `deploy/`; see
`deploy/README.md` for the order, the names to use, and what has and hasn't been checked (nothing has been run on real
AWS). One thing the platform's permissions changed in the code: `/ready` checks the DynamoDB tables with a `GetItem`
on a key that doesn't exist, not `DescribeTable` (which the platform's role doesn't allow), and a failure names the
store and the AWS error, e.g. `routes table: AccessDeniedException`. A good result is remembered for `READY_CACHE_S`
seconds (default 10) so a burst of probes doesn't each call AWS; a failure never is.

## Saved routes

Routes are stored through `app/route_store.py`, which has two stores with the same four methods
(`create`, `get`, `replace`, `list_for_traveller`). `ROUTES_STORE` picks one:

- `dynamodb`: the real one. Table `careroute-routes` (`contracts/dynamo-db-tables.md`): partition key `routeId`,
  on-demand billing, global index `travellerId-index` on `travellerId`. Edits are one conditional write
  (`version = :expected`), so two writers can't overwrite each other. Runs against LocalStack locally (the compose
  overlay selects it and enables DynamoDB) and real DynamoDB on AWS (leave `AWS_ENDPOINT_URL` unset).
  `CREATE_RESOURCES=true` creates the table on startup (local only); in AWS create it once with
  `python -m app.aws_resources`. Note the index is eventually consistent on real DynamoDB, so a list straight after
  a save can briefly miss the new route; reading by id is always current.
- `redis` (the default when `ROUTES_STORE` is unset): needs no AWS, for demos and tests. Not durable: the contract says
  Redis only holds data that can be rebuilt.

LocalStack's free edition doesn't keep data across a restart of the LocalStack container; the table is recreated
(empty) the next time the service starts with `CREATE_RESOURCES=true`.

- `routeId` is `R-` plus 6 random hex characters, not a counter, so two replicas can't clash.
- Saving takes `travellerId` and `caregiverId` from the request body. With `AUTH_MODE=off` they are unverified labels;
  with a login (see "Who is calling") `caregiverId` must be the caller's own id.
- Editing needs the `version` you edited; if it's stale you get `409` and nothing is overwritten.
  Every edit clears `approved`, so the caregiver approves what they changed. Approving keeps the version.
- `travellerId` and `caregiverId` can't be changed on a saved route.
- Deleting a route leaves its planned trips in place, but they can no longer start or build a pack (`409`: the route was
  deleted). A route with an active trip can't be deleted until that trip ends.
- A plan option can be posted as-is; `summary`, `live`, `warnings` and `stepPaths` are dropped, extra step fields
  (like a train step's `stops`) are kept.

## Trips and the route pack

A **trip** is one journey on one day along an approved route (`status`: `planned` -> `active` -> `completed`).
It pins the route's `version`: only the latest version of a route is kept, so if the route is edited after the trip was
created, starting it or building its pack returns `409` and a new trip has to be created.

- `POST /api/trips` `{"routeId", "date"}` needs an **approved** route (else `409`). Table `careroute-trips`
  (`travellerId-date-index`, `status-index`, TTL on `expiresAt` = 30 days after the trip; never returned by the API).
- `POST /api/trips/{id}/start` writes what the rest of the system reads (`contracts/redis-keys.md`): the id in the
  set `active-trips` and the steps in `trip:{id}:route` (24 h). That cached route holds ids, coordinates, line
  numbers and public stop names only: no instruction text, place labels or `easyRead`. Starting twice is harmless
  and re-writes the keys, so a Redis that lost its data is repaired.
- `POST /api/trips/{id}/end` removes both keys. The disruption checker only looks at trips in `active-trips`.
- `GET /api/trips/{id}/pack` builds the offline file (`contracts/examples/route-pack.json`), saves it to S3 as
  `packs/{routeId}/v{version}.json` and returns `{packUrl, packVersion}`; the link is temporary
  (`PACK_URL_TTL_S`, default 1 h). `travellerName` and `caregiver` are `null` until the Profiles service exists.
- `GET /api/trips/{id}` and `GET /api/trips?travellerId=&date=` are not in the contract yet.

Pack storage is **off unless `PACKS_BUCKET` is set** (so a deployment without a bucket still becomes ready); then
`/api/trips/{id}/pack` returns `503`. The compose overlay sets `PACKS_BUCKET=careroute-packs` and creates the bucket,
with a CORS rule so a browser can download from the link. `S3_PUBLIC_ENDPOINT_URL` is the address a browser uses to
reach S3 (`http://localhost:4566` for LocalStack): the host is part of the link's signature, so it must be the one
the phone will call. The free LocalStack doesn't check signatures, so a tampered link still downloads locally; real
AWS rejects it.

## Easy-read wording

`easy_read(step)` in `app/easy_read.py` writes short plain-language text for each step, e.g. "At Opposite Fengshan Primary
School, wait for bus number 45. Get on and tap your card. Get off at Tanah Merah Station Exit 2 after 5 stops."
It uses only the step's own facts (bus number, station names, stop count), so nothing can be invented. Plans and saved
routes get it automatically; text a caregiver wrote is kept. Bus and train steps carry an extra `stopCount` field.
The contract's `POST /internal/easyread` endpoint and an AI rewrite (with this text as the fallback) are not built.

## Route geometry

The demo UI is the caregiver app (`apps/caregiver`, see its `DEMO.md`). `DEMO_UI=true` (set in `compose.journey.yaml`)
only turns on the dev extras: the live/saved switch and `app/demo_tools.py`. Neither has a login, so leave `DEMO_UI`
off anywhere that isn't a local demo.

Each plan option includes `stepPaths`, the `[lat, lng]` path of every step (`path` is all of them joined).
Each step's path starts at the previous step's end, because OneMap's legs can end up to ~80 m apart.

## Disruption checker (Lambda)

`app/lambda_handler.py` (handler `app.lambda_handler.handler`, meant for an EventBridge schedule) reads the
trips in Redis `active-trips`, and for each one the route (`trip:{id}:route`) and context
(`trip:{id}:context`, written by the Check-in Service). If LTA reports a disrupted MRT segment (status 2)
that touches a train step the traveller hasn't finished, it sends one `disruption.json` to the
`careroute-disruptions` queue. Minor delays (status 1) are ignored (`DISRUPTION_MIN_STATUS`).

- Delivery is at-least-once; a marker key `journey:disruption-sent:{id}:{tripId}` (6 h) stops repeats.
- The checker watches trips that were started (`POST /api/trips/{id}/start` writes the Redis keys above).
- LTA being down makes the run fail on purpose, so the outage shows up in CloudWatch.
- Local one-off run: `docker exec -e CREATE_RESOURCES=true careroute-journey-service-1 python -m app.lambda_handler`
- Not done: Lambda packaging, the EventBridge rule, IAM permissions, and VPC access to Redis.

### Demoing a disruption

Real MRT outages are rare (and the saved data is a normal day), so for a demo you can make LTA *look* disrupted
(`app/demo_tools.py`, only runs when `DEMO_UI=true`, not part of the Lambda package). Both the planner and the checker
read LTA's train alerts through a Redis cache; this writes a pretend "major disruption" response into it, for live and
saved data alike.

```powershell
$t = "docker exec careroute-journey-service-1 python -m app.demo_tools"
# 1. in the caregiver app: plan a route that uses the MRT, save it, approve it, create a trip and start it
iex "$t on"                      # pretend the whole North East Line is down (or: on --line NEL --stations NE10,NE11,NE12)
# 2. plan the route again: the MRT step gets a red warning and an orange marker on the map
iex "$t check"                   # the disruption Lambda's job: sends a disruption message for each active trip on those stations
iex "$t check"                   # run again: nothing new is sent (already told); `check --again` sends it once more
iex "$t check --dry-run"         # show what would be sent without putting it on the queue
iex "$t off"                     # back to the normal LTA feed
iex "$t crowd on --stations NE17"   # pretend Punggol's platform is very crowded; `crowd off` removes it
```

`on` lasts 30 minutes unless you pass `--minutes`. A normal-day `check` finds nothing, because the checker does
nothing until LTA reports a disruption. Trips on a different line, or already past the affected stretch, are not reported.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LTA_API_KEY` | (none) | DataMall AccountKey. Put it in the root `.env` (see `.env.example`) |
| `ROUTES_STORE` | `redis` | `dynamodb` or `redis`, see Saved routes |
| `READY_CACHE_S` | `10` | how long `/ready` remembers a good storage check |
| `ROUTES_TABLE`, `TRIPS_TABLE` | `careroute-routes`, `careroute-trips` | DynamoDB table names |
| `PACKS_BUCKET` | (empty = packs off) | S3 bucket for route packs, e.g. `careroute-packs` |
| `S3_PUBLIC_ENDPOINT_URL` | = `AWS_ENDPOINT_URL` | S3 address as a browser sees it (LocalStack: `http://localhost:4566`) |
| `PACK_URL_TTL_S`, `TRIP_KEY_TTL_S` | 3600, 86400 | pack link lifetime; lifetime of `trip:{id}:route` in Redis |
| `CREATE_RESOURCES` | `false` | create the table and queue on startup (local only) |
| `ONEMAP_API_EMAIL`, `ONEMAP_API_PASSWORD` | (none) | the OneMap account; the service logs in and renews its own token, see "OneMap login". Not needed when `ONEMAP_MODE=recorded` |
| `ONEMAP_API_KEY` | (none) | a pasted OneMap token (lasts 3 days), used when there is no account or the login fails |
| `ONEMAP_TOKEN_ENV_FILE` | (none; compose: `/app/.env`) | local only: write each new OneMap token to this `.env` as `ONEMAP_API_KEY` |
| `PLAN_MAX_WALK_M` | `500` | longest single walk before a `long_walk` warning (a request's `maxWalkM` overrides) |
| `PLATFORM_CROWD_TTL_S`, `PLATFORM_FORECAST_TTL_S` | 120, 21600 | cache lifetimes for platform crowding (real-time, forecast) |
| `ONEMAP_MODE`, `LTA_MODE` | `recorded` (AWS: `live`) | `recorded` answers from the saved responses in `app/saved_data/`, see "Saved-data mode"; with `DEMO_UI` only the starting value |
| `DEMO_UI` | `false` (compose: `true`) | local demo extras: the live/saved switch (`/api/dev/data-mode`) and `app/demo_tools.py` |
| `INCIDENT_RADIUS_M` | `200` | how close a road incident must be to a bus leg to be flagged |
| `REDIS_URL` | `redis://localhost:6379/0` | response cache |
| `AUTH_MODE` | `off` | `off`, `dev` or `cognito`, see "Who is calling" |
| `AUTH_ID_CLAIM`, `AUTH_ROLE_CLAIM` | `custom:userId`, `cognito:groups` | token claims holding the caller's `G-`/`U-` id and role (names not yet agreed with the team) |
| `AUTH_CAREGIVER_GROUP`, `AUTH_TRAVELLER_GROUP` | `caregiver`, `traveller` | the group names that mean each role |
| `AUTH_TOKEN_USE` | `id` | which Cognito token the apps send: `id` or `access` (custom attributes only exist in the ID token) |
| `COGNITO_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID` | (none) | required when `AUTH_MODE=cognito`; the service refuses to start without them |
| `COGNITO_JWKS_URL` | the pool's standard address | where the signing keys come from |
| `BUS_ARRIVAL_TTL_S` etc. | 20 s / 60 s / 120 s / 24 h | cache lifetimes, matched to LTA's refresh rates |

## Run

Tests (no Docker, no network, no key needed):

```powershell
cd services/journey-service
pip install -r requirements-dev.txt
pytest
```

Docker Compose (from the repo root, with `LTA_API_KEY` and `LOCALSTACK_AUTH_TOKEN` in `.env`; it starts its own Redis
and LocalStack, so stop `services/detectors/compose.yaml` first, since both use host port 6379):

```powershell
docker compose --env-file .env -f services/journey-service/compose.journey.yaml up --build -d
curl.exe http://localhost:8006/health
curl.exe http://localhost:8006/internal/lta/bus-arrival/83139
```

kind:

```powershell
docker build -t journey-service:local services/journey-service
kind load docker-image journey-service:local --name csc3104-local
kubectl create secret generic journey-api-keys --from-env-file=.env -n csc3104
kubectl apply -k k8s/local
kubectl port-forward svc/journey-service 8006:8000 -n csc3104
```

## Capturing real LTA responses

```powershell
$env:LTA_API_KEY = "<your key>"
python scripts/capture_data.py --target tests  # writes app/saved_data/fixtures/lta_*.json and onemap_*.json
```
