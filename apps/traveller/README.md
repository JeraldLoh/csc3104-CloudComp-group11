# Traveller app (Member 2, issues 2.1 and 2.2)

The web app the traveller follows on their phone: one easy-read step at a time, a Help
button that always works, and a background check-in sender that never loses or reorders
a check-in, even with no signal.

React + TypeScript + Vite. Contracts: `contracts/examples/checkin.json`,
`checkin-batch.json`, `route-pack.json`.

## Run it

You need Node.js 20 or newer.

```powershell
cd apps/traveller
npm install
npm run dev
```

Open **http://localhost:5173/?demo=1&debug=1**.

- `?demo=1` uses a sample route and a pretend GPS that walks along it, so it works on
  a laptop and before the Journey Service (6.1) exists.
- `?debug=1` shows the debug panel: outbox size, sent, duplicates, retries, last error,
  route version, offline storage, plus three demo controls:
  - **Pretend no signal**: check-ins pile up in the outbox, then go as one batch when switched off.
  - **Change route** (during a trip): pretends the caregiver changed the route (bus 190 → bus 83).
  - **Reset demo**: forgets the demo trip on this device and goes back to "Hi Alex". Only works
    with `?demo=1`, so a real trip (which may hold unsent check-ins) can never be wiped.
- A real trip link looks like `/?trip=T-001&traveller=U-042` (until login, issue 1.5).

The dev server forwards `/api/checkins` to the Check-in Service on `localhost:8001`, so start
that first (see `services/checkin-service/README.md`). `/api/help` goes to port 8005 and
`/api/trips` to 8006; until those services exist, those requests wait in the queue and retry.
Change the ports with `CHECKIN_URL`, `ALERT_URL` and `JOURNEY_URL` environment variables.

**On a phone:** browsers only give GPS to `https` pages or `localhost`. For a quick test on
your phone, use Chrome on Android with USB debugging and port forwarding of 5173, or deploy
to Amplify (which gives https).

## Tests

```powershell
npm test          # 84 tests: outbox, sender, tracker, request queue, HTTP, offline, route updates, wake lock
npm run typecheck
```

## How check-ins get to the server

```
GPS (watchPosition) ──► latest position
                              │  every 10 s, on each step change, on Help,
                              │  and when the app goes to the background / comes back
                              ▼
          Outbox (IndexedDB)  ◄── stored FIRST, with its seq, in one transaction
                              │
                Sender ───────┘  one at a time (also across tabs: Web Locks)
                  │
   up to date ────┼──► POST /api/checkins          (1 check-in)
   behind ────────┴──► POST /api/checkins/batch    (up to 100, oldest first)
                  │
       202 ──► delete from outbox
       422 ──► drop it (a bad check-in must not block the rest), count as rejected
       401 ──► stop until login
   5xx / timeout / no network ──► keep it, mark it bufferedOffline,
                                   retry after exponential backoff with full jitter
```

| Problem | What the app does |
|---|---|
| No signal (tunnel, MRT underground) | Check-ins wait in the outbox, marked `bufferedOffline`. When the signal returns they go as **one batch**, in order. |
| Server down or slow | Same as no signal; retries back off 1 s, 2 s, 4 s … up to 60 s, at a random point in each window so thousands of phones don't retry at the same instant. |
| Reply lost after the server stored it | The retry has the same `seq`; the server answers "duplicate" and nothing is stored twice. |
| Tab closed, phone restarted | The outbox is in IndexedDB; whatever was waiting is sent when the app opens again. The trip and current step are remembered too. |
| App storage wiped mid-trip | `seq` restarts from the current time in seconds, above every seq already sent, so new check-ins aren't mistaken for old duplicates. |
| Phone locked / app in background | One check-in with `status: "backgrounded"` goes out with `keepalive` as the page hides, so the Heartbeat Monitor knows silence is expected. |
| Help pressed with no signal | The request is queued and retried, goes before anything else waiting, and can't be blocked by another failing request. The **Call Sam** button is there straight away and becomes the main action once the request has failed. |

## Working with no signal (issue 2.4)

| What | How it's kept on the phone |
|---|---|
| The app itself | A service worker (built by `vite-plugin-pwa`/Workbox) stores every built file. Opening the app with no signal shows the app, not the browser's offline page. Production builds only, so try it with `npm run build` then `npm run preview` (port 4173). |
| The route pack | IndexedDB. Loaded network-first so a route the caregiver changed is picked up, but **only replaced by a pack that passes `validatePack()`**: a cut-off or broken download can never overwrite a good saved pack. |
| Photos and audio | Downloaded into Cache Storage as soon as the trip opens, before the traveller sets off. Shown from there as `blob:` URLs, so they work with no signal. |
| Unsent check-ins | The outbox (see above). |

Details that matter:

- **Ready before leaving.** The start screen says "Your trip is saved. It works without signal." once everything is stored. It never blocks starting: the steps are text and work regardless, and a missing photo just shows the pictogram.
- **Only what's missing is downloaded again** when signal returns, so a flaky connection gets there in the end.
- **A Wi-Fi login page is never stored as a photo.** Public Wi-Fi often answers every request with its own HTML page and status 200; only image and audio responses are stored.
- **A download cut off half-way is never stored.** The whole file is read before it's saved.
- **Signed S3 links.** Each request gets a new signature, but the stored file is found by its address without the signature.
- **Route changes.** After the caregiver edits the route, photos the new route doesn't use are removed, but only once the new set is complete, so an old photo can still help in the meantime.
- **The browser is asked not to clear our storage** when the phone is low on space (`navigator.storage.persist()`).
- **Route changes reach the phone mid-trip.** Every minute, and straight away when signal
  returns or the traveller comes back to the app, it asks the Journey Service for the current
  `packVersion` (a tiny request). Only if the version changed is the new pack downloaded,
  validated and switched to. The traveller sees "Your trip has changed. Read this step again.",
  the screen scrolls to it, and the next check-in reports the new `routeVersion`, so the
  detectors judge against the new route at once. Try it with **Change route** in the debug panel.
- **Photos are stored per route version.** A photo is only shown next to the instructions of the
  version it was downloaded for: after a change, an old picture of bus 190 can never appear next
  to "Get on bus 83". Until the new photo has downloaded, the network copy or the pictogram is shown.
- **App updates never interrupt a journey.** A new version waits until every tab of the app is closed.
- **After arriving,** the trip's photos are deleted to free space and not keep pictures of the route longer than needed.
- **Layout.** The steps scroll in their own area above the Help bar, so a tap meant for "I've done this step" can never land on Help.

## Keeping the screen on (issue 2.2)

During a trip the app asks the browser to keep the screen on (Screen Wake Lock API). If the
screen turned off, the browser would treat the app as backgrounded and slow or stop GPS and
check-ins, and the traveller would have to unlock the phone to see the next step. The browser
drops the lock whenever the app is hidden, so it's requested again each time the app comes back,
and released on arrival. It costs some battery; check-ins carry `batteryPct`. Shown as
"Screen kept on" in the debug panel. (Headless test browsers always refuse it, so check it in
real Chrome.)

## Hosting on Amplify (issue 2.1)

`amplify.yml` and `customHttp.yml` at the repo root are already set up for this folder.

1. AWS console → **Amplify** → **Create new app** → **GitHub** → this repository and branch.
2. Tick **My app is a monorepo** and enter `apps/traveller`. Amplify picks up `amplify.yml`.
3. Deploy. You get an `https://….amplifyapp.com` address, which also gives the phone GPS access
   (browsers only allow GPS on https).
4. **Rewrites and redirects** (in the app's settings), add, in this order:
   - `/api/<*>` → `https://<load balancer address>/api/<*>`, type **200 (Rewrite)**. Amplify
     only proxies to **https** addresses, so the load balancer needs an HTTPS listener
     (an ACM certificate). Same origin, so no CORS needed.
   - The single-page app rule from the Amplify docs, so `/?trip=…` links always load the app:
     source `</^[^.]+$|\.(?!(css|gif|ico|jpg|js|png|txt|svg|woff|woff2|ttf|map|json|webp|webmanifest)$)([^.]+$)/>`,
     target `/index.html`, type **200 (Rewrite)**.
5. If the load balancer has no HTTPS yet: instead of the `/api` rewrite, set the environment
   variable `VITE_API_BASE` to the load balancer's address, redeploy, and set `CORS_ORIGINS` on the
   Check-in Service to the Amplify address.

`customHttp.yml` stops browsers caching `sw.js` and `index.html` (otherwise phones would keep an
old version of the app) and lets the hashed files in `/assets` be cached for a year.

## Files

| File | |
|---|---|
| `src/lib/outbox.ts` | IndexedDB outbox, atomic `seq` |
| `src/lib/sender.ts` | single-flight sending, batching, backoff, error handling |
| `src/lib/tracker.ts` | GPS, 10 s heartbeat, background / foreground |
| `src/lib/requests.ts` | durable queue for help, trip start, trip end |
| `src/lib/api.ts` | HTTP, timeouts, sorting responses into ok / invalid / unauthorized / retry |
| `src/lib/pack.ts` | loads and saves the route pack |
| `src/lib/offline.ts` | validates packs, downloads photos/audio, serves stored copies |
| `src/lib/sw-register.ts` | registers the service worker |
| `src/lib/wakelock.ts` | keeps the screen on during a trip |
| `src/config.ts` | API address (`VITE_API_BASE`) |
| `src/lib/demo.ts`, `netsim.ts` | demo route, pretend GPS, pretend no signal |
| `src/app/useCareRoute.ts` | wires it all together for the screens |
| `src/App.tsx`, `src/components/` | the screens |

## Design notes

Each step is shown like a transit sign: coloured by mode (walk, bus, train), one instruction
in large type (Atkinson Hyperlegible, designed for legibility, bundled so it works offline),
a Listen button (recorded audio from the pack, or the phone's voice). Red is used for Help
and nothing else, and the Help button never moves. Status messages are calm and plain:
"No signal. That's OK, keep going."

## Not done yet

- The pack builder (backend half of 2.4): waiting to agree the interface with the Journey Service owner.
- Login: issue 1.5. `HttpTransport` already takes a `getToken` function for it.
- Actually deploying to Amplify needs the team's AWS account (steps above).
