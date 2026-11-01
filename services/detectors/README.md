# Detectors

One codebase and one image. `DETECTOR_ID` selects which detector a container runs:
`route-corridor`, `heading-speed`, `dwell-time`, `route-progress` (and `ml-anomaly` later).

The Coordinator calls `POST /internal/detect` with `contracts/examples/detector-request.json`
and gets back `contracts/examples/detector-vote.json`. Verdicts are `normal`, `problem` or `abstain`.
`abstain` means "can't decide" and always has `problemType: null` and `confidence: 0`.

## Layout

| Path | What |
|---|---|
| `detectors/models.py` | Pydantic models matching the contract JSON (camelCase on the wire) |
| `detectors/config.py` | Settings and thresholds from env vars (`Settings.from_env()`) |
| `detectors/app.py` | FastAPI app: `POST /internal/detect`, `GET /health` (liveness), `GET /ready` (pings Redis, 503 if down) |
| `tests/` | pytest; `test_models.py` checks every shared contract example still parses |

## Run the tests

```bash
cd services/detectors
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Run the service

```bash
DETECTOR_ID=route-corridor uvicorn detectors.app:app --reload   # then open /docs
```

It refuses to start if `DETECTOR_ID` is not a registered detector. Any detection or Redis failure
returns an `abstain` vote (`internal error: <type>`), never a 500; a malformed body is a 422.

## Docker

One image for all four detectors; `DETECTOR_ID` picks which one runs. Deployment is via Kubernetes (stage 11).

### Kubernetes (kind)

`k8s/local/detectors/` has one shared `base/` (Deployment with 2 replicas, ClusterIP Service on 8000,
HPA 2-4 replicas at 60% CPU, preferred pod anti-affinity across nodes and zones) and one tiny
kustomization per detector that names it and sets `DETECTOR_ID`. The Services are
`detector-route`, `-heading`, `-dwell` and `-progress`. They are not part of
`kubectl apply -k k8s/local` (so teammates without the image aren't affected); apply them
with `kubectl apply -k k8s/local/detectors`. They read Redis from `cloud-redis`, so apply `k8s/local` first.

```bash
docker build -t cloud-detectors:local services/detectors
kind load docker-image cloud-detectors:local --name csc3104-local
kubectl apply -k k8s/local            # Redis etc., if not already there
kubectl apply -k k8s/local/detectors
kubectl get pods,svc,hpa -n csc3104
kubectl port-forward -n csc3104 svc/detector-route 8101:8000
```

The HPA shows `<unknown>` CPU until metrics-server is installed in the kind cluster. On EKS: `k8s/eks/services/detectors.yaml`, deployed with
`deploy.sh --service detectors`.

### Local dev with mock data

`compose.yaml` starts Redis, a one-shot `seed` job and the four detectors (8101 route-corridor,
8102 heading-speed, 8103 dwell-time, 8104 route-progress). `dev/seed.py` writes one mock trip per simulator scenario
(`off_route`, `wrong_direction`, `long_dwell`, `normal`, `normal_bus_wait`, `poor_gps`) into Redis,
using the same keys as `contracts/redis-keys.md`.

```bash
cd services/detectors
docker compose up --build -d
python dev/seed.py --request off_route | curl -s -X POST localhost:8101/internal/detect \
     -H 'content-type: application/json' -d @-
docker compose down
```

`--request <scenario>` prints a `detector-request.json` for the scenario's interesting moment; send it
to any port. `dev/seed.py` also runs on the host (`python dev/seed.py --redis redis://localhost:6379/0`)
to re-seed. `long_dwell` makes dwell-time abstain: Redis only keeps 10 check-ins (see the dwell-time docstring).

### Replay a whole scenario

With the compose stack up, from the repo root (needs the detectors venv for `redis`):

```bash
python -m tools.simulator replay --scenario off_route      # per-check-in table + results/<scenario>-<time>.csv
python -m tools.simulator replay --all                     # every scenario, then a summary per detector
```

Flags: `--redis`, `--detectors` (comma-separated URLs), `--speed` (0 = fast, 1 = real time),
`--no-browser` (clean check-ins), `--seed`. The summary lists true positives, false alarms, abstains and the
detection delay in check-ins (only for the problem each detector owns). `results/` is git-ignored.

### Load test: many travellers at once, and watching the HPAs scale

`python -m tools.simulator load` simulates many travellers checking in at the same time. Each traveller
follows its own trip (scenarios assigned round-robin), and every check-in is written to Redis like the
Check-in Service does and then sent to every detector in parallel. Each detector gets about
`travellers / interval` requests per second. Real check-ins are 10 s apart; a smaller `--interval`
compresses time. Redis keys start with `trip:LOAD-` and expire after an hour.

Against the compose stack (ports 8101-8104):

```bash
python -m tools.simulator load --travellers 50 --interval 1 --duration 60
```

To see the detectors autoscale, run it **inside** kind as a Job. It calls the Services by name so
requests spread over every replica (a `kubectl port-forward` sends everything to one pod), and the
HPAs need metrics-server to see CPU:

```bash
kubectl apply -k k8s/local/metrics-server          # once; kind needs --kubelet-insecure-tls, already patched in
docker build -t cloud-simulator:local -f tools/simulator/Dockerfile tools
kind load docker-image cloud-simulator:local --name csc3104-local
kubectl delete job detector-load -n csc3104 --ignore-not-found
kubectl apply -k k8s/local/load-test               # 100 travellers, every 1 s, for 5 min
kubectl get hpa -n csc3104 -w                      # replicas 2 -> 4 within a minute or two
kubectl logs -n csc3104 job/detector-load -f       # req/s, p50/p95 latency and verdicts every 10 s
```

Every 10 s it prints the request rate, p50/p95 latency and the verdict counts; at the end, a summary
per detector including how many answers missed the Coordinator's 500 ms budget.
After the Job ends, the HPAs take about 5 minutes (their default stabilisation window) to scale back to 2.

## Settings

Each env var is the field name in upper case. Defaults are in `detectors/config.py`.

| Env var | Default | Used by |
|---|---|---|
| `DETECTOR_ID` | none | all |
| `REDIS_URL` | `redis://localhost:6379/0` | all |
| `GPS_MAX_ACCURACY_M` | 50 | all (abstain above this) |
| `CORRIDOR_M`, `CORRIDOR_CONSECUTIVE` | 100, 3 | route-corridor, route-progress |
| `MIN_MOVING_SPEED_MPS`, `WRONG_DIR_DEG` | 0.5, 120 | heading-speed |
| `WALK_MAX_SPEED_MPS` | 4 | heading-speed, route-progress |
| `DWELL_RADIUS_M`, `DWELL_WALK_S`, `DWELL_TRANSIT_S` | 30, 300, 1200 | dwell-time |
| `PROGRESS_MIN_M`, `MISSED_STOP_M`, `PROGRESS_MAX_SPEED_MPS`, `PROGRESS_UNSURE_CONFIDENCE` | 20, 150, 30, 0.3 | route-progress |

## Route-progress

Measures distance left to the end **along the route**, not in a straight line, so a route that bends
away from the destination still counts as progress. Each check-in is matched to its own step's part of
the path, and no further from the previous match than it could have travelled, so loops and nearby later
legs are not mistaken for progress. It judges three problem types, first match wins:

| Vote | When |
|---|---|
| `missed_stop` | bus/train step, last 3 check-ins over 150 m past the step's end stop, moving away faster than walking |
| `wrong_direction` | last 3 check-ins on the path and clearly behind the furthest point reached |
| `off_route` | last 3 check-ins outside the corridor and not getting closer to the end |
| `normal` 0.3 | outside the corridor but still getting closer (a detour), or past the stop at walking speed |
| `normal` | otherwise, more confident the more progress was made |

The low-confidence `normal` keeps it from outvoting route-corridor on a detour. Missed stop assumes the
step index stays on the bus/train step until the traveller gets off.
