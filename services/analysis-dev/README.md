# Check-in analysis: local run

Runs the Coordinator and Aggregator on your laptop with stand-ins for everything else:
Redis, ElasticMQ (a local SQS, no account needed), the real detectors and the real Alert
Service (Member 5) with a local DynamoDB. No AWS, no cost.

```
send ──► ElasticMQ ──► coordinator ──► detector-route / -heading / -dwell / -progress (real)
                           │
                           ▼
           Redis ◄── aggregator ──► alert-service (real) ──► DynamoDB (local)
```

Run all commands from the repo root.

## Start and stop

```bash
docker compose -f services/analysis-dev/compose.yaml up --build -d
docker compose -f services/analysis-dev/compose.yaml down          # stop (data is not kept)
```

`curl localhost:8000/ready` (Coordinator) and `curl localhost:8001/ready` (Aggregator) should
both answer `{"status":"ready"}`.

## Demo panel: http://localhost:8005

Drives everything from the browser, with the dashboard embedded below (no terminal needed):

- **Journey:** walk, wait at the bus stop, ride, go off route, back on route, or the full 15-check-in journey.
- **Services:** stop or start the Alert Service or any detector. These are real outages (the
  container stops), so they work with the real services. The square shows its state now.
- **Fakes only:** make route-corridor slow or faulty (needs `compose.fakes.yaml`, below).
- **Send duplicate** (the Coordinator skips it) and **Reset demo** (clears Redis and the incidents,
  re-seeds the trip).

It's `services/demo-control`, local only. It mounts the Docker socket to stop and start containers.

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

**Dashboard: http://localhost:8001/dashboard** (served by the Aggregator, refreshes every 2 s):
risk per traveller, and every decision with its detectors, votes, latency, escalations and alerts.
Click a traveller to see only their decisions.

Or from the terminal:

```bash
docker compose -f services/analysis-dev/compose.yaml logs -f coordinator aggregator alert-service
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
| demo-control | 8005 | the demo panel |

## Scenarios to try

Observed with the real detectors (route-corridor needs 3 usable check-ins before it judges):

| Do | Expect in the logs |
|---|---|
| `send --seq 1`, `--seq 2` (on route) | route-corridor abstains (not enough history yet), `escalating ... too few real votes`, `k=4 ... -> inconclusive` |
| `send --seq 3`, `--seq 4` (on route) | `k=4 ... -> normal` |
| `send --seq 5`, `6`, `7` with `--off-route` | `normal`, `normal`, then `-> problem`, `ALERT` (route-corridor needs 3 usable check-ins to flag) |
| `send --seq 7 --off-route` again | `skip T-001#7: already decided`, no new incident |
| panel: Stop Alert Service, go off route, Start Alert Service | `not processed, it will be retried` (Aggregator 503); ≤30 s after it's back, `decided` and the incident exists |
| panel: Stop route-corridor, walk | route-corridor `abstain` (`error: ConnectError`), k=4, the other three still decide |

## Fault demos (fake detectors)

The real detectors can be stopped (panel or `docker compose stop detector-route`), but they never
answer slowly or wrongly. For that, run the same stack with fake detectors instead (`compose.fakes.yaml` replaces the
four detector services; only one of each runs):

```bash
docker compose -f services/analysis-dev/compose.yaml -f services/analysis-dev/compose.fakes.yaml up --build -d
```

The fakes answer at once (no history needed) and take orders while they run:

```bash
curl -X POST localhost:9001/control -H 'Content-Type: application/json' -d '{"delayMs":2000}'  # route-corridor slow
curl -X POST localhost:9001/control -H 'Content-Type: application/json' -d '{"mode":"wrong"}'  # route-corridor faulty
curl -X POST localhost:9001/control -H 'Content-Type: application/json' -d '{"mode":"auto","delayMs":0}'  # back to normal
```

Modes: `normal`, `problem`, `auto` (problem when outside the demo area), `wrong`, `abstain`, `error`.
Ports: route 9001, dwell 9002, heading 9003, progress 9004.

| Do | Expect in the logs |
|---|---|
| `send --seq 1` | `k=2 ... -> normal`, no incident |
| `send --seq 2 --off-route` | `escalating ... single_voter` (route-corridor asks for route-progress's second opinion), `k=4 ... -> problem`, `ALERT`, risk `elevated` |
| `send --seq 3` | `k=4` (risk is elevated) |
| `send --seq 2 --off-route` again | `skip T-001#2: already decided`, no new incident |
| 3 more on-route check-ins | risk back to `normal`, then `k=2` again |
| detector-route `delayMs: 2000`, then send | `abstained ... (timeout, 501 ms)`, escalates, still decides |

To go back to the real detectors, run `compose.yaml` on its own again.

## Experiment modes

Compare the experiment's modes by restarting with `REDUNDANCY_MODE=fixed2` or `fixed4`:

```bash
REDUNDANCY_MODE=fixed4 docker compose -f services/analysis-dev/compose.yaml up -d coordinator
```
