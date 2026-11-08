# Evaluation (issue 2.5)

Experiments that measure how the Traveller App and the Check-in Service behave when things
go wrong: lost signal, overload, servers failing part-way, phones dying, a replica crashing.
Results and figures: **[RESULTS.md](RESULTS.md)**.

| | Question | Compares |
|---|---|---|
| **E1** | Does any check-in get lost, duplicated or reordered when the phone loses signal for 1, 5 or 10 minutes? (3 runs each) | the CareRoute app (outbox) vs an app that sends each check-in once |
| **E1-restart** | ...and if the app is closed and reopened during the outage? | outbox in IndexedDB vs in memory |
| **E1-budget** | Do phones catch up after a 5-minute outage when every phone sends its backlog at once? | batches time-limited (1 s) vs not |
| **E1-real** | Same as E1, 5-minute outage, at full length (no time scaling) | checks the scaled runs |
| **E2** | 200 phones regain signal at the same instant against an overloaded server | no randomness vs random retry delays vs + spread reconnect |
| **E3** | The server fails part-way (20% of queue sends) and 30% of phones die mid-request | clean-up task off vs on vs on with the leader replica killed |
| **E4** | How fast would the failure detector notice a phone that dies right after catching up? | the service's heartbeat rule vs "every arrival counts" (from E1-real) |

## How it works

- **The phones run the app's real code.** `apps/traveller/experiments/phones.ts` runs many phones in
  one Node process, each with the app's own `Outbox`, `Sender` and `HttpTransport` (`src/lib`) on an
  in-memory IndexedDB. The network is simulated per phone: a *silent* outage (the phone thinks it's
  online but requests hang until the timeout, like a tunnel) or a *known* one (`navigator.onLine` is
  false, then the `online` event fires).
- **The server is the real service** (`services/checkin-service/tools/fault_server.py`), with optional
  faults: SQS sends that fail after the check-in was stored, and a fixed extra processing time per
  check-in so an overload experiment has a known capacity. Several replicas run behind a simulated
  load balancer that stops routing to a crashed replica after its health checks fail.
- **Ground truth comes from the systems themselves.** After each run, `run.py` reads every item in
  DynamoDB, every message in SQS and the Redis keys, and compares them with what each phone made.
  A check-in counts as **lost** only if it was never stored *and* is no longer on the phone; one
  still in the outbox when the run stops is counted separately as *still waiting*. A phone counts
  a check-in as acknowledged only if the server's answer covered it (`processed`), exactly as the
  app does. `run.py` also records any unexpected exception in the server logs.
- **Time is scaled 1:10** (10 s heartbeat → 1 s, 5-minute outage → 30 s) so the suite runs in about
  an hour and a half. The phone's request timeout is not scaled, because server response times don't
  shrink with the experiment's clock. Scaling the clock also multiplies the load by 10: 20 scaled
  phones send as much as 200 real ones. E1-real repeats the 5-minute case at full length (50 phones,
  no scaling) to check that the scaled runs tell the same story.
- **Load is kept below what the local AWS emulator handles** (about 45 check-ins/s), so the results
  describe the design, not the emulator. This setup measures correctness and behaviour under
  failure, not AWS throughput.

## Run it yourself

Needs Docker (for Redis + LocalStack), Node 22 and Python 3.12.

```powershell
# 1. Redis and LocalStack (from the repo root; needs LOCALSTACK_AUTH_TOKEN in .env)
$env:COMPOSE_FILE = "services/checkin-service/compose.checkin.yaml"
docker compose up -d --build

# 2. Python packages (the service's venv is fine)
cd services\checkin-service
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r ..\..\evaluation\requirements.txt
cd ..\..

# 3. The app's packages (once)
cd apps\traveller; npm install; cd ..\..

# 4. Run, analyse, plot
python evaluation/run.py --quick        # about 15 minutes; drop --quick for everything (about 1.5 hours)
python evaluation/analyse.py            # -> results/summary.json
python evaluation/plot.py               # -> figures/
python evaluation/report.py             # -> RESULTS.md
```

`run.py` uses Redis database 9 (it is wiped) at `redis://localhost:6379/9` and LocalStack at
`http://localhost:4566`; set `REDIS_URL` / `AWS_ENDPOINT_URL` to change them. LocalStack is slower than
the emulator used for RESULTS.md (moto), so absolute times will differ; the comparisons should not.
Run single experiments by name: `python evaluation/run.py E2 E3` (names: `E1`, `E1-restart`, `E1-real`,
`E2`, `E3`).

| File | |
|---|---|
| `run.py` | starts replicas, runs the phones, injects faults, collects ground truth |
| `analyse.py` | turns raw runs into `results/summary.json` |
| `plot.py` | figures in `figures/` (PDF for the article, PNG for slides) |
| `report.py` | writes RESULTS.md from `summary.json` (every number in it comes from the data) |
| `results/` | `summary.json` (in git); raw runs (`*.json`) and server logs are not committed (about 15 MB): rerun to recreate them |
