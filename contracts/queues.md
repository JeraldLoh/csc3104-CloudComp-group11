# SQS queues 

| Queue | Message | Sent by | Received by | Settings |
|---|---|---|---|---|
| `careroute-checkins` | `checkin.json` (one per message) | Check-in Service (M2) | Coordinator (M3) | Standard queue; visibility timeout 30 s; long polling 20 s; after 5 failed attempts → `careroute-checkins-dlq` |
| `careroute-disruptions` | `disruption.json` (one per affected trip) | Disruption Lambda (M6) | Alert Service (M5) | Standard queue; visibility timeout 60 s; after 5 failed attempts → `careroute-disruptions-dlq` |

## Rules for receivers

- **Messages can arrive more than once.** Standard queues deliver *at least once*, so receivers must handle duplicates safely (the Coordinator checks `tripId` + `seq`; the Alert Service uses incident IDs).
- **Delete a message only after it's fully processed.** If your service crashes before deleting it, the queue gives it to another replica, which is how we survive crashes.
- **Messages that keep failing** go to the dead-letter queue (`-dlq`) so they don't block everything else. Check it when debugging.

Each queue is created by the **sender's** owner (M2 creates `careroute-checkins`, M6 creates `careroute-disruptions`), so the queue exists before anything is sent.
