# Heartbeat Monitor (Member 1, issue 1.4)

Notices when a traveller's phone has gone quiet for longer than its usual rhythm allows, and raises a
`no_response` alert. The detectors only see check-ins that arrive; this service covers the ones that
don't (phone lost, battery dead, no signal, traveller in trouble).

It has no API apart from `GET /health` and `GET /ready` (contracts/api.md). Every 5 s, one replica
checks every trip in `active-trips` and sends `problem-report-no-response.json` to the Alert Service's
`POST /internal/problems`.

## How it decides: φ accrual failure detector

A fixed timeout would be either too slow for a phone that checks in like clockwork or too jumpy for one
on a patchy connection. Instead, [`app/phi.py`](app/phi.py) implements the φ accrual failure detector
(Hayashibara et al., 2004, as used by Akka and Cassandra):

1. Learn the trip's rhythm from `hb:{tripId}:intervals`, the last 100 gaps between live check-ins that
   the Check-in Service records. Until there are 5 gaps, assume the app's normal 10 s.
2. Measure the silence since `hb:{tripId}:lastseen`, the server time of the last check-in, so phone
   clocks don't matter.
3. Work out φ = −log₁₀(chance that the next check-in is merely late, given that rhythm).
   φ 1 = 10 %, φ 2 = 1 %, φ 8 = 1 in 100 million.
4. At φ ≥ 8, report the traveller.

A patchy connection (gaps that vary a lot) automatically gets more slack than a steady one.

### Expected silences: underground and background

Some silences are expected, so they're added to the learnt rhythm as an "acceptable pause" before φ starts
rising:

| Situation | How it's recognised | Extra pause | Alert after (10 s rhythm) |
|---|---|---|---|
| Normal | | 30 s | ≈ 50 s |
| Underground step | The current step (`trip:{tripId}:context` → `stepIndex`) in `trip:{tripId}:route` has `underground: true`, or is a `train` step with `underground: null` (OneMap doesn't say which MRT stretches are underground, so we assume they might be) | + 5 min | ≈ 5.8 min |
| App in the background | `hb:{tripId}:status` is `backgrounded` (the app says so as the page hides) | + 15 min | ≈ 15.8 min |

If both apply, the larger extra pause is used.

### Once per silence

When the Alert Service accepts a report, `hb:{tripId}:reported` is set to the `lastseen` it was about.
That silence isn't reported again; the next check-in changes `lastseen`, so a later silence is. If the
Alert Service is down, nothing is marked and the next tick (5 s later) tries again.

### Two replicas, one checker

Both replicas run the loop, but only the holder of the Redis lease `lock:heartbeat-monitor` checks
trips (same pattern as the Check-in Service's clean-up task). If it dies, the other takes over within
about 15 s. Even two leaders at once can't produce two alerts that matter: the Alert Service merges
repeated reports of the same problem into one incident.

## Settings

All environment variables; defaults in [`app/config.py`](app/config.py).

| Variable | Default | Meaning |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Shared Redis |
| `ALERT_SERVICE_URL` | `http://alert-service:8000` | Where reports go |
| `PHI_THRESHOLD` | `8` | φ at which a traveller is reported |
| `BASE_PAUSE_S` | `30` | Pause always allowed on top of the learnt rhythm |
| `UNDERGROUND_PAUSE_S` | `300` | Extra on an underground step |
| `BACKGROUNDED_PAUSE_S` | `900` | Extra while the app is in the background |
| `EXPECTED_INTERVAL_MS` | `10000` | Rhythm assumed until a trip has `MIN_SAMPLES` (5) gaps |
| `MIN_STD_MS` | `2000` | Floor on the learnt spread |
| `TICK_S` | `5` | How often trips are checked |

## Run the tests

```
cd services/heartbeat-monitor
pip install -r requirements-dev.txt
pytest
```

The Redis tests use `fakeredis[lua]`. On Windows, if the leader-lease tests fail with
`unknown command 'evalsha'`, your virtual environment's path is too long for its Lua DLL; create the
venv somewhere with a shorter path.

## Deploy

```
docker build -t heartbeat-monitor:local services/heartbeat-monitor        # local kind cluster:
kind load docker-image heartbeat-monitor:local --name csc3104-local       #   k8s/local/heartbeat-monitor.yaml
bash scripts/macos/deploy.sh --service heartbeat-monitor                  # EKS: k8s/eks/services/heartbeat-monitor.yaml
```

It needs the Check-in Service (writes the `hb:*` keys), the Journey Service (`active-trips`, routes)
and the Alert Service running.
