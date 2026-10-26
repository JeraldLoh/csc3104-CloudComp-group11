# Check-in analysis: local run

Runs the Coordinator and Aggregator on your laptop with stand-ins for everything else:
Redis, ElasticMQ (a local SQS, no account needed), four fake detectors and a fake Alert
Service. No AWS, no cost.

```
send ──► ElasticMQ ──► coordinator ──► detector-route / -dwell / -heading / -ml (fakes)
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

## Send check-ins

```bash
docker compose -f services/analysis-dev/compose.yaml run --rm send --seq 1              # on route
docker compose -f services/analysis-dev/compose.yaml run --rm send --seq 2 --off-route  # off route
```

Each check-in needs a new `--seq` (the same `--seq` again counts as a duplicate and is skipped).

## See what happened

**Dashboard: http://localhost:8001/dashboard** (served by the Aggregator, refreshes every 2 s):
risk per traveller, and every decision with its detectors, votes, latency, escalations and alerts.
Click a traveller to see only their decisions.

Or from the terminal:

```bash
docker compose -f services/analysis-dev/compose.yaml logs -f coordinator aggregator alert-service
curl localhost:8004/api/incidents                                                    # alerts
docker compose -f services/analysis-dev/compose.yaml exec redis redis-cli hgetall risk:U-042
docker compose -f services/analysis-dev/compose.yaml exec redis redis-cli lindex decisions:U-042 0
```

ElasticMQ's web UI (queue depths, dead-letter queue): http://localhost:9325

## The fakes

| Service | Port | Default behaviour |
|---|---|---|
| detector-route | 9001 | `auto`: problem when off route |
| detector-dwell | 9002 | always `normal` (it only judges long dwells, so it never outvotes an off-route) |
| detector-heading | 9003 | `auto` |
| detector-ml | 9004 | `abstain` (ml-anomaly isn't built yet; switch it on live for a demo) |
| alert-service | 8004 | creates one incident per problem |

Change a fake while it runs:

```bash
curl -X POST localhost:9001/control -H 'Content-Type: application/json' -d '{"delayMs":2000}'  # slow -> timeout
curl -X POST localhost:9001/control -H 'Content-Type: application/json' -d '{"mode":"wrong"}'  # faulty detector
curl -X POST localhost:8004/control -H 'Content-Type: application/json' -d '{"mode":"error"}'  # Alert Service down
curl -X POST localhost:8004/control/reset                                                       # clear incidents
```

Detector modes: `normal`, `problem`, `auto`, `wrong`, `abstain`, `error`. Put `{"mode":"ok"}` /
`{"delayMs":0}` to undo.

## Scenarios to try

| Do | Expect in the logs |
|---|---|
| `send --seq 1` | `k=2 ... -> normal`, no incident |
| `send --seq 2 --off-route` | `escalating ... single_voter` (only route-corridor judges the route; asks ml-anomaly too), `k=4 ... -> problem`, `ALERT`, risk `elevated` |
| `send --seq 3` | `k=4` (risk is elevated) |
| `send --seq 2 --off-route` again | `skip T-001#2: already decided`, no new incident |
| 3 more on-route check-ins | risk back to `normal`, then `k=2` again |
| detector-route `delayMs: 2000`, then send | `abstained ... (timeout, 501 ms)`, escalates, still decides |
| alert-service `mode: error`, send off-route, then `mode: ok` | `not processed, it will be retried`; ~30 s after it's back, `decided` |

Compare the experiment's modes by restarting with `REDUNDANCY_MODE=fixed2` or `fixed4`:

```bash
REDUNDANCY_MODE=fixed4 docker compose -f services/analysis-dev/compose.yaml up -d coordinator
```
