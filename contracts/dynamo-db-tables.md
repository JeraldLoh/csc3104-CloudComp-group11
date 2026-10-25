# DynamoDB tables

All tables are named `careroute-<name>`, use **on-demand** billing, and are created by their owner in their own `infra/` stack.

| Table | Partition key | Sort key | Extra indexes | Delete automatically (TTL) | Owner |
|---|---|---|---|---|---|
| `checkins` | `tripId` | `seq` (number) | `pending` + `receivedAt` (to find stuck check-ins) | `expiresAt`, 7 days after the check-in | Member 2 |
| `routes` | `routeId` | — | `travellerId` | — | Member 6 |
| `trips` | `tripId` | — | `travellerId` + `date`; `status` (to find active trips) | `expiresAt`, 30 days after the trip | Member 6 |
| `incidents` | `incidentId` | — | `travellerId` + `createdAt` | `expiresAt`, 30 days after resolved | Member 5 |
| `travellers` | `travellerId` | — | — | — | Member 5 |
| `caregiver-links` | `caregiverId` | `travellerId` | `travellerId` (who cares for this traveller?) | — | Member 5 |
| `safe-locations` | `travellerId` | `locationId` | — | — | Member 5 |
| `device-tokens` | `userId` | `token` | — | — | Member 5 |

Item shapes follow the example files: `checkin.json`, `route.json`, `trip.json`, `incident.json`.

## Important rules

- **Incidents are created with a conditional write**: only if `incidentId` doesn't exist yet (`attribute_not_exists(incidentId)`). This is what stops two replicas sending the same alert.
- **Incident ID** = `travellerId#tripId#problemType#stepIndex`, e.g. `U-042#T-001#off_route#2`.
- **Routes and trips are separate.** A disruption only changes the trip (`override` field), never the caregiver's planned route.
- **Who can read what:** only the Journey Service and Profiles APIs read names and personal details. Detectors, Coordinator, Aggregator and Heartbeat use IDs only.
