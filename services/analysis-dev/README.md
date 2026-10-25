# Check-in analysis: local run

Runs the Coordinator and Aggregator on your laptop with stand-ins for everything else:
Redis, ElasticMQ (a local SQS, no account needed), the real detectors and a fake Alert
Service. No AWS, no cost.

```
send ──► ElasticMQ ──► coordinator ──► detector-route / -dwell / -heading (real)
                           │
                           ▼
           Redis ◄── aggregator ──► alert-service (fake)
```

Run all commands from the repo root.

## Start and stop

```bash
docker compose -f services/analysis-dev/compose.yaml up --build -d
docker compose -f services/analysis-dev/compose.yaml down          # stop (data is not kept)
```

`curl localhost:8000/ready` (Coordinator) and `curl localhost:8001/ready` (Aggregator) should
both answer `{"status":"ready"}`.

## Detectors need a trip in Redis

The detectors read the trip from Redis. A `seed` job writes the route and context for
`T-001` (standing in for the Journey Service), and `send` also writes `trip:{id}:recent` and `:context`
(standing in for the Check-in Service). They also need a *stream* of check-ins: route-corridor
abstains until it has 3 usable ones, so send `--seq 1` to `--seq 5` before expecting a verdict.
There is no ML detector yet, so `ml-anomaly` always abstains (the Coordinator records the failed call).

## Send check-ins

```bash
docker compose -f services/analysis-dev/compose.yaml run --rm send --seq 1              # on route
docker compose -f services/analysis-dev/compose.yaml run --rm send --seq 2 --off-route  # off route
```

Each check-in needs a new `--seq` (the same `--seq` again counts as a duplicate and is skipped).

## See what happened

```bash
docker compose -f services/analysis-dev/compose.yaml logs -f coordinator aggregator alert-service
curl localhost:8004/api/incidents                                                    # alerts
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
| alert-service | 8004 | fake: creates one incident per problem |

Change the fake Alert Service while it runs:

```bash
curl -X POST localhost:8004/control -H 'Content-Type: application/json' -d '{"mode":"error"}'  # Alert Service down
curl -X POST localhost:8004/control/reset                                                       # clear incidents
```

## Scenarios to try

Observed with the real detectors (route-corridor needs 3 usable check-ins before it judges):

| Do | Expect in the logs |
|---|---|
| `send --seq 1`, `--seq 2` (on route) | route-corridor abstains, `escalating ... too few real votes`, `ml-anomaly abstained (error: ConnectError)`, `k=4 ... -> inconclusive`, risk goes `elevated` |
| `send --seq 3`, `--seq 4` (on route) | `k=4 ... -> normal` |
| `send --seq 5`, `6`, `7` with `--off-route` | `normal`, `normal`, then `-> problem`, `ALERT` (route-corridor needs 3 usable check-ins to flag) |
| `send --seq 7 --off-route` again | `skip T-001#7: already decided`, no new incident |
| alert-service `mode: error`, send off-route, then `mode: ok` | `not processed, it will be retried`; ~30 s after it's back, `decided` |

Compare the experiment's modes by restarting with `REDUNDANCY_MODE=fixed2` or `fixed4`:

```bash
REDUNDANCY_MODE=fixed4 docker compose -f services/analysis-dev/compose.yaml up -d coordinator
```
