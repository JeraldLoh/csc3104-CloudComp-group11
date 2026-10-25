# Demo: Journey Service + caregiver web app

A script for showing the whole thing working together: the **caregiver web app** (React, `apps/caregiver`)
talking to the **Journey Service** (FastAPI, `services/journey-service`) with Redis, DynamoDB, S3 and SQS behind
it. All commands are Windows PowerShell, run from the repo root (`csc3104-CloudComp-group11`). Everything runs on
your own machine; nothing touches AWS.

**The story:** a caregiver plans a trip for a traveller, tailors the wording the traveller will read, approves it,
makes a trip and prepares the traveller's offline pack. Then an MRT line breaks while the traveller is on the way,
and the system notices and raises an alert. Along the way: editing a route stops trips made on the old version,
two caregivers can't overwrite each other, and a stranger can't see or change anything.

**Data:** by default the service runs on **saved API data** (real OneMap and LTA responses captured earlier, in
`services/journey-service/app/saved_data/`). It needs no OneMap token and no internet for the data, so an expired
token can't break the demo. The real code still runs on that data. Only the nine saved trips (section 3) can be
planned. To use the live APIs, switch the header's **Data** box to **Live** (section 2).

> **Status of this guide.** The steps and the texts you should see were taken from the code and from
> `services/journey-service/DEMO.md`, and the web app's behaviour is covered by its 88 automated tests
> (`npm test`). The two halves have **not yet been run together end to end** by whoever wrote this guide, and the
> app hasn't been looked at in a real browser. Do one dry run before presenting and fix anything that differs.

---

## 1. One-time setup

You need: Docker Desktop, Node.js 22 or newer, and the root `.env` with `LOCALSTACK_AUTH_TOKEN` (never commit it).
No OneMap or LTA keys are needed to run on saved data, but the saved data is gitignored: on a fresh clone, capture it
once (this one step needs `LTA_API_KEY` and the OneMap account, `ONEMAP_API_EMAIL` / `ONEMAP_API_PASSWORD`, in `.env`).

```powershell
# 0. Fresh clone only: capture the saved data into services/journey-service/app/saved_data
cd services/journey-service; python scripts/capture_data.py --target both --routes live; cd ../..

# 1. Start Docker Desktop, then the backend (Redis and LocalStack start with it):
docker compose --env-file .env -f services/journey-service/compose.journey.yaml up --build -d journey-service

# 2. Install the web app:
cd apps/caregiver
npm install
cd ../..
```

> If `npm install` stops with `Cannot read properties of null (reading 'edgesOut')`, your npm is too old.
> Use `npx npm@latest install` instead.

## 2. Before every demo: start and check

```powershell
# Is the backend up? (give LocalStack about 20 seconds after starting)
curl.exe -s http://localhost:8006/ready

# Start the web app (leave this window open):
cd apps/caregiver
npm run dev
```

Open **http://localhost:5174**. You should see the header **CareRoute caregiver** with *Act as* `G-DEMO` and
*Traveller* `U-DEMO`, a **Data: Saved** box (with the date the data was saved), the planner on the left and the
OneMap map on the right. Switch **Data** to **Live** to use the real OneMap and LTA APIs straight away, no restart
(needs the keys in `.env`); switch back to **Saved** for the scripted demo. The box only appears when the service runs
with `DEMO_UI=true`, as compose does.

The web app forwards every `/api/...` call to the service on port 8006 (`apps/caregiver/vite.config.ts`; set
`JOURNEY_URL` if yours differs), so there is no CORS setup.

## 3. Demo A: plan a route on the map

The saved data covers these trips, all between MRT stations. Typing a place only finds these stations, and a
click on the map only plans if both ends are within 150 m of a saved station.

| Area | Trips |
|---|---|
| North | Ang Mo Kio to Toa Payoh |
| North-east | Punggol to Orchard, Sengkang to Serangoon, Hougang to Dhoby Ghaut |
| East | Tampines to Changi Airport, Pasir Ris to Bugis, Bedok to Paya Lebar |
| South | HarbourFront to Marina Bay, Tiong Bahru to Paya Lebar |

1. In **From** type `Punggol`, press **Enter**. The first match is pinned and the map jumps there.
2. In **To** type `Orchard`, press **Enter**.
3. Leave **Travel by** on *Bus or train* and press **Plan route**.

**You should see:** "N options found. Click one to see it on the map." (under **Plan**, with the options listed in their own **Options** card and the steps of the chosen one in **Steps**), a button per option such as
`Option 1: 38 min, walk + train, walk 249 m, quiet, $1.69` (your numbers will differ), and the first option drawn on the map. Click
the other options to compare.

**Point out:**
- Walking is a dashed grey line, buses blue, trains red. Click a step marker on the map for its instruction.
- Each step is in plain words ("Get on and tap your card. Get off at ... after 5 stops").
- Under bus steps, the next bus times from LTA (`Next bus 22 at stop 54009: 10 min (SEA), 25 min`). With saved
  data these were recorded earlier and shifted to now.
- **Sort by** (*Least walking*, *Least crowded*, *Fastest*) and **Travel by** (*Bus only*, *Train only*):
  change one and press **Plan route** again to see the options change.
- The pins: click the map to place *From*, then *To* (a third click starts again). **Clear** resets everything.

Quick links that plan straight away (handy when you want to skip typing):

- MRT: `http://localhost:5174/?from=1.4043,103.9021&to=1.3040,103.8318&mode=train`
- Bus with bus times: `http://localhost:5174/?from=1.3700,103.8486&to=1.3326,103.8474&mode=bus`

Routes saved from those links get the names "Start" and "Destination" because only coordinates were given. For
the rest of the demo, **type the places** (step 1 and 2) so the saved route is called *Punggol Mrt Station to
Orchard Mrt Station*.

**A crowded platform** (the saved data is all quiet, so pretend one):

```powershell
$t = "docker exec careroute-journey-service-1 python -m app.demo_tools"
iex "$t crowd on --stations NE17 --level high"   # Punggol MRT platform is very crowded (30 minutes)
# plan Punggol to Orchard again: "Platform at NE17: very busy" under the train step, and "very busy" on the option
iex "$t crowd off"
```

## 4. Demo B: save, approve, make a trip, get the route pack

Plan **Punggol to Orchard** with **Travel by: Train only** (the disruption demo needs an MRT trip), then:

1. In the **Save this option** card, name it (it suggests *Punggol Mrt Station to Orchard Mrt Station*) and press
   **Save route**. *You should see:* "Saved as R-XXXXXX (version 1, not approved yet)." The panel switches to the
   **Saved routes** tab and the route appears there
   with a *Not approved* badge. (It is saved for the traveller in the header's *Traveller* box, `U-DEMO`, by the
   caregiver in *Act as*, `G-DEMO`.)
2. Press **Approve** on that row. *You should see:* "R-XXXXXX approved (version 1)." and the badge turns to *Approved*.
3. Open the **Trips** tab. In **New trip**, the *Make a trip on* box now offers the route (unapproved routes never appear). Leave the day
   as today and press **New trip**. *You should see:* "Trip T-XXXXXX planned for 2026-… on route R-XXXXXX (version 1)."
4. Press **Start** on the trip. *You should see:* "Trip T-XXXXXX is now active." and the badge turns to *active*.
5. Press **Pack**. *You should see:* "Pack ready (link valid for an hour): R-XXXXXX-v1". Open the link: that JSON
   is the offline file the traveller's phone downloads (short steps, coordinates, standard help messages), served
   from S3 through a temporary signed link.
6. The trip also shows a **traveller link** (`http://localhost:5173/?trip=T-XXXXXX&traveller=U-DEMO`) with a
   **Copy** button. That is the link the caregiver sends to the traveller's phone (see section 9).

**Show what the rest of the system sees** (the detectors and the disruption checker read these):

```powershell
docker exec careroute-cloud-redis-1 redis-cli SMEMBERS active-trips          # the trip is listed while active
docker exec careroute-cloud-redis-1 redis-cli KEYS "trip:*:route"            # and its cached route
```

Leave the trip **active** for Demo D. Pressing **End** removes it from both.

## 5. Demo C: editing a route (and why that is safe)

Open the saved route: on the **Saved routes** tab, click its row. It is drawn on the map (planning again shows the plan
instead; clicking the route name brings it back) and its steps are listed with the easy-read wording the traveller
will see.

1. Press **Edit**. A yellow note says saving makes a new version and clears the approval, and that
   **1 planned or active trip on this route will stop working** (the trip from Demo B).
2. Change the wording of step 1, for example *Walk to the bus stop by the playground.* Empty a step to get the
   service's own automatic wording back. **Save changes** stays greyed out until something changes.
3. Press **Save changes**. *You should see:* "Saved version 2 of ... It needs approving again." and the row says *v2, Not approved*.
4. Open the **Trips** tab: the trip now has a warning, "The route was changed after this trip was made. Approve it,
   then make a new trip.", and its **Start**, **Pack** and traveller link are gone. (The service would refuse
   them anyway: a trip is pinned to the version it was made with, so a journey already under way can never change
   under the traveller.)
5. **Approve** version 2 and press **New trip** again. Start it. This is the trip Demo D needs.

**Two caregivers at once.** Open **http://localhost:5174** in a second browser tab. In both tabs open the same
saved route and press **Edit**. Save a change in tab 1. Now save a different change in tab 2:
*"This route was changed by someone else. Reload it and try again."* A **Reload this route** button appears and
your typing stays until you press it. Nothing was overwritten.

**Deleting a route.** On the **Saved routes** tab press **Delete** on a row. A red box asks first, and says how many
planned trips on that route will stop working. **Yes, delete** removes it; **Keep it** changes nothing. A route with an
active trip can't be deleted (end the trip first). Planned trips left behind are flagged "The route was deleted
after this trip was made" on the **Trips** tab. Only the route's caregiver can delete (a traveller gets "Only the
route's caregiver can delete it."). Don't delete the route Demo D needs.

## 6. Demo D: an MRT disruption is detected

Real MRT breakdowns are rare, so this pretends LTA reports one. You need an **active trip on an MRT route** (the
Punggol to Orchard *Train only* trip from Demo B or C).

```powershell
$t = "docker exec careroute-journey-service-1 python -m app.demo_tools"
iex "$t on"                          # pretend the whole North East Line is down (30 minutes)
# or just a stretch:  iex "$t on --stations NE10,NE11,NE12"
```

1. **Plan the same trip again** in the web app (*Train only*): the train step now has a **red warning**
   ("DEMO: no train service on the NEL...") under the steps, and an orange marker on the map.
2. **See what the alert system would send, without sending it:** `iex "$t check --dry-run"`
3. **Send it for real** (this is what the disruption Lambda does every minute in AWS): `iex "$t check"`. It
   prints the message for the traveller's trip (which trip, which stations).
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
'@ | docker exec -i careroute-journey-service-1 python -
```

5. **Run `iex "$t check"` again:** nothing new is sent, because the traveller was already told (no repeated alerts).
   `iex "$t check --again"` sends it once more, handy for a second showing.
6. **Switch it off:** `iex "$t off"`. Planning goes back to the normal feed (no warning).

Trips on a different line, trips already past the broken stretch, and minor delays are not alerted (only LTA's
"disrupted" status counts), so travellers aren't alarmed for nothing.

## 7. Demo E: who can see what (logins)

Real logins (Cognito) aren't set up yet, so this uses a stand-in: the service trusts an `X-Dev-User` header, which
the web app's **Act as** box sends. **Local only.** Switch the service into dev mode:

```powershell
$env:AUTH_MODE = "dev"
docker compose --env-file .env -f services/journey-service/compose.journey.yaml up -d journey-service
```

Only the service restarts; LocalStack and Redis keep the routes and trips you made earlier, so refresh the web app
and they are still there. Then try:

| Act as | Traveller | What to do | What happens |
|---|---|---|---|
| `G-DEMO` (the caregiver) | `U-DEMO` | the whole demo above | works |
| `G-OTHER` (another caregiver) | `U-DEMO` | look at the **Saved routes** and **Trips** tabs | "Nothing saved for this traveller yet." It isn't theirs |
| `U-DEMO` (the traveller) | `U-DEMO` | open the route, press **Approve** (on an unapproved one) | "Only the route's caregiver can approve it." |
| `U-DEMO` | `U-DEMO` | open the route, **Edit**, **Save changes** | "Only the route's caregiver can change it." |
| `U-DEMO` | `U-DEMO` | **Delete** a route, then **Yes, delete** | "Only the route's caregiver can delete it." |
| `U-DEMO` | `U-DEMO` | **Start** / **End** their own trip, **Pack** | works |
| `hacker` | `U-DEMO` | anything | "Missing or invalid login token" |

A stranger gets the same "not found" as for a route that doesn't exist, so they can't even tell it's there.

**Switch it back off** afterwards:

```powershell
Remove-Item Env:AUTH_MODE
docker compose --env-file .env -f services/journey-service/compose.journey.yaml up -d journey-service
```

## 8. Starting clean between demos

```powershell
iex "$t off"                                                    # stop pretending (if $t is not set: see Demo D)
docker exec careroute-cloud-redis-1 redis-cli FLUSHALL            # clears the cache and any active trips (your local Redis only)
docker compose --env-file .env -f services/journey-service/compose.journey.yaml restart cloud-localstack
# wait about 20 seconds for LocalStack, then:
docker compose --env-file .env -f services/journey-service/compose.journey.yaml restart journey-service
```

LocalStack's free edition forgets all saved routes, trips and packs when it restarts, and the service recreates the
empty tables when it starts. (If you restart only LocalStack, `/ready` says `routes table: ResourceNotFoundException`
until you restart the service.) After a reset, refresh the web app.

## 9. Optional: the traveller's side

The traveller's phone app (`apps/traveller`, React) lives on the **`chloe-traveller-app`** branch and is **not in this
branch**, so it is not part of this demo. To show it, check that branch out in a second working copy, run it with
`npm run dev` (port 5173), and open the **traveller link** from step 6 of Demo B. Its dev server forwards
`/api/trips` to port 8006, so it reads the same pack. Be ready for two honest caveats:

- The pack builder is partly done: the pack has every step's easy-read wording, the path and the standard messages,
  but the traveller and caregiver **names and phone are null**, and **photos and audio are not produced yet**
  (`photoUrl` / `audioUrl` are null). Check that the traveller app accepts this before
  promising it works.
- Starting the trip from the traveller's phone calls the same Start endpoint the caregiver's **Start** button
  does, and is safe to repeat.

## 10. If something goes wrong

| What you see | Why | Fix |
|---|---|---|
| The app shows "Can't reach the server. Check the connection and try again." | the service isn't running, or isn't on port 8006 | `curl.exe -s http://localhost:8006/ready`; start it (section 1). Using another port? Set `JOURNEY_URL` before `npm run dev` |
| Plan says "That trip is not in the saved demo data. Saved trips: ..." | saved-data mode and a trip that wasn't recorded | use one of the nine trips (section 3), or switch to live data |
| "No places found for ..." | saved data only knows the 17 stations | type a station (`Punggol`, `Orchard`, ...) |
| Plan says "Route planning is not available" | live data and the OneMap login failed or the token expired | go back to saved data (`Remove-Item Env:ONEMAP_MODE, Env:LTA_MODE`, then the `up -d` command) |
| "Missing or invalid login token" | the service is in dev mode and **Act as** is empty or wrong | type `G-DEMO` in **Act as**, or switch dev mode off (section 7) |
| Saved routes and trips are gone | LocalStack was restarted | plan and save again (section 8) |
| "Pack" says the pack can't be prepared | the pack bucket is missing or LocalStack is down | `docker ps`; restart (section 8) |
| `check` finds nothing | no **active** trip on an MRT route, or the pretend disruption is off or on another line | start a trip first; `iex "$t on"` |
| A trip shows "The route was changed after this trip was made" | the route was edited after the trip was made | approve the new version and press **New trip** (section 5) |
| The map is blank or has no tiles | no internet (map tiles come from OneMap) | check the connection |
| `npm run dev` says the port is in use | something else is on 5174 | stop it, or `npm run dev -- --port 5180` |

## 11. What this demo does not show

- **Real logins** (Cognito): dev mode stands in for them. The user pool and token fields are still to be agreed with the team.
- **Live data by default:** the default is saved data. Live is the same code with the real APIs.
- **Running on AWS:** the deployment files exist (`services/journey-service/deploy/`) but nothing has been deployed.
- **AI wording:** the plain-language steps come from fixed rules, not an AI, so they can't make facts up. The caregiver can rewrite any step by hand (Demo C).
- **Replacing a route's steps with a new plan**, and a **live dashboard of travellers**: not built yet. Today a different journey is a new route.
- **Names in the route pack** (traveller, caregiver) are empty until the Profiles service exists.
