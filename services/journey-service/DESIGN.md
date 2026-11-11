# Journey Service: how it works

The details behind [README.md](README.md): what each part does and why. Start with the README to run and use the
service; come here when changing it.

## Code layout

The service has two deployable parts that share a little code, so the files are split by who needs them:

| File | API container | Lambda (disruption checker) |
|---|---|---|
| `main.py` (FastAPI endpoints), `models.py`, `auth.py` | yes | no |
| `demo_tools.py` (pretend disruption / crowding, live/saved switch; `DEMO_UI` only) | local demos | no |
| `journey_client.py` (what the API calls: routes, trips, packs; holds the parts below) | yes | no |
| `onemap.py` (OneMap client), `planning.py` (route options, live warnings, where trains aren't running) | yes | no |
| `photos.py` (step photos, uploads, links), `trip_changes.py` (bridging buses, re-routes; mixed into the client), `timefmt.py` | yes | no |
| `easy_read.py` (plain-language wording for route steps, no imports from the rest of `app/`) | yes | no |
| `easy_read_ai.py` (asks the Easy-read Service for simpler wording), `streetview.py` (step photos) | yes | no |
| `speech.py` (each step read aloud by Amazon Polly, for the pack's `audioUrl`) | yes | no |
| `landmarks.py` (buildings along a route for the AI wording to mention), `geo.py` (distances between points and to a path) | yes | no |
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

Tests are split by area in `tests/` (`test_planning.py`, `test_routes_trips.py`, `test_bridge.py`, `test_disruption.py`,
`test_lta.py`, `test_api.py`, `test_auth.py`, `test_replay.py`, `test_easy_read.py`, `test_easy_read_ai.py`,
`test_landmarks.py`, `test_streetview.py`, `test_speech.py`; shared setup in `helpers.py`), so you can run just the file
you changed. `app/saved_data/fixtures/` holds the real LTA and OneMap responses they use (made by
`capture_data.py --target tests`; in Git, like the rest of `app/saved_data/`).

## Route planning (`POST /api/routes/plan`)

Asks OneMap for up to 3 public-transport itineraries, then checks each against LTA DataMall.
The response shape is a proof-of-concept proposal and is **not yet in `contracts/api.md`**.

Each option has:
- `summary`: `durationS`, `startTime`, `endTime`, `walkDistanceM`, `transfers`, `fare`, `modes`, `crowd`
  (`low`/`moderate`/`high` for the busiest leg, `null` when LTA has no data)
- `steps`: one per OneMap leg, in the `contracts/examples/route.json` step format
  (`mode` is `walk`, `bus` or `train`; `photoKey`, `audioKey`, `backup` are null in a plan (photos are added on save);
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
traveller id; they just can't see other caregivers' routes. Try it locally: `$env:AUTH_MODE = "dev"` (bash:
`export AUTH_MODE=dev`) before `docker compose ... up`, then `curl.exe -H "X-Dev-User: G-DEMO" ...` (bash: `curl`)
(the caregiver app has an "Act as" box).

## Saved-data mode (demos without the live APIs)

OneMap access tokens last 3 days, so the service runs on responses captured earlier (and needs no login at all). This
is the **default** (`ONEMAP_MODE` and `LTA_MODE` default to `recorded`): the service answers from `app/saved_data/`
instead of the internet. Everything after the API call is the real code: parsing, planning, the plain-language wording,
the LTA cross-check, saving, trips, packs and the disruption demo. AWS sets both to `live` (the EKS manifests and the
Lambda stack).

`app/saved_data/` is **in Git**, so a fresh clone runs on it straight away. To refresh it from the real APIs, run
`python services/journey-service/scripts/capture_data.py --target both` from the repo root. A request the saved data
doesn't cover answers `404` saying so.

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
  Each step's `photoUrl` and `audioUrl` (see "Spoken instructions") are presigned links that last `PHOTO_URL_TTL_S`.
- `GET /api/trips/{id}` and `GET /api/trips?travellerId=&date=` look trips up (`expiresAt` and the plan internals left out).

Pack storage is **off unless `PACKS_BUCKET` is set** (so a deployment without a bucket still becomes ready); then
`/api/trips/{id}/pack` returns `503`. The compose overlay sets `PACKS_BUCKET=careroute-packs` and creates the bucket,
with a CORS rule so a browser can download from the link. `S3_PUBLIC_ENDPOINT_URL` is the address a browser uses to
reach S3 (`http://localhost:4566` for LocalStack): the host is part of the link's signature, so it must be the one
the phone will call. The free LocalStack doesn't check signatures, so a tampered link still downloads locally; real
AWS rejects it.

## Easy-read wording

`app/easy_read.py` writes short plain-language text for each step as a checklist: one action per sentence, each
starting with a verb. The caregiver app shows each sentence as a bullet point (tests: `tests/test_easy_read.py`). It
uses only the route's own facts, so nothing can be invented, and says what heads off the usual mistakes:

| Step | Example | Why |
|---|---|---|
| bus | Find bus stop 84039 at Bedok Station Exit A. Check the number on the pole. Raise your hand for bus 28 to Toa Payoh Interchange. It comes every 10 to 13 minutes. Tap your card to get on. The ride takes about 17 minutes. After Block 14 Market and Food Centre, press the bell. Get off at Paya Lebar Station Exit D. Tap your card to get off. | The stop across the road has the same name but goes the other way (its pole number differs); buses only stop when someone waits or rings; fares are by distance, so tap out; a bus skips stops nobody waits at, so the stop before is the cue, not a count |
| train | Tap your card at the gantry. Follow the purple signs for the North East Line. Take the train going to HarbourFront. Get off at Dhoby Ghaut MRT Station. It is the 11th stop. | Both platforms are the same line: the signs say "Towards HarbourFront". No gantry after a change of line. |
| walk | Walk to the bus stop at Block 119D. / Change to the red North South Line. Follow the red signs. / Walk to ION Orchard. You have arrived. | What is at the end; the line to change to; the destination by name |

- `route_wording(steps, destination)` words each step knowing the one before it and whether it is the last.
  Plans use it (without the destination's name, which a plan doesn't have); saving a route redoes wording that is
  still the planner's own with the name ("Walk to your destination." -> "Walk to ION Orchard."). Text a caregiver
  wrote is kept.
- Which way a train goes is worked out from the first and last station codes and `TRAIN_SIGNS`, each line's two
  ends as of 2026 (update it when a line is extended). Where there are no two simple ends (the Circle Line, LRT
  loops) the wording names the station the train stops at next instead, from the route's own stops.
- A bus step keeps every stop of the ride (`stops`, as train steps do) for the stop before getting off, and gets
  `towards` (where the bus is going, its front sign; none for a loop service) and `every` ([low, high] minutes
  between buses in the daytime) from LTA's BusRoutes and BusServices lists (`lta.bus_service()`: fetched live, also
  with saved data, cached a week and prefetched at startup, as they take ~9 s; best-effort). One thread loads them
  at a time: a plan made meanwhile goes without, and a failed load is retried after 10 minutes, not on every bus
  step. A loop service names no destination: it passes each stop pole once a loop, the same way round, so at the
  right pole every bus of that number is the right one.
- Bus wording stays within 300 characters (`MAX_CHARS`, set by `EASYREAD_MAX_CHARS`: the one setting for it, sent
  to the Easy-read Service with each request and to the caregiver app's editor by `GET /api/easy-read/limits`), so
  the traveller app's main button stays on screen: how
  often the bus comes is left out first, then the ride's length. A step whose essential sentences alone are longer
  (very long stop names) stays longer; the editor only asks to shorten wording the caregiver is changing. LTA's short forms are spelled out (Blk, Opp, Int, Stn, Bef, Aft, Mkt/Fc, Ave, Rd, ...).

### Simpler wording with AI

On the **Saved routes** tab, **Simplify with AI** sends the route's steps to the Easy-read Service
(`services/easyread-service`, which runs a small model on Ollama) and opens the editor with its wording. The caregiver
checks and edits each step, then saves (a new version that needs approving again). Nothing is saved before that.

- `POST /api/routes/{routeId}/easy-read` (route owner only) -> `{"steps": [{stepIndex, easyRead, fallback, landmarks}],
  "provider"}`. `landmarks` lists the ones the wording mentions, each with a photo: `[{name, kind, lat, lng, seenFrom,
  photoKey, sameView, photoUrl}]`.
- The AI rewrites the template text above, not text a caregiver wrote. Names that aren't public stops ("Home", an
  address) are sent as `[place 1]` and put back afterwards (`easy_read_ai.py`), as `contracts/api.md` asks.
- The Easy-read Service checks each step (`hallucination_check.py` there): the bus number, stop names and stop count must
  still be there, and every number must come from the step. A step that fails keeps the template text (`fallback: true`).
- `EASYREAD_URL` unset, the service down or slower than `EASYREAD_TIMEOUT_S` (90 s): every step gets the template text
  and `provider: "template"`. Planning, saving and trips never call it, and `/ready` doesn't check it.

### Landmarks along the route

So the traveller can tell they are going the right way, the AI wording also mentions buildings they can't miss:
"Look out for City Plaza on your left. Press the bell. Get off at Paya Lebar Station Exit D." (`EASYREAD_LANDMARKS`,
on by default).

- `app/landmarks.py` picks the spots to look at: the start, middle (walks over 250 m) and end of a walk, and the end of a
  bus ride plus a spot 300 m before it (to press the bell). Train steps and changes of line get none.
- `nearby_buildings()` asks OneMap's reverse geocode for the named buildings within 100 m of each spot (live OneMap, also
  in saved-data mode, like station exits; cached 30 days). A building is kept only within 100 m of the step's own path,
  with its kind (mall, station, hawker centre, ...; condominiums are left out) and which side of the road it is on.
- The Easy-read Service lets the model look them up with a tool, choose one per spot, and checks every name it writes
  (its README says how). It never gets coordinates.
- **Privacy:** nothing is looked up within 200 m of a place without a stop code (home, an address someone typed, or a
  station typed as the start or end), so the names can't give away where the traveller lives. A route that starts and
  ends at typed places and only takes trains may get no landmarks at all.
- **A photo of each landmark** the wording names is taken when the caregiver presses Simplify with AI, so they see it
  in the editor before saving: Street View from the traveller's spot on the route (`seenFrom`, e.g. the bus stop),
  turned to face the building, the same way as step photos (`photo_key(..., near=)`: `<lat>_<lng>_from_<lat>_<lng>.jpg`).
  Not from the building's own point: that is its middle, and the panorama nearest it can be on the far side or
  against a wall. Each new one is a paid Street View image (at most 2 per step); the same view is fetched once.
- **No photo that repeats another.** A landmark next to the bus stop, seen from the stop, would come out as the stop
  again. So first (with free metadata calls) the service works out which panorama and direction each photo would
  use; a landmark photo from the same panorama as the step's photo or an earlier landmark's (or one within 15 m),
  facing less than 90 degrees away from it, isn't taken. Its `sameView` says where it is seen instead (`"step"`, or
  the other landmark's name), and the app says so under the photos ("it is in the step photo above").
- Saved, a step keeps `landmarks: [{name, kind, lat, lng, seenFrom, photoKey}]`, but only those its `easyRead` still
  names: a landmark the caregiver edits out of the wording goes with it. Answers add a `photoUrl` to each, and the
  route pack gives the traveller `landmarks: [{name, photoUrl, sameView}]` per step.

## Step photos

Saving a route adds a Google Street View photo of where each walk and bus step ends (the stop to wait at, the stop to
get off at, the destination), turned to face it, so the traveller can check they are in the right place
(`streetview.py`). Train steps get none: a street above an underground platform would mislead.

- **Recent panoramas, Google's own first.** Anything older than `STREETVIEW_MAX_AGE_YEARS` (default 5) is skipped:
  Google's indoor walk-throughs (malls, stations) date from 2014-2019, while the street car came by in 2022-2025.
  The metadata call only gives the panorama nearest the point asked about, and next to a station or stop that is
  nearly always someone's upload (by the metadata's `copyright`), often indoors (a restaurant, a mall corridor). The
  street car stays on the road, often 60-90 m away, so rings of 8 points 30, 60 and 90 m around the stop are asked
  too (metadata calls are free) until a recent Google panorama turns up; the closest within 100 m is used, the
  camera zoomed in the further away it is. If there is none, the closest recent upload within 50 m is used. Neither:
  the closest older panorama is still used (Google's first), stored under `media/streetview/v2/rough/`, and the
  caregiver app shows it with a hint to check it or add a better one. No panorama at all: the step saves without a
  photo, for the caregiver to add one (e.g. of the gantry).
- **The last walk is photographed at the destination itself**, the route's `destination` point, not where OneMap's
  leg ends: in the saved demo trips that is up to ~50 m off (recorded with rounded endpoints). A destination that is a
  station ("Orchard MRT Station (NS22)") gets its exit, as below.
- **A walk to an MRT/LRT station is photographed at its exit**, the one nearest to where the walk starts: the
  station's own point is underground or inside a mall. Exits come from OneMap's search ("SENGKANG MRT STATION EXIT
  A", no token needed; cached 7 days); this lookup is always live, also with saved data. Unknown exits: the station
  point is used, as before.
- Needs `GOOGLE_MAPS_API_KEY` (Street View Static API) and `PACKS_BUCKET`. Without them routes save without photos.
- Photos are stored in the pack bucket as `media/streetview/v2/<lat>_<lng>.jpg`, so a stop used by many routes is
  fetched once (`v2`: photos stored before Google-only weren't reused; routes saved with one keep it until **Use
  Street View photo**). A step whose photo can't be fetched (no panorama nearby, Google or S3 down) saves without one.
- The caregiver can replace a step's photo in the editor: `POST /api/routes/{routeId}/photos` (multipart field
  `photo`; JPEG, PNG or WebP, checked by its bytes, up to 5 MB) stores it as `media/uploads/<hash>.<ext>` and returns
  `{photoKey, photoUrl}`; saving the route with that `photoKey` keeps it. **Use Street View photo** sets it back to null.
- `photoKey` must be a key under `media/` (`models.py`), because answers hand out a presigned `photoUrl` for it.
  `photoUrl` is made for each answer (route answers and packs, valid for `PHOTO_URL_TTL_S`, 7 days) and never stored.
- Google's terms limit how long Street View images may be stored; keeping them in S3 is a shortcut for this project.

## Spoken instructions

The traveller app's **Listen** button plays each step's `audioUrl` from the pack: the step's easy-read wording read
aloud by Amazon Polly (`app/speech.py`). The phone downloads it with the photos, so it plays with no signal. Without
one, the phone reads the text with its own voice.

- `TTS_MODE=polly` turns it on (set in `k8s/eks/services/journey-service.yaml`; the platform stack's IAM role allows
  `polly:SynthesizeSpeech`). Default `off`: LocalStack has no Polly, so locally packs have `audioUrl: null`. To try
  Polly locally, set `TTS_MODE=polly` with real AWS credentials; `TTS_ENDPOINT_URL` stays unset, so Polly is real
  AWS while S3 stays LocalStack.
- **When it is made:** when the caregiver approves a route (only approved routes get trips, and their wording is
  final), and for the new steps when a changed plan (bridging bus, re-route) is sent, before the phone can see the new
  pack. Not on every draft save. Building a pack only makes what is still missing (Polly was down earlier), so the
  phone's pack request normally doesn't wait for Polly, and two pods don't both make the same file.
- **Stored** in the pack bucket as `media/speech/v1/<voice>-<engine>-<rate>/<hash of the words>.mp3`, so the same
  words are made once and an edited step gets a new file. Nothing is saved on the route. The link in the pack is
  presigned like `photoUrl` (`PHOTO_URL_TTL_S`).
- **How it reads:** a little slower than normal (`TTS_RATE`, `90%`), with a pause between sentences; bus numbers and
  bus stop codes are read digit by digit ("bus one nine zero"). Voice: `TTS_VOICE` (`Joanna`), `TTS_ENGINE`
  (`neural`). The step's text is escaped, so it can't add SSML of its own.
- **Failures:** Polly down or refusing gives that step `audioUrl: null`; approving, sending and the pack all still
  work. A step's own `audioKey` (an S3 key under `media/`) is used instead of Polly when present.

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

`app/demo_tools.py` (only with `DEMO_UI=true`, not in the Lambda package) writes a pretend "major disruption" LTA
response into the Redis cache that both the planner and the checker read, for live and saved data alike. `on` lasts
30 minutes unless you pass `--minutes`. A normal-day `check` finds nothing, because the checker does nothing until LTA
reports a disruption; trips on a different line, or already past the affected stretch, are not reported. The commands
are in the README.

## Changing a trip under way: bridging buses and re-routing

A trip keeps its route version (a disruption never changes the planned route); what changes is the trip's own plan:
`planSteps` (kept on the trip, not returned) and `planVersion` (+1 per change). The pack then follows the plan
(`packVersion` `R-007-v3-b2`, S3 `packs/R-007/v3-b2.json`), so the traveller app, which looks for a new pack every
minute, shows "Your trip has changed"; and `trip:{id}:route` is rewritten, so the detectors and the disruption
checker follow it. `override` on the trip is the latest change or proposal.

- **Free bridging buses** (`app/bridge.py`, wording in `easy_read.py`): during a breakdown LTA's TrainServiceAlerts
  lists, per broken stretch, the stations the free bridging buses call at (`FreeMRTShuttle`, going
  `MRTShuttleDirection`) and where ordinary buses are free (`FreePublicBus`). For a trip still to ride through the
  stretch, the train step is replaced by: the train to the station before the stretch (if the bridging bus calls
  there), the free bridging bus to the station after it (or the traveller's own stop, if inside), and the train on.
  No bridging bus that way: an ordinary free bus between stops within 300 m of the two stations (LTA's BusRoutes).
  Neither: nothing is proposed, and the caregiver can plan a new route.
- **Held, then sent by itself**: the API looks every `BRIDGE_WATCH_S` (30; a Redis lock means one replica does each
  pass) and stores a `pending` proposal with `sendAt` = now + `BRIDGE_AUTO_SEND_S` (180). The caregiver can
  **Send now** (`POST /api/trips/{id}/bridge/send`) or **Don't send** (`/bridge/dismiss`, not sent later either);
  if they do neither, it is sent at `sendAt` (`sentBy: "auto"`). It is withdrawn if the breakdown ends first.
- **Choosing it yourself**: `POST /api/trips/{id}/bridge/use` works the bridging bus out and uses it at once, also for
  a trip not started yet (its plan changes; start_trip publishes it). A bridging bus the caregiver chose not to send
  can still be sent (`/bridge/send`) while its breakdown lasts; the watcher withdraws it when the breakdown ends. A
  route saved for a planned trip can be used for it too. The caregiver app asks before starting or making a trip on a
  route with a disruption, and before sending a bridging bus.
- **A whole new route** (`POST /api/trips/{id}/replan` -> options, `/replan/apply {steps}` -> sent): from the
  traveller's newest check-in (`trip:{id}:recent`) if under 5 minutes old, else the start of their current step,
  to the route's destination. It replaces their steps from the current one on. Only ever the caregiver's choice.
  OneMap doesn't know about breakdowns, so options that ride through a broken stretch are left out (else the same
  ride comes back, and a bridging bus is proposed for it again). In the caregiver app the new route is planned on the
  Plan tab instead (from `location`'s `replanFrom` to the trip's destination) and saved like any route, with
  `forTripId` and a name starting "Re-route (T-…)", so its wording and photos are checked on Saved routes; once
  approved, `/replan/apply {routeId}` sends its own steps (wording, photos, landmarks: they go in the pack).
- **Planning during a breakdown**: every plan option says where it rides with no trains (`affected`, as for a trip)
  and LTA's free bus past it (`bridges`: the train to the station where the bus calls, the bus, the train on). They
  are shown, never saved: a trip gets a bridging bus only while trains are still not running. Those options are
  sorted last, and when the breakdown hits every option, bus-only routes are planned as well.
- **Following the traveller**: `GET /api/trips/{id}/location` gives their newest check-in (however old; `at` says
  when) and the last few as a trail, or before the first check-in where their step starts, with the step they are on.
  `GET /api/trips/{id}/plan` gives the steps they follow now (with a bridging bus or new route sent), each with its
  line on the map (`planPaths` on the trip: a re-route's own from OneMap, a bridging bus's cut from the train line it
  stands in for), and `affected`: where those steps ride with no trains running, from the station before the broken
  stretch to the one after it. The caregiver app's Trips tab polls the location every 5 s and the plan every 30 s
  (and when `planVersion` changes) and draws the trip on its own map: the current route with the affected part in
  amber, the traveller, their next stop, and a header naming the trip; a new route being looked at can be switched
  to and back. Clicks there don't change the planner's From and To.
- **The caregiver is told** at each step: on the Trips tab (a card with the countdown, the new steps with their
  photos, **Send now** / **Don't send**, and **Plan a new route**; trips are looked at every 15 s while one is under
  way), and as a message on the `careroute-disruptions` queue ("…will be sent to them at 08:03…", "…has been sent the
  free bridging bus (automatically…)"), which the Alert Service shows as the trip's disruption alert: the first
  message opens it, later ones for the same trip and step update its text (checked against alert-service).
