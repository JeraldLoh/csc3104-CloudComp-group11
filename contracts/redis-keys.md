# Redis keys

Redis holds **live, fast-changing data**. Anything here can be rebuilt from new check-ins or the database if Redis restarts. Permanent data goes in DynamoDB.

Journey keys expire **24 hours** after their last update.

| Key | Type | Contents | Written by | Read by |
|---|---|---|---|---|
| `active-trips` | Set | Trip IDs currently in progress | Journey Service (M6), on start/end | Heartbeat (M1), Status page (M3) |
| `trip:{tripId}:route` | String (JSON) | Cached steps and path for the trip (same shape as `route.json` steps + `path`) | Journey Service (M6), on trip start | Detectors (M4), Heartbeat (M1) (is the current step underground?) |
| `trip:{tripId}:context` | Hash | `travellerId`, `stepIndex`, `routeVersion`, `status` | Check-in Service (M2) | Detectors (M4), Coordinator (M3), Alert Service (M5), Heartbeat (M1) |
| `trip:{tripId}:lastseq` | String | Highest sequence number processed | Check-in Service (M2) | Check-in Service (M2) |
| `trip:{tripId}:recent` | List | Last 10 check-ins (JSON), newest first | Check-in Service (M2) | Detectors (M4), Alert Service (M5), Heartbeat (M1) (last location and battery) |
| `trip:{tripId}:dwell` | Hash | `lat`, `lng`, `since`: where the traveller stopped and when, so a stop longer than the `recent` window can be timed | Detectors (M4), dwell-time | Detectors (M4), dwell-time |
| `trip:{tripId}:arrived` | String | When the traveller pressed "I've arrived"; set once, so only the first request tells caregivers | Alert Service (M5) | Alert Service (M5) |
| `hb:{tripId}:lastseen` | String | Time of last check-in | Check-in Service (M2) | Heartbeat (M1), Alert Service (M5) |
| `hb:{tripId}:intervals` | List | Last 100 gaps between single live check-ins, in milliseconds (catch-up batches don't add gaps; they only update lastseen) | Check-in Service (M2) | Heartbeat (M1) |
| `hb:{tripId}:status` | String | `active` or `backgrounded` | Check-in Service (M2) | Heartbeat (M1) |
| `hb:{tripId}:reported` | String | The `lastseen` value of the silence already reported as `no_response`, so each silence is reported once | Heartbeat (M1) | Heartbeat (M1) |
| `risk:{travellerId}` | Hash | `score`, `level` (`normal`/`elevated`), `detectorsUsed`, `updatedAt` | Coordinator (M3) | Coordinator (M3), dashboard (M3) |
| `detector:weights` | Hash | `detectorId` → reliability, 0–1 (default 1.0); 0 silences a faulty detector | Set by hand (operator, experiment) | Coordinator (M3) |
| `decided:{tripId}:{seq}` | String | `1` once that check-in has been decided, so a redelivered SQS message isn't decided twice (expires after 24 h) | Coordinator (M3) | Coordinator (M3) |
| `decisions:{travellerId}` | List | Last 50 decisions for the traveller (JSON, newest first): votes, decision, risk before and after, incident | Coordinator (M3) | Coordinator dashboard and `/api/decisions` (M3), experiment |
| `decisions:recent` | List | Last 200 decisions for everyone (same JSON) | Coordinator (M3) | Coordinator dashboard and `/api/decisions` (M3), experiment |
| `lock:heartbeat-monitor` | String | Leader lease: which Heartbeat Monitor replica checks the trips (pod name), 15 s | Heartbeat (M1) | Heartbeat (M1) |
| `lock:checkin-reconciler` | String | Leader lease: which Check-in Service replica runs the clean-up task (pod name), 45 s | Check-in Service (M2) | Check-in Service (M2) |
| `lock:escalation:{incidentId}` | String | Short lock so only one replica escalates an incident | Alert Service (M5) | Alert Service (M5) |

## Pub/Sub channel

| Channel | Message | Published by | Listened to by |
|---|---|---|---|
| `alerts:events` | `alert-event.json`, `arrival-event.json` | Alert Service (any replica) | Alert Service (every replica, forwards to connected dashboards) |
