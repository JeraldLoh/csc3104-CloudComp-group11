# Journey Service demo

A script for showing what the Journey Service does, with every command to type. All commands are Windows PowerShell,
run from the repo root (`csc3104-CloudComp-group11`). Everything runs on your own machine (Docker, LocalStack, Redis);
nothing touches AWS.

**By default the demo runs on saved API data** (real OneMap and LTA responses captured earlier, kept in
`app/demo_data/`). It needs no OneMap token and no internet, so the expiring token can't break it. The real code still
runs on that data: parsing, planning, the plain-language wording, the LTA cross-check, saving, trips, packs and the
disruption steps. Only the nine saved trips (below) can be planned, and bus times are moved to "now". To use the live
APIs instead, see "Live data" at the end of section 2.

**The story:** a caregiver plans a trip for a traveller, approves it, the traveller's phone gets an offline "route pack",
and when an MRT line breaks while the traveller is on the way, the system notices and raises an alert.

## 1. One-time setup

1. Start **Docker Desktop**.
2. The root `.env` needs `LOCALSTACK_AUTH_TOKEN` (never commit it). For live data it also needs `LTA_API_KEY` and your OneMap account (`ONEMAP_API_EMAIL`, `ONEMAP_API_PASSWORD`).
3. Start everything (Redis and LocalStack start with it):

```powershell
docker compose -f compose.yaml -f services/journey-service/compose.journey.yaml up --build -d journey-service
```

## 2. Before every demo: check it is up

Open **http://localhost:8006/demo**. If the page loads, the service is up; the yellow banner says it is using saved data
(`GET /demo/info` says the same). If it doesn't load, start Docker Desktop and run the `up` command from section 1
(`docker ps` lists the containers; give LocalStack about 20 seconds).

### Live data (optional)

Only if you want the real APIs, for example to show a trip that isn't saved. With your OneMap account in `.env`
(`ONEMAP_API_EMAIL`, `ONEMAP_API_PASSWORD`) the service logs in and renews its own token every 3 days, so nothing
expires under you (without the account, a pasted `ONEMAP_API_KEY` lasts 3 days and then planning answers 502).
To switch to live, set both modes and recreate the service:

```powershell
$env:ONEMAP_MODE = "live"; $env:LTA_MODE = "live"
docker compose -f compose.yaml -f services/journey-service/compose.journey.yaml up -d journey-service
```

(`Remove-Item Env:ONEMAP_MODE, Env:LTA_MODE` and the same `up -d` go back to saved data.)

## 3. Demo A: plan a route on the map

Open **http://localhost:8006/demo**. A yellow banner says it is using saved data, with one button per saved trip
(between MRT stations):

| Area | Trips |
|---|---|
| North | Ang Mo Kio to Toa Payoh |
| North-east | Punggol to Orchard, Sengkang to Serangoon, Hougang to Dhoby Ghaut |
| East | Tampines to Changi Airport, Pasir Ris to Bugis, Bedok to Paya Lebar |
| South | HarbourFront to Marina Bay, Tiong Bahru to Paya Lebar |

Click one (it fills From and To and plans), then try *Bus only* / *Train only* under **Travel by** and press
**Plan route** again.

- Or type the places (*Tampines* and *Changi Airport*, *Punggol* and *Orchard*, ...) and press Enter in each box.
- Or open a ready-made link: an MRT trip
  `http://localhost:8006/demo?from=1.4043,103.9021&to=1.3040,103.8318&mode=train`, or a bus trip with bus times
  `http://localhost:8006/demo?from=1.3700,103.8486&to=1.3326,103.8474&mode=bus`.
- Any other trip says so and lists the saved ones ("That trip is not in the saved demo data...").
  With live data (section 2) any trip works, for example *Tampines Mall* to *Changi City Point*; to save more trips for
  the offline demo see "Saved-data mode" in `README.md`.

**Point out:** up to 3 options; walking is a dashed grey line, buses blue, trains red; each step is in plain words
("Get on and tap your card. Get off at ... after 5 stops"); under bus steps, the next bus times from LTA
(`Next bus 22 at stop 54009: 10 min (SEA), 25 min`). With saved data these were recorded earlier and shifted to now.

### Walking, crowding and sorting

Try these on the same page:

- **Sort options by** (above *Travel by*): *Least walking* (default), *Least crowded* or *Fastest*. Plan again to see the
  order change. Each option shows its walking and how busy it is ("walk 249 m, quiet").
- **Too much walking:** `http://localhost:8006/demo?from=1.4043,103.9021&to=1.3040,103.8318&mode=train` has about 250 m
  of walking, which is fine by default (500 m per walk). To see the warnings, call the API with `maxWalkM` at its minimum, which gives a
  `long_walk` and a `long_total_walk` warning:
  ```powershell
  curl.exe -s -X POST http://localhost:8006/api/routes/plan -H "Content-Type: application/json" -d '{\"from\":{\"lat\":1.4043,\"lng\":103.9021},\"to\":{\"lat\":1.3040,\"lng\":103.8318},\"mode\":\"train\",\"maxWalkM\":100}'
  ```
- **A crowded platform** (the saved data is all quiet, so pretend one; like the pretend disruption, it only changes
  what the planner reads):

```powershell
$t = "docker exec csc3104-journey-service-1 python -m app.demo_tools"
iex "$t crowd on --stations NE17 --level high"   # Punggol MRT platform is very crowded (30 minutes)
# plan Punggol to Orchard again: "Platform at NE17: very busy", "Punggol MRT Station is very crowded now." and "very busy" on the option
iex "$t crowd off"
```

With saved data the buses are real captures (mostly seats available), so a crowded *bus* is only shown if one was
saved with standing room; the tests cover a full bus.

## 4. Demo B: save, approve, start a trip, get the route pack

On the same page, after planning:

1. **Save route** (the panel under the steps). It appears under **Saved routes** as *not approved*.
2. **Approve** it. (A traveller can't; see Demo D.)
3. **New trip today**, then **Start** in the **Trips** list. The trip is now *active*.
4. **Pack**, then open the link that appears. That is the offline file the traveller's phone downloads
   (short steps, coordinates, standard help messages), served from S3 through a temporary signed link.

**Show what the rest of the system sees** (the detectors and the disruption checker read these):

```powershell
docker exec csc3104-cloud-redis-1 redis-cli SMEMBERS active-trips          # the trip is listed while active
docker exec csc3104-cloud-redis-1 redis-cli KEYS "trip:*:route"            # and its cached route
```

Leave the trip **active** for Demo C. Pressing **End** removes it from both.

## 5. Demo C: an MRT disruption is detected

Real MRT breakdowns are rare, so this pretends LTA reports one. You need an **active trip on an MRT route** (Demo B with
*Train only*, e.g. the saved Punggol to Orchard trip).

```powershell
$t = "docker exec csc3104-journey-service-1 python -m app.demo_tools"
iex "$t on"                          # pretend the whole North East Line is down (30 minutes)
# or just a stretch:  iex "$t on --stations NE10,NE11,NE12"
```

1. **Plan the same trip again** on the map page: the MRT step now has a **red warning**
   ("DEMO: no train service on the NEL...") and an orange marker on the map.
2. **See what the alert system would send, without sending it:**
   `iex "$t check --dry-run"`
3. **Send it for real** (this is what the disruption Lambda does every minute in AWS):
   `iex "$t check"`: it prints the message for the traveller's trip (which trip, which stations).
4. **See it waiting on the queue** the Alert Service reads (`careroute-disruptions`), without consuming it:

```powershell
@'
import json, os, boto3
sqs = boto3.client("sqs", region_name=os.environ["AWS_DEFAULT_REGION"], endpoint_url=os.environ["AWS_ENDPOINT_URL"])
url = sqs.get_queue_url(QueueName="careroute-disruptions")["QueueUrl"]
msgs = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, VisibilityTimeout=0).get("Messages", [])
print(len(msgs), "message(s) waiting on careroute-disruptions")
for m in msgs:
    b = json.loads(m["Body"]); print(" -", b["disruptionId"], "| trip", b["tripId"], "| traveller", b["travellerId"], "|", ", ".join(b["affectedStations"]))
'@ | docker exec -i csc3104-journey-service-1 python -
```

5. **Run `iex "$t check"` again:** nothing new is sent, because the traveller was already told (no repeated alerts).
   `iex "$t check --again"` sends it once more, handy for a second showing.
6. **Switch it off:** `iex "$t off"`. Planning goes back to the real LTA feed (no warning).

Trips on a different line, or already past the broken stretch, are not alerted, and neither are minor delays (only LTA's
"disrupted" status counts), so travellers aren't alarmed for nothing.

## 6. Demo D: who can see what (logins)

Real logins (Cognito) aren't set up yet, so this uses a stand-in: the service trusts an `X-Dev-User` header. **Local
only.** Switch it on, and the page's **Act as** box (top left) becomes the login:

```powershell
$env:AUTH_MODE = "dev"
docker compose -f compose.yaml -f services/journey-service/compose.journey.yaml up -d journey-service
```

Then, on the demo page:

| Act as | What to do | What happens |
|---|---|---|
| `G-DEMO` (the caregiver) | plan, **Save route** for traveller `U-DEMO`, **Approve** | works |
| `G-OTHER` (another caregiver) | look at **Saved routes** for `U-DEMO` | nothing there: it isn't theirs |
| `U-DEMO` (the traveller) | open the route, click **Approve** | "Only the route's caregiver can approve it." |
| `U-DEMO` | **Start** / **End** their own trip, **Pack** | works |
| `hacker` | anything | "Missing or invalid login token" |

Or from the command line (use a route id from the page):

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" http://localhost:8006/api/routes/R-XXXXXX                            # 401, no login
curl.exe -s -o NUL -w "%{http_code}`n" -H "X-Dev-User: G-DEMO"  http://localhost:8006/api/routes/R-XXXXXX   # 200, the caregiver
curl.exe -s -o NUL -w "%{http_code}`n" -H "X-Dev-User: U-DEMO"  http://localhost:8006/api/routes/R-XXXXXX   # 200, the traveller
curl.exe -s -o NUL -w "%{http_code}`n" -H "X-Dev-User: G-OTHER" http://localhost:8006/api/routes/R-XXXXXX   # 404, a stranger
```

A stranger gets the same "not found" as for a route that doesn't exist, so they can't even tell it's there.

**Switch it back off** afterwards:

```powershell
Remove-Item Env:AUTH_MODE
docker compose -f compose.yaml -f services/journey-service/compose.journey.yaml up -d journey-service
```

## 7. Demo E: the disruption Lambda on its own

```powershell
docker exec csc3104-journey-service-1 python -m app.lambda_handler
```

It prints a summary like `{'activeTrips': 1, 'disruptions': 1, 'sent': 1, ...}`. On a normal day it's `activeTrips: 0`:
the checker does nothing until LTA reports a disruption. In AWS this runs every minute on a schedule; the files to deploy
it are in `deploy/` (not run on real AWS yet, see `deploy/README.md`).

## 8. Starting clean between demos

```powershell
iex "$t off"                                                    # stop pretending (if $t is not set: see Demo C)
docker exec csc3104-cloud-redis-1 redis-cli FLUSHALL            # clears the cache and any active trips (your local Redis only)
docker compose -f compose.yaml -f services/journey-service/compose.journey.yaml restart cloud-localstack
# wait about 20 seconds for LocalStack, then:
docker compose -f compose.yaml -f services/journey-service/compose.journey.yaml restart journey-service
```

LocalStack's free edition forgets all saved routes, trips and packs when it restarts, and the service recreates the empty
tables when it starts. (If you restart only LocalStack, `/ready` says `routes table: ResourceNotFoundException` until
you restart the service.)

## 9. If something goes wrong

| What you see | Why | Fix |
|---|---|---|
| Planning says "That trip is not in the saved demo data" | saved-data mode and a trip that wasn't recorded | use a preset button, or switch to live data (section 2) |
| Planning says "Route planning is not available" | live data and the OneMap login failed (wrong `ONEMAP_API_EMAIL` / `ONEMAP_API_PASSWORD`), or only a pasted token that expired (3 days) | fix the account in `.env` and recreate the service (section 2), or go back to saved data |
| `/ready` says `routes table: ...` or `pack bucket: ...` | LocalStack was restarted and lost its tables | restart the service (section 8) |
| The page says "Missing or invalid login token" | the service is in dev mode (`AUTH_MODE=dev`) and **Act as** is empty or wrong | put `G-DEMO` in **Act as**, or switch dev mode off (Demo D) |
| "Pack" says the pack can't be prepared | the pack bucket is missing or LocalStack is down | check `docker ps`; restart (section 8) |
| `check` finds nothing | no **active** trip on an MRT route, or the pretend disruption is off or on another line | start a trip first; `iex "$t on"` |
| `demo_tools` says "Refusing to run" | `DEMO_UI` isn't true | it is set by `compose.journey.yaml`; recreate the service |
| Page tiles or map don't load | no internet (map and library files come from the internet) | check the connection |

## 10. What this demo does not show

- **Real logins** (Cognito): dev mode stands in for them. The user pool and the token fields are still to be agreed with the team.
- **Live data by default:** the default is saved data (section 2 switches to live). Live is the same code with the real APIs.
- **Running on AWS:** the deployment files exist but nothing has been deployed (it costs money); everything above runs locally.
- **AI wording:** the plain-language steps come from fixed rules, not an AI, so they can't make facts up. An AI rewrite is a possible later step.
- **Names in the route pack** (traveller, caregiver) are empty until the Profiles service exists.

---

*Checked on 2026-10-24 on saved data, with the pasted OneMap token already expired: the page loading, planning (no outbound network
connection was made), saving, approving, trips, packs, the disruption steps (`on`, re-plan, `check`, `off`), the
Lambda handler, and the demo page (banner, preset buttons). Dev-mode logins and the reset commands were checked
earlier the same day on live data; saved-data mode doesn't change them.*
