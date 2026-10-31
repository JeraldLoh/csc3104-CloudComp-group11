# Redis keys

Redis holds **live, fast-changing data**. Anything here can be rebuilt from new check-ins or the database if Redis restarts. Permanent data goes in DynamoDB.

Journey keys expire **24 hours** after their last update.

| Key | Type | Contents | Written by | Read by |
|---|---|---|---|---|
| `active-trips` | Set | Trip IDs currently in progress | Journey Service (M6), on start/end | Heartbeat (M1), Status page (M3) |
| `trip:{tripId}:route` | String (JSON) | Cached steps and path for the trip (same shape as `route.json` steps + `path`) | Journey Service (M6), on trip start | Detectors (M4) |
| `trip:{tripId}:context` | Hash | `travellerId`, `stepIndex`, `routeVersion`, `status` | Check-in Service (M2) | Detectors (M4), Coordinator (M3), Alert Service (M5) |
| `trip:{tripId}:lastseq` | String | Highest sequence number processed | Check-in Service (M2) | Check-in Service (M2) |
| `trip:{tripId}:recent` | List | Last 10 check-ins (JSON), newest first | Check-in Service (M2) | Detectors (M4), Alert Service (M5) |
| `trip:{tripId}:arrived` | String | When the traveller pressed "I've arrived"; set once, so only the first request tells caregivers | Alert Service (M5) | Alert Service (M5) |
| `hb:{tripId}:lastseen` | String | Time of last check-in | Check-in Service (M2) | Heartbeat (M1), Alert Service (M5) |
| `hb:{tripId}:intervals` | List | Last 100 gaps between single live check-ins, in milliseconds (catch-up batches don't add gaps; they only update lastseen) | Check-in Service (M2) | Heartbeat (M1) |
| `hb:{tripId}:status` | String | `active` or `backgrounded` | Check-in Service (M2) | Heartbeat (M1) |
| `risk:{travellerId}` | Hash | `score`, `level` (`normal`/`elevated`), `detectorsUsed`, `updatedAt` | Coordinator / Aggregator (M3) | Coordinator (M3), Status page (M3) |
| `detector:weights` | Hash | `detectorId` → voting weight | Aggregator (M3) | Aggregator (M3) |
| `lock:checkin-reconciler` | String | Leader lease: which Check-in Service replica runs the clean-up task (pod name), 45 s | Check-in Service (M2) | Check-in Service (M2) |
| `lock:escalation:{incidentId}` | String | Short lock so only one replica escalates an incident | Alert Service (M5) | Alert Service (M5) |

## Pub/Sub channel

| Channel | Message | Published by | Listened to by |
|---|---|---|---|
| `alerts:events` | `alert-event.json`, `arrival-event.json` | Alert Service (any replica) | Alert Service (every replica, forwards to connected dashboards) |
