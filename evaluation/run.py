"""CareRoute evaluation (issue 2.5): runs the experiments and saves raw results.

Each run:
  1. starts N Check-in Service replicas (services/checkin-service/tools/fault_server.py)
     on a fresh table, queue and Redis database
  2. runs the simulated phones (apps/traveller/experiments/phones.ts), which use the
     app's real outbox and sender code
  3. meanwhile, optionally kills a replica and samples Redis/DynamoDB over time
  4. reads the ground truth: every item in DynamoDB, every message in SQS, the Redis keys
  5. writes evaluation/results/<run>.json; analyse.py and plot.py take it from there

Needs Redis and an AWS endpoint (moto_server or LocalStack). Defaults match the team's
Docker Compose stack (Redis on 6379, LocalStack on 4566):

    python evaluation/run.py                  # all experiments
    python evaluation/run.py E1 E3            # some of them
    python evaluation/run.py --quick          # smaller, for a smoke test

Environment: REDIS_URL (default redis://localhost:6379/9; database 9 is wiped),
AWS_ENDPOINT_URL (default http://localhost:4566).
"""
import argparse
import json
import re
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import boto3
import redis

ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = ROOT / "services" / "checkin-service"
APP_DIR = ROOT / "apps" / "traveller"
RESULTS = Path(__file__).resolve().parent / "results"

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/9")
AWS_ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = "ap-southeast-1"
AWS_ENV = {"AWS_ACCESS_KEY_ID": "csc3104", "AWS_SECRET_ACCESS_KEY": "csc3104", "AWS_DEFAULT_REGION": REGION}
os.environ.update({k: os.getenv(k, v) for k, v in AWS_ENV.items()})

# Time is scaled 1:10 so a 5-minute outage takes 30 s: heartbeat 10 s -> 1 s, backoff
# 1 s..60 s -> 100 ms..6 s. Only the travel clock changes; every check-in, retry and batch
# is real. The phone's request timeout is NOT scaled (2 s): how long the server takes to
# answer doesn't shrink with the experiment's clock. A full-length, unscaled run
# (E1-real) checks that the scaled runs give the same answer.
#
# The local AWS emulator handles about 45 check-ins/s, so runs stay well below that
# (20 phones = about 20 check-ins/s); otherwise the emulator, not the design, is measured.
SCALE = 10
TIMEOUT_MS = 2000


def scaled(ms: float) -> int:
    return int(ms / SCALE)


# ----------------------------------------------------------------------------- service

class Replicas:
    def __init__(self, run: str, resource: str, n: int, base_port: int, env: dict):
        self.run, self.n, self.base_port = run, n, base_port
        self.env = {**os.environ, **AWS_ENV, "AWS_ENDPOINT_URL": AWS_ENDPOINT, "REDIS_URL": REDIS_URL,
                    "CHECKINS_TABLE": resource, "CHECKINS_QUEUE": resource,
                    "CREATE_RESOURCES": "true", "LOG_LEVEL": "WARNING", "PYTHONUNBUFFERED": "1",
                    **{k: str(v) for k, v in env.items()}}
        self.procs: list[subprocess.Popen | None] = []
        self.logs = []

    @property
    def urls(self) -> list[str]:
        return [f"http://127.0.0.1:{self.base_port + i}" for i in range(self.n)]

    def start(self) -> None:
        for i in range(self.n):
            log = open(RESULTS / f"{self.run}.replica{i}.log", "w")
            self.logs.append(log)
            env = {**self.env, "PORT": str(self.base_port + i), "HOSTNAME": f"replica-{i}", "FAULT_SEED": str(i + 1)}
            self.procs.append(subprocess.Popen([sys.executable, "-m", "tools.fault_server"], cwd=SERVICE_DIR,
                                               env=env, stdout=log, stderr=subprocess.STDOUT))
        for url in self.urls:
            wait_ready(url)

    def kill(self, i: int) -> None:
        """Hard kill (like a crashed pod): no clean shutdown, so the lease is NOT released."""
        p = self.procs[i]
        if p and p.poll() is None:
            p.kill()
            p.wait()
        self.procs[i] = None

    def stop(self) -> None:
        for p in self.procs:
            if p and p.poll() is None:
                p.terminate()  # graceful on Linux/macOS (lease released); hard stop on Windows
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        for log in self.logs:
            log.close()


EXCEPTION_LINE = re.compile(r"^[\w.]+(Error|Exception): .*")


def server_errors(run: str, n: int) -> dict:
    """Exceptions in the replicas' logs, except the faults injected on purpose."""
    found: dict[str, int] = {}
    for i in range(n):
        for line in (RESULTS / f"{run}.replica{i}.log").read_text(errors="replace").splitlines():
            if EXCEPTION_LINE.match(line) and "injected fault" not in line:
                found[line[:160]] = found.get(line[:160], 0) + 1
    return found


def wait_ready(url: str, timeout_s: float = 60) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/ready", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    raise RuntimeError(f"{url} never became ready")


# ----------------------------------------------------------------------------- truth

def aws(kind):
    return boto3.client(kind, region_name=REGION, endpoint_url=AWS_ENDPOINT)


def count_stuck(table: str) -> int:
    resp = aws("dynamodb").query(TableName=table, IndexName="pending-index", Select="COUNT",
                                 KeyConditionExpression="pending = :p", ExpressionAttributeValues={":p": {"S": "P"}})
    return resp["Count"]


def collect_truth(resource: str, trips: list[str]) -> dict:
    ddb, sqs, r = aws("dynamodb"), aws("sqs"), redis.Redis.from_url(REDIS_URL)
    items, kwargs = [], {"TableName": resource}
    while True:
        page = ddb.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    stored = [{"tripId": i["tripId"]["S"], "seq": int(i["seq"]["N"]), "enqueued": i["enqueued"]["BOOL"],
               "buffered": i.get("bufferedOffline", {}).get("BOOL", False)} for i in items]

    url = sqs.get_queue_url(QueueName=resource)["QueueUrl"]
    messages, empty = [], 0
    while empty < 3:  # standard queues can return nothing even when messages remain
        got = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=0).get("Messages", [])
        if not got:
            empty += 1
            continue
        empty = 0
        for m in got:
            b = json.loads(m["Body"])
            messages.append({"tripId": b["tripId"], "seq": b["seq"], "buffered": b["bufferedOffline"]})
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])

    redis_state = {}
    for t in trips:
        recent = [json.loads(x)["seq"] for x in r.lrange(f"trip:{t}:recent", 0, -1)]
        last = r.get(f"trip:{t}:lastseq")
        intervals = [int(x) for x in r.lrange(f"hb:{t}:intervals", 0, -1)]
        redis_state[t] = {"lastseq": int(last) if last else None, "recent": recent, "intervals": intervals}
    return {"stored": stored, "messages": messages, "redis": redis_state}


class Sampler(threading.Thread):
    """Samples stuck check-ins and the lease holder every 250 ms during a run."""

    def __init__(self, resource: str):
        super().__init__(daemon=True)
        self.resource, self.samples, self.stop_flag, self.t0 = resource, [], False, time.time()

    def run(self) -> None:
        r = redis.Redis.from_url(REDIS_URL)
        while not self.stop_flag:
            try:
                holder = r.get("lock:checkin-reconciler")
                self.samples.append({"epochMs": round(time.time() * 1000), "stuck": count_stuck(self.resource),
                                     "leader": holder.decode() if holder else None})
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)


# ----------------------------------------------------------------------------- one run

def run_one(name: str, phones_cfg: dict, replicas: int = 1, server_env: dict | None = None,
            kill_leader_at_ms: int | None = None, settle_ms: int = 0, sample: bool = False) -> Path:
    print(f"\n=== {name}", flush=True)
    redis.Redis.from_url(REDIS_URL).flushdb()
    # A fresh table and queue for every run (queue names can't be reused for 60 s).
    resource = f"exp-{name.lower()}-{int(time.time())}"
    reps = Replicas(name, resource, replicas, 8100, server_env or {})
    reps.start()
    cfg = {"name": name, "baseUrls": reps.urls, **phones_cfg}
    cfg_path, out_path = RESULTS / f"{name}.config.json", RESULTS / f"{name}.phones.json"
    cfg_path.write_text(json.dumps(cfg))

    sampler = Sampler(resource) if sample else None
    killed = None
    try:
        if sampler:
            sampler.start()
        npx = shutil.which("npx") or "npx"
        phones = subprocess.Popen([npx, "tsx", "experiments/phones.ts", str(cfg_path), str(out_path)], cwd=APP_DIR)
        t0 = time.time()
        if kill_leader_at_ms is not None:
            time.sleep(max(0, kill_leader_at_ms / 1000 - (time.time() - t0)))
            holder = redis.Redis.from_url(REDIS_URL).get("lock:checkin-reconciler")
            idx = int(holder.decode().split("-")[1]) if holder else 0
            reps.kill(idx)
            killed = {"replica": idx, "epochMs": round(time.time() * 1000)}
            print(f"    killed leader replica-{idx}", flush=True)
        if phones.wait() != 0:
            raise RuntimeError("phones failed")
        time.sleep(settle_ms / 1000)  # let the clean-up task run after the phones stop
    finally:
        if sampler:
            sampler.stop_flag = True
            sampler.join()
        reps.stop()

    data = json.loads(out_path.read_text())
    trips = [p["tripId"] for p in data["phones"]]
    data["truth"] = collect_truth(resource, trips)
    data["serverEnv"] = server_env or {}
    data["replicas"] = replicas
    data["samples"] = sampler.samples if sampler else []
    data["killed"] = killed
    data["serverErrors"] = server_errors(name, replicas)
    if data["serverErrors"]:
        print(f"    WARNING unexpected server errors: {data['serverErrors']}", flush=True)
    result = RESULTS / f"{name}.json"
    result.write_text(json.dumps(data))
    out_path.unlink()
    cfg_path.unlink()
    print(f"    saved {result.relative_to(ROOT)}", flush=True)
    return result


# ----------------------------------------------------------------------------- experiments

def base_phones(**over) -> dict:
    cfg = {"phones": 20, "kind": "outbox", "tripPrefix": "T-x", "heartbeatMs": scaled(10_000),
           "durationMs": 0, "drainMs": scaled(300_000), "timeoutMs": TIMEOUT_MS,
           "backoffBaseMs": scaled(1_000), "backoffCapMs": scaled(60_000), "reconnectJitterMs": scaled(2_000),
           "jitter": True, "outages": []}
    cfg.update(over)
    return cfg


def e1(quick: bool) -> None:
    """E1: loss, duplicates and order under signal loss. Outbox app vs a naive app."""
    outages_min = [5] if quick else [1, 5, 10]
    repeats = 1 if quick else 3
    for minutes in outages_min:
        out_ms = minutes * 60_000
        for kind in ("outbox", "naive"):
            for rep in range(repeats):
                start = scaled(120_000)
                # Drain for at least as long as the outage: the scaled clock also multiplies
                # the load by 10, so a long backlog needs time to go through the emulator.
                cfg = base_phones(kind=kind, tripPrefix=f"T-E1-{minutes}m-{kind}-{rep}",
                                  durationMs=start + scaled(out_ms) + scaled(120_000),
                                  drainMs=scaled(max(300_000, out_ms)),
                                  outages=[{"startMs": start, "endMs": start + scaled(out_ms), "mode": "silent"}])
                run_one(f"E1-{minutes}min-{kind}-r{rep}", cfg)
    # The retry storm found while building this evaluation: the same 5-minute outage with
    # the server's batch time budget switched off (BATCH_BUDGET_MS very large).
    for budget, label in ((10**9, "off"), (1000, "on")):
        start, out_ms = scaled(120_000), 300_000
        cfg = base_phones(tripPrefix=f"T-E1-budget-{label}", durationMs=start + scaled(out_ms) + scaled(120_000),
                          outages=[{"startMs": start, "endMs": start + scaled(out_ms), "mode": "silent"}])
        run_one(f"E1-batch-budget-{label}", cfg, server_env={"BATCH_BUDGET_MS": budget})


def e1_restart() -> None:
    """App restarted (tab closed and reopened) in the middle of a 5-minute outage."""
    for persistent in (True, False):
        start, out_ms = scaled(120_000), 300_000
        cfg = base_phones(tripPrefix=f"T-E1-restart-{persistent}", durationMs=start + scaled(out_ms) + scaled(120_000),
                          outages=[{"startMs": start, "endMs": start + scaled(out_ms), "mode": "silent"}],
                          restartAtMs=start + scaled(out_ms) // 2, persistent=persistent)
        run_one(f"E1-restart-{'indexeddb' if persistent else 'memory'}", cfg)


def e1_real() -> None:
    """E1 at full length, no time scaling: 50 phones, 10 s heartbeat, a 5-minute outage."""
    cfg = {"phones": 50, "kind": "outbox", "tripPrefix": "T-E1real", "heartbeatMs": 10_000,
           "durationMs": 120_000 + 300_000 + 60_000, "drainMs": 60_000, "timeoutMs": 8_000,
           "backoffBaseMs": 1_000, "backoffCapMs": 60_000, "reconnectJitterMs": 2_000, "jitter": True,
           "outages": [{"startMs": 120_000, "endMs": 420_000, "mode": "silent"}]}
    run_one("E1-real-5min-outbox", cfg)


def e2(quick: bool) -> None:
    """E2: 200 phones get signal back at the same instant, each with 10 check-ins of
    backlog, against one replica with a fixed, small capacity (4 requests at once, +10 ms
    per check-in), so it is overloaded on purpose."""
    n = 100 if quick else 200
    server = {"MAX_IN_FLIGHT": 4, "FAULT_LATENCY_MS": 10, "RETRY_AFTER_S": 1, "RECONCILER_ENABLED": "false"}
    offline_until = scaled(100_000)
    variants = {
        "lockstep": dict(jitter=False, reconnectJitterMs=0),        # no randomness anywhere
        "jitter-retries": dict(jitter=True, reconnectJitterMs=0),   # random backoff/Retry-After only
        "full": dict(jitter=True, reconnectJitterMs=2_000),         # + spread the reconnect itself
    }
    for name, v in variants.items():
        cfg = base_phones(phones=n, tripPrefix=f"T-E2-{name}", startTogether=True,
                          durationMs=offline_until + 1, drainMs=120_000,
                          backoffBaseMs=1_000, backoffCapMs=8_000, timeoutMs=8_000,
                          outages=[{"startMs": 0, "endMs": offline_until, "mode": "known"}], **v)
        run_one(f"E2-{name}", cfg, server_env=server)


def e3(quick: bool) -> None:
    """E3: the server fails part-way (20 % of SQS sends fail after storing) and 30 % of
    phones die at their first failure. Clean-up task off / on / on with the leader killed."""
    server = {"FAULT_SEND_FAIL_RATE": 0.2, "RECONCILE_INTERVAL_S": 0.5, "RECONCILE_GRACE_S": 2,
              "LEADER_LEASE_MS": 3000}
    phones = base_phones(tripPrefix="T-E3", durationMs=scaled(300_000), drainMs=scaled(180_000),
                         dieOnFailureFraction=0.3)
    run_one("E3-cleanup-off", {**phones, "tripPrefix": "T-E3off"}, replicas=2,
            server_env={**server, "RECONCILER_ENABLED": "false"}, settle_ms=8000, sample=True)
    run_one("E3-cleanup-on", {**phones, "tripPrefix": "T-E3on"}, replicas=2, server_env=server,
            settle_ms=8000, sample=True)
    run_one("E3-cleanup-on-leader-killed", {**phones, "tripPrefix": "T-E3kill"}, replicas=2, server_env=server,
            kill_leader_at_ms=scaled(150_000), settle_ms=8000, sample=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("experiments", nargs="*", default=["E1", "E1-restart", "E1-real", "E2", "E3"])
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    print(f"Redis {REDIS_URL}, AWS {AWS_ENDPOINT}")
    for e in a.experiments:
        {"E1": lambda: e1(a.quick), "E1-restart": e1_restart, "E1-real": e1_real, "E2": lambda: e2(a.quick), "E3": lambda: e3(a.quick)}[e]()


if __name__ == "__main__":
    main()
