# Evaluation: Alert Service (Member 5, issue 5.6)

Experiments that measure whether the Alert Service tells a caregiver about each problem **exactly once** when
several replicas handle the same problem at once, and whether **reminders** are sent exactly once with several
replicas checking. Numbers: **[RESULTS.md](RESULTS.md)**. The check-in path and the Coordinator are measured
separately ([`evaluation/`](../README.md), [`evaluation/coordinator/`](../coordinator/README.md)).

| | Question | Compares |
|---|---|---|
| **A1** | When the same problem is reported many times at once, spread over replicas, is there one incident, one push, and is every report counted? And when a replica is killed mid-run? | as deployed (conditional writes + push outbox) vs read-then-write, with and without the outbox; 1, 2, 4 replicas; a replica killed every 5 s, with and without the outbox |
| **A2** | With every replica looking for due reminders, does each unacknowledged urgent alert get exactly 3 reminders, on time? And when a replica is killed? | as deployed vs read-then-write, with and without the outbox; 1, 2, 4 replicas; a replica killed every 4 s |

## How it works

- **The replicas run the real service.** `replica.py` imports `services/alert-service/app` unchanged and starts it
  with uvicorn, as the container does. Each condition starts its own replicas on `127.0.0.1:18101` and up, with
  fresh DynamoDB tables named for that condition (deleted afterwards), on LocalStack and the cluster's Redis.
- **Three things can be changed from outside the service:**
  - pushes go to a recorder (the Redis list `eval:pushes`, with the time and the replica) instead of Firebase, so
    they can be counted;
  - with `EVAL_WRITES=naive`, the conditional writes (creating an incident, claiming a reminder, claiming a push)
    are done the naive way, by reading the incident, deciding in Python, then writing without a condition
    (`ReadThenWrite`). That is the baseline: it shows what the conditional writes prevent.
  - with `EVAL_OUTBOX=off`, pushes are sent as before the push outbox: straight away, and never by another replica.
- **Load is spread like a Kubernetes Service.** Each report goes to the next replica in turn. A report that gets no
  answer (a replica down, or killed mid-request) is sent again to the next one, as the Coordinator's SQS message
  comes back when it gets no answer.
- **A crash is a kill.** `Crasher` kills a replica's process outright (no shutdown, like a crashed pod), taking
  turns, and starts it again a few seconds later, as Kubernetes would.
- **Ground truth comes from the systems.** After each condition, `run.py` reads every incident from DynamoDB and
  every recorded push, and compares them with what was sent. It doesn't trust the replicas' answers.
- **Pushes are counted after the outbox is empty.** A push a killed replica didn't send is sent by another once its
  lease runs out (`services/alert-service/README.md`), so each condition waits until no incident has a push pending,
  then until no new push has come for a few seconds.
- **A2 is time-scaled.** The deployed reminder waits 120 s and replicas check every 15 s; a push lease is 15 s. A2
  uses 8 s, 1 s and 1 s (about 1:15), so a run takes seconds, not minutes.

## Run it yourself

Needs the kind cluster from the repo README (LocalStack and Redis in namespace `csc3104`), and Python with the
Alert Service's packages.

```powershell
# 1. LocalStack and Redis on this laptop (two terminals, or as jobs)
kubectl port-forward svc/cloud-localstack 4566:4566 -n csc3104
kubectl port-forward svc/cloud-redis 6379:6379 -n csc3104

# 2. Python packages (from the repo root)
python -m venv .venv-eval
.\.venv-eval\Scripts\Activate.ps1
pip install -r services\alert-service\requirements-dev.txt

# 3. Run and write RESULTS.md
python evaluation/alert-service/run.py A1 A2 --quick   # about 5 minutes; drop --quick for the full run (about 12)
python evaluation/alert-service/report.py

# The evaluation's own tests (no services needed)
pytest evaluation/alert-service/tests
```

`run.py --only killed` runs just the conditions with that in their name. `--keep` keeps each condition's tables
to look at. Sizes: `--problems` and `--copies` (A1), `--alerts`, `--after`, `--check`, `--max` and `--lease` (A2). Settings:
`AWS_ENDPOINT_URL` (default `http://localhost:4566`) and `REDIS_URL` (default `redis://localhost:6379/8`; only the
key `eval:pushes` is used, and it's cleared before each condition).

| File | |
|---|---|
| `replica.py` | one replica of the real service, with pushes recorded and optionally naive writes |
| `run.py` | starts the replicas, sends the load, kills replicas, reads the ground truth, saves `results/` |
| `report.py` | writes RESULTS.md from the newest `results/A1-*.json` and `results/A2-*.json` |
| `results/` | raw runs (not committed) and each replica's log in `results/logs/` |
| `tests/` | the naive writes and the counting, against moto |
