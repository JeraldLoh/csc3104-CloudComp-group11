# Contracts

**Status: DRAFT.** Review the parts that affect your service, comment on the pull request, and we merge by **Tue 13 Oct**.

This folder is how our services agree on what they send each other. If your code sends or receives a message, it must match the example here.

## What's in here

| File | What it describes |
|---|---|
| `examples/*.json` | One example of every message passed between services |
| `api.md` | Every endpoint, which service owns it, and what it takes and returns |
| `tables.md` | DynamoDB tables and their keys |
| `redis-keys.md` | Redis key names and who writes/reads them |
| `queues.md` | SQS queues: who sends, who receives |
| `problem-definitions.md` | When something counts as each problem type (DRAFT) |

## Owners

| Example file | Sent by | Received by |
|---|---|---|
| `checkin.json` | Traveller app (M2) | Check-in Service (M2) → SQS → Coordinator (M3) |
| `checkin-batch.json` | Traveller app (M2), after being offline | Check-in Service (M2) |
| `detector-request.json` | Coordinator (M3) | Detectors (M4) |
| `detector-vote.json` | Detectors (M4) | Coordinator (M3) → Aggregator (M3) |
| `problem-report.json` | Aggregator (M3) | Alert Service (M5) |
| `problem-report-no-response.json` | Heartbeat Monitor (M1) | Alert Service (M5) |
| `incident.json` | Alert Service (M5) | Stored in DynamoDB; shown on dashboard |
| `alert-event.json` | Alert Service (M5) | Caregiver dashboard (live, SSE) |
| `disruption.json` | Disruption Lambda (M6) | Alert Service (M5), via SQS |
| `route.json` | Journey Service (M6) | Dashboard, pack builder (M2) |
| `trip.json` | Journey Service (M6) | Detectors (M4), Heartbeat (M1), app (M2) |
| `route-pack.json` | Pack builder (M2) | Traveller app (M2), offline |

## Conventions

- **Field names:** `camelCase`
- **Times:** ISO 8601 in UTC, ending in `Z`, e.g. `2026-10-12T08:15:30Z`
- **Units in the name:** `accuracyM` (metres), `speedMps` (metres per second), `durationS` (seconds), `batteryPct` (percent)
- **ID prefixes:** `U-` traveller, `G-` caregiver, `R-` route, `T-` trip, `I-` incident (built from parts, see `incident.json`), `D-` disruption
- **Unknown values:** use `null`, don't leave the field out
- **Problem types** (used everywhere): `off_route`, `wrong_direction`, `missed_stop`, `long_dwell`, `no_response`, `help_requested`, `disruption`

## Changing a contract later

1. Open a pull request that changes the file.
2. Get approval from the owners of every service that sends or receives that message.
3. Post in the group chat before merging.

Adding a new optional field is safe. Renaming or removing a field breaks other people's code, so avoid it.

## Open questions for the team

- [ ] Check-in every 10 seconds OK? (affects battery and cost)
- [ ] Do detectors read route context from Redis themselves (current draft), or should the Coordinator send it in the request?
- [ ] Is 7 days the right time to keep check-ins?
