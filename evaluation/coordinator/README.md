# Evaluation: check-in analysis (Member 3)

Experiments that measure the Coordinator: is its decision better than any single detector, does it
keep deciding correctly when detectors are slow, wrong or down, how fast it is under load, and
whether a crashed replica loses or repeats anything. The check-in path (Traveller App, Check-in
Service) is measured separately by Member 2 in [`evaluation/`](../README.md).

| | Question | Compares | Status |
|---|---|---|---|
| **C1** | How many problems does the final decision catch, how late, and how many false alarms does it raise? | the Coordinator's decision vs each detector on its own | done ([RESULTS.md](RESULTS.md)) |
| **C2** | Does the decision stay correct when one detector is slow (over 500 ms), wrong or down? | no fault vs each fault, and a wrong detector with its reliability set to 0 | planned |
| **C3** | How long does a decision take as the check-in rate grows? | p50 / p95 decision time at increasing check-ins per second | planned |
| **C4** | Is any check-in lost or decided twice, or any alert repeated, when a Coordinator replica is killed mid-run? | no crash vs a replica killed | planned |
| **C5** | What does asking fewer detectors save, and what does it cost? | `fixed4` (deployed) vs `adaptive` (2 at normal risk) | optional |

## How it works

- **The scenarios and their ground truth are Member 4's simulator** (`tools/simulator`): `normal`,
  `off_route`, `wrong_direction`, `long_dwell`, `normal_bus_wait`, `poor_gps`. Every simulated
  check-in carries the correct answer (`expected`), using the definitions in
  `contracts/problem-definitions.md`.
- **Everything else is the real code**, on the local stack (`services/analysis-dev`): the
  Coordinator, the 4 detectors and the Alert Service, with ElasticMQ for SQS, Redis and DynamoDB
  Local. C2 swaps in the fake detectors (`compose.fakes.yaml`), which can be made slow, wrong or
  down while they run.
- **The results come from the system itself.** Every decision is logged in Redis
  (`decisions:recent`: votes, latency, k, decision, incident). Alerts are read back from the
  Alert Service (`/api/incidents`). Each is compared with the simulator's labels.
- **One check-in at a time, in order.** Each is sent only after the previous one was decided, as
  a phone checking in every 10 s would: the detectors read the trip's recent check-ins, so they
  must not see later ones early. Every journey has its own trip and traveller, so runs never
  share risk, duplicate markers or incidents.
- **Labels are checked against the contract.** The simulator labels `off_route` once the
  traveller is 100 m sideways from where they should be; the contract measures to the nearest
  point of the route. A drift towards another leg of the route (the road north to the bus stop)
  is labelled off route but never leaves the corridor, so a correct "normal" would score as a
  miss. C1 skips those journeys, uses the next seed, and lists the skipped ones in the results.
- No AWS: the numbers describe the design on one machine, not EKS performance.

## Running

From the repo root, with the local stack up in `fixed4` (the default):

```bash
docker compose -f services/analysis-dev/compose.yaml up --build -d
pip install -r evaluation/coordinator/requirements.txt
python evaluation/coordinator/run.py C1           # about 2 minutes; --seeds 1 for a quick check
python evaluation/coordinator/analyse.py          # -> results/summary.json and RESULTS.md
python -m pytest evaluation/coordinator/tests     # the scoring definitions (no stack needed)
```

`run.py` saves each raw run to `results/<experiment>-<time>.json` (not committed, like every
`results/` folder); `analyse.py` scores the newest run of each experiment and writes
`RESULTS.md`, which is committed. How C1 is scored is at the top of
`analyse.py`.
