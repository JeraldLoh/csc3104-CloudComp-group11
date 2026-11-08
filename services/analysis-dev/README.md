# Check-in analysis: local run

Runs the Coordinator on your laptop with stand-ins for everything else: Redis, ElasticMQ
(a local SQS, no account needed), the real detectors and the real Alert Service (Member 5)
with a local DynamoDB. No AWS, no cost.

```
send ──► ElasticMQ ──► coordinator ──► detector-route / -heading / -dwell / -progress (real)
                        │   decides (rule-based), updates risk
                        ▼
           Redis ◄──────┴──────► alert-service (real) ──► DynamoDB (local)
```

Run all commands from the repo root.

## Start and stop

```bash
docker compose -f services/analysis-dev/compose.yaml up --build -d
docker compose -f services/analysis-dev/compose.yaml down          # stop (data is not kept)
```

`curl localhost:8000/ready` (Coordinator) should answer `{"status":"ready"}`.

## Detectors need a trip in Redis

The detectors read the trip from Redis. A `seed` job writes the route and context for
`T-001` (standing in for the Journey Service), and `send` also writes `trip:{id}:recent` and `:context`
(standing in for the Check-in Service). They also need a *stream* of check-ins: route-corridor
abstains until it has 3 usable ones, so send `--seq 1` to `--seq 5` before expecting a verdict.

## Send check-ins

```bash
docker compose -f services/analysis-dev/compose.yaml run --rm send --seq 1              # on route
docker compose -f services/analysis-dev/compose.yaml run --rm send --seq 2 --off-route  # off route
```

Each check-in needs a new `--seq` (the same `--seq` again counts as a duplicate and is skipped).

## See what happened

**Dashboard: http://localhost:8000/dashboard** (served by the Coordinator, refreshes every 2 s):
risk per traveller, and every decision with its detectors, votes, latency, escalations and alerts.
Click a traveller to see only their decisions.

Or from the terminal:

```bash
docker compose -f services/analysis-dev/compose.yaml logs -f coordinator alert-service
curl 'localhost:8004/api/incidents?travellerId=U-042'                              # alerts (M5's service)
docker compose -f services/analysis-dev/compose.yaml exec redis redis-cli hgetall risk:U-042
docker compose -f services/analysis-dev/compose.yaml exec redis redis-cli lindex decisions:U-042 0
```

ElasticMQ's web UI (queue depths, dead-letter queue): http://localhost:9325

## The services

| Service | Port | What it is |
|---|---|---|
| detector-route | 9001 | real route-corridor detector |
| detector-dwell | 9002 | real dwell-time detector |
| detector-heading | 9003 | real heading-speed detector |
| detector-progress | 9004 | real route-progress detector (second judge of off-route and wrong direction; missed stops) |
| alert-service | 8004 | real (Member 5): one incident per problem, kept in DynamoDB |
| dynamodb | – | DynamoDB Local, in memory (empty after a restart) |

## Scenarios to try

Observed with the real detectors (route-corridor needs 3 usable check-ins before it judges):

| Do | Expect in the logs |
|---|---|
| `send --seq 1`, `--seq 2` (on route) | route-corridor abstains (not enough history yet), `escalating ... too few real votes`, `k=4 ... -> inconclusive` |
| `send --seq 3`, `--seq 4` (on route) | `k=4 ... -> normal` |
| `send --seq 5`, `6`, `7` with `--off-route` | `normal`, `normal`, then `-> problem`, `ALERT` (route-corridor needs 3 usable check-ins to flag) |
| `send --seq 7 --off-route` again | `skip T-001#7: already decided`, no new incident |
| `docker compose stop alert-service`, send 3 off-route, `docker compose start alert-service` | `not processed, it will be retried` (`ConnectError`); ≤30 s after it's back, `decided` and the incident exists |
| `docker compose stop detector-route`, send on-route | route-corridor `abstain` (`error: ConnectError`), k=4, the other three still decide |

## Faulty detectors on demand (fault switches)

The real detectors can be stopped (`docker compose stop detector-route`), but they never
answer slowly or wrongly. For that, add a fault switch (`services/fault-switch`) in front of each
real detector; the Coordinator then calls the switches, which pass the real votes on:

```bash
docker compose -f services/analysis-dev/compose.yaml -f evaluation/coordinator/compose.c2.yaml up --build -d
```

Each switch takes orders while it runs (the evaluation's C2 uses the same switches):

```bash
curl -X POST localhost:9102/control -H 'Content-Type: application/json' -d '{"delayMs":2000}'  # dwell-time slow
curl -X POST localhost:9102/control -H 'Content-Type: application/json' -d '{"mode":"wrong"}'   # dwell-time faulty
curl -X POST localhost:9102/control -H 'Content-Type: application/json' -d '{"mode":"error"}'   # dwell-time dead
curl -X POST localhost:9102/control -H 'Content-Type: application/json' -d '{"mode":"pass","delayMs":0}'  # back to normal
```

Modes: `pass` (the real vote), `wrong` (the real vote inverted), `error` (HTTP 500); `delayMs` on top.
Ports: route 9101, dwell 9102, heading 9103, progress 9104.

| Do | Expect in the logs |
|---|---|
| dwell-time `delayMs: 2000`, then send | dwell-time `abstained ... (timeout, 501 ms)`, the other three still decide |
| dwell-time `mode: error` for 2 min of check-ins | decisions still `normal`, then `long_dwell unchecked ... (detectors failed)` and a `tracking_lost` incident |
| dwell-time `mode: wrong` | dwell-time votes `problem` on a moving traveller: a false `long_dwell` alert |

To go back to calling the real detectors directly, run `compose.yaml` on its own again.

## Experiment modes

The Coordinator asks all 4 detectors for every check-in by default (`REDUNDANCY_MODE=fixed4`):
a missed alert is the unsafe failure. The experiment compares it with `adaptive` (2 detectors at
normal risk, 4 when elevated or uncertain) and `fixed2`. Restart the Coordinator in another mode:

```bash
REDUNDANCY_MODE=adaptive docker compose -f services/analysis-dev/compose.yaml up -d coordinator
```

The k=2 rows in the tables above are what `adaptive` does; in `fixed4` every row has k=4.
