# Journey Service benchmarks

Two scripts that measure and print their results. Standard library only, so no `pip install`. Every run also writes
its raw numbers to `results/` as JSON.

Both need the local stack running (kind local-sim or Docker Compose) with `AUTH_MODE=off`. Run from the repo root.

## Latency: photos and Simplify with AI

```
python services/journey-service/benchmarks/bench_latency.py
python services/journey-service/benchmarks/bench_latency.py --routes 5 --runs 5 --force-cold
```

Plans, saves, loads and simplifies a few of the saved demo trips through the Journey Service's API (port 8006), and
downloads every photo. Routes it saves are deleted again. It reports, per trip and overall (p50, p95, min, max):

- Simplify with AI: seconds per call, seconds per step, how time follows the number of steps (correlation r), and how
  many calls were answered by Ollama and not the template fallback
- saving a route: when its photos are fetched from Street View (cold) and when they are already stored (warm)
- loading a route with its photo links, and downloading one photo

A cold save only happens for a place nobody saved before. `--force-cold` moves each stop a few metres on a trip's first
run so that save must fetch new photos. Each new photo is a paid Street View image (about US$0.007).

## Load: requests at once before Ollama fails

```
kubectl port-forward svc/easyread-service 8007:8000 -n csc3104     # kind only; Compose already publishes 8007
python services/journey-service/benchmarks/bench_ollama_load.py
python services/journey-service/benchmarks/bench_ollama_load.py --levels 1,2,4,8,16 --keep-going
```

Sends bursts of route steps to the Easy-read Service. A request has failed when it got no AI wording (`provider` is
not `ollama`): the service never answers with an error, it falls back to the template wording.

It times one request alone (T1), then for each level reports how many requests got AI wording, their latency (p50,
p95, max), the slowest against c x T1, and the throughput; and at the end the highest level where every request got
AI wording and the level where they start to fail. Takes a few minutes: the failing level runs into the 75 s timeout.

Numbers depend on the machine and on what else it is running, so quote them with the hardware and the model
(`OLLAMA_MODEL`).
