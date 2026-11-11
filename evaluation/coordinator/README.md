# Evaluation: check-in analysis (Member 3)

Experiments that measure the Coordinator: is its decision better than any single detector, does it
keep deciding correctly when detectors are slow, wrong or down, how fast it is under load, and
whether a crashed replica loses or repeats anything. The check-in path (Traveller App, Check-in
Service) is measured separately by Member 2 in [`evaluation/`](../README.md).

| | Question | Compares | Status |
|---|---|---|---|
| **C1** | How many problems does the final decision catch, how late, and how many false alarms does it raise? | the Coordinator's decision vs each detector on its own | done ([RESULTS.md](RESULTS.md)) |
| **C2** | Does the decision stay correct when one detector is slow (over 500 ms), wrong or down? | no fault vs each fault, and a wrong detector with its reliability set to 0 | done ([RESULTS.md](RESULTS.md)) |
| **C3** | How long does a decision take as the check-in rate grows? | p50 / p95 decision time at increasing check-ins per second | planned |
| **C4** | Is any check-in lost or decided twice, or any alert repeated, when a Coordinator replica is killed mid-run, or the Alert Service is down? | 2 replicas: no crash vs a replica killed every 20 s vs the Alert Service down for 45 s | done ([RESULTS.md](RESULTS.md)) |
| **C5** | What does asking fewer detectors save, and what does it cost? | `fixed4` (deployed) vs `adaptive` (2 at normal risk) vs `fixed2` | done ([RESULTS.md](RESULTS.md)) |

## How it works

- **The scenarios and their ground truth are Member 4's simulator** (`tools/simulator`): `normal`,
  `off_route`, `wrong_direction`, `long_dwell`, `normal_bus_wait`, `poor_gps`. Every simulated
  check-in carries the correct answer (`expected`), using the definitions in
  `contracts/problem-definitions.md`.
- **Everything else is the real code**, on the local stack (`services/analysis-dev`): the
  Coordinator, the 4 detectors and the Alert Service, with ElasticMQ for SQS, Redis and DynamoDB
  Local.
- **C2 breaks one real detector at a time.** `compose.c2.yaml` puts a fault switch (our
  `services/fault-switch`) in front of each real detector. Normally it passes
  the real vote on unchanged; `run.py` tells one switch to be *slow* (2 s, over the 500 ms
  timeout), *dead* (every request fails), or *wrong* (the real vote inverted), and for *wrong,
  silenced* also sets that detector's reliability to 0. The same journeys as C1 are replayed for
  each of the 17 conditions, 8 journeys at a time (a slow detector costs 500 ms per check-in).
  `run.py` stops if a fault doesn't show in the votes, so it can't silently measure a healthy
  stack.
- **The results come from the system itself.** Every decision is logged in Redis
  (`decisions:recent`: votes, latency, k, decision, incident). Alerts are read back from the
  Alert Service (`/api/incidents`). Each is compared with the simulator's labels.
- **One check-in at a time, in order.** Each is sent only after the previous one was decided, as
  a phone checking in every 10 s would: the detectors read the trip's recent check-ins, so they
  must not see later ones early. Every journey has its own trip and traveller, so runs never
  share risk, duplicate markers or incidents.
- **Two kinds of off route.** The simulator labels `off_route` once the traveller is 100 m
  sideways from where they should be; the contract (and route-corridor) measure to the nearest
  point of the whole route. A drift towards another leg of the route (the road north to the bus
  stop) is labelled off route but never leaves the corridor. C1 scores those journeys as their
  own case, *off route near another leg*: route-corridor can't see them by definition, so they
  show what the second judge (route-progress) adds. Seeds are drawn until there are 5 that
  leave the corridor; every near-leg one on the way is kept.
- **C4 crashes real containers.** `compose.c4.yaml` adds a second Coordinator replica on the same
  queue (as on EKS). `run.py` SIGKILLs one replica every 20 s, alternating, and starts it again
  5 s later; or stops the Alert Service for 45 s. How often each check-in was decided comes from
  both replicas' logs, and every decision is compared with the run without crashes.
- **C5 switches the Coordinator's mode.** `run.py` restarts the Coordinator with each
  `REDUNDANCY_MODE` in turn (`fixed4`, `adaptive`, `fixed2`), checks from the decisions that it
  really switched, and leaves it in `fixed4`.
- No AWS: the numbers describe the design on one machine, not EKS performance.

## Requirements

- **Docker** with Compose v2 (`docker compose`), and about 4 CPUs and 8 GB of memory for it. The
  first `up --build` pulls and builds every image, which takes a few minutes.
- **Python 3.10 or newer**, run from the repo root (the scripts import Member 4's simulator from
  `tools/`).
- **These ports free on localhost:** 8000 (Coordinator and dashboard), 8004 (Alert Service), 6379
  (Redis), 9324 (ElasticMQ), 9001-9004 (detectors) and, for C2, 9101-9104 (fault switches).
- **A machine that stays awake.** C2 and C4 fail, on purpose, if the stack becomes too slow to
  measure (over 1% of healthy detector calls timing out), which happens when the computer sleeps.
  On a Mac, prefix the long runs with `caffeinate -dimsu` (`-i` alone doesn't wake a sleeping Mac).
  On Windows, set *Sleep* to *Never* while plugged in; on Linux, `systemd-inhibit` does the same.
- **Enough free CPU.** The same check stops the run if other containers or programs leave the
  stack short of CPU: `30 of 2472 votes from healthy detectors timed out` means it was. Stop other
  Docker containers (`docker ps`), give Docker more CPUs (Docker Desktop: *Settings > Resources*),
  or set `PARALLEL = 4` in `run.py` (journeys at once; the run takes longer, the detection results
  are the same). Then run the same command again.

## Running

From the repo root. Each block starts the stack the way that experiment needs it;
`--remove-orphans` stops what another experiment added (C2's switches, C4's second replica).

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r evaluation/coordinator/requirements.txt
python -m pytest evaluation/coordinator/tests     # the scoring definitions (no stack needed)

# C1: one Coordinator calling the detectors directly, in fixed4 (the default)
docker compose -f services/analysis-dev/compose.yaml up --build -d --remove-orphans
python evaluation/coordinator/run.py C1           # about 2 minutes; --seeds 1 for a quick check

# C2: a fault switch in front of each detector (restarts the Coordinator pointing at them)
docker compose -f services/analysis-dev/compose.yaml -f evaluation/coordinator/compose.c2.yaml up --build -d --remove-orphans
caffeinate -dimsu python evaluation/coordinator/run.py C2   # about 45 minutes; --seeds 1 takes about 15

# C4: a second Coordinator replica (kills and restarts containers with the docker CLI)
docker compose -f services/analysis-dev/compose.yaml -f evaluation/coordinator/compose.c4.yaml up --build -d --remove-orphans
caffeinate -dimsu python evaluation/coordinator/run.py C4   # about 5 minutes

# C5: restarts the Coordinator in each mode itself, alone, and leaves it in fixed4
caffeinate -dimsu python evaluation/coordinator/run.py C5   # about 6 minutes

python evaluation/coordinator/analyse.py          # -> results/summary.json and RESULTS.md
```

If C2, C4 or C5 stops part-way, run the same command again: it resumes after the last
condition saved (`--fresh` starts over). After a Coordinator change, `run.py C2 --only <fault>`
reruns just that fault and replaces it in a copy of the newest C2 result.

## What to expect when you rerun them

- **Detection results should match [RESULTS.md](RESULTS.md).** The journeys come from fixed
  simulator seeds and the decisions are deterministic: the same check-ins get the same decisions,
  so caught / missed, delays (in simulated seconds), false alarms, alerts, and C4's lost /
  decided twice / changed counts should come out the same.
- **Timings won't.** Latency (p50 / p95), how many check-ins C4 counts as late, and which crash
  catches which check-in in flight depend on the machine and on timing.
- **The raw runs aren't in git.** `run.py` saves each run to `results/<experiment>-<time>.json`
  (gitignored, like every `results/` folder; a full C2 run is about 100 MB). `analyse.py` scores
  the newest finished run of each experiment and writes `RESULTS.md`, which is committed. How
  the decisions are scored is at the top of `analyse.py`.
