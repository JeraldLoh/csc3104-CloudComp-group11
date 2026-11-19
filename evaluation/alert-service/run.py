"""Alert Service evaluation (issue 5.6): runs the experiments against real replicas and saves the results.

    python evaluation/alert-service/run.py A1            # the same problem reported many times at once
    python evaluation/alert-service/run.py A2            # reminders with several replicas checking
    python evaluation/alert-service/run.py A1 A2 --quick # smaller, for a first try (about 5 minutes)
    python evaluation/alert-service/run.py A1 --only killed   # just the conditions whose name contains "killed"

Each condition starts its own replicas of the real service (replica.py) on 127.0.0.1:18101 and up, with fresh
DynamoDB tables named for that condition, and deletes them afterwards (--keep leaves them). Pushes are recorded in
Redis, not sent. Ground truth comes from DynamoDB and the recorded pushes, never from the replicas' answers alone.
Writes results/<experiment>-<time>.json; report.py turns the newest of each into RESULTS.md.

Needs LocalStack (AWS_ENDPOINT_URL, default http://localhost:4566) and Redis (REDIS_URL, default
redis://localhost:6379/8; only the key eval:pushes is used). See README.md.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import httpx
import redis
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RESULTS = HERE / "results"
REPLICA = HERE / "replica.py"
FIRST_PORT = 18101
PUSHES_KEY = "eval:pushes"  # replica.py
TOKEN = "eval-browser-" + "0" * 40  # the one caregiver browser every push goes to

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/8")
ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")
REGION = os.getenv("AWS_DEFAULT_REGION", "ap-southeast-1")
BASE_ENV = {
    "AUTH_MODE": "dev",  # setting up the traveller needs a caregiver (X-Dev-User); /internal/problems needs none
    "REDIS_URL": REDIS_URL,
    "AWS_ENDPOINT_URL": ENDPOINT,
    "AWS_DEFAULT_REGION": REGION,
    "AWS_ACCESS_KEY_ID": os.getenv("AWS_ACCESS_KEY_ID", "test"),
    "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    "DISRUPTIONS_CONSUMER_ENABLED": "false",
    "FIREBASE_CREDENTIALS": "",
    "PYTHONUNBUFFERED": "1",
}

# Every condition: a name, how many replicas, "conditional" (as deployed) or "naive" writes, the push outbox on (as
# deployed) or off (the design before it) (replica.py), and optionally a replica killed every `crashEvery` s and
# started again `crashDown` s later (as Kubernetes restarts a pod).
DEPLOYED = {"writes": "conditional", "outbox": "on"}
NAIVE = {"writes": "naive", "outbox": "off"}
A1_CONDITIONS = [
    {"name": "conditional + outbox, 1 replica", "replicas": 1, **DEPLOYED},
    {"name": "conditional + outbox, 2 replicas", "replicas": 2, **DEPLOYED},
    {"name": "conditional + outbox, 4 replicas", "replicas": 4, **DEPLOYED},
    {"name": "read-then-write, no outbox, 1 replica", "replicas": 1, **NAIVE},
    {"name": "read-then-write, no outbox, 4 replicas", "replicas": 4, **NAIVE},
    {"name": "read-then-write + outbox, 4 replicas", "replicas": 4, "writes": "naive", "outbox": "on"},
    {"name": "conditional, no outbox, 2 replicas, one killed every 5 s", "replicas": 2, "writes": "conditional",
     "outbox": "off", "crashEvery": 5, "crashDown": 3},
    {"name": "conditional + outbox, 2 replicas, one killed every 5 s", "replicas": 2, **DEPLOYED,
     "crashEvery": 5, "crashDown": 3},
]
A2_CONDITIONS = [
    {"name": "conditional + outbox, 1 replica", "replicas": 1, **DEPLOYED},
    {"name": "conditional + outbox, 2 replicas", "replicas": 2, **DEPLOYED},
    {"name": "conditional + outbox, 4 replicas", "replicas": 4, **DEPLOYED},
    {"name": "read-then-write, no outbox, 4 replicas", "replicas": 4, **NAIVE},
    {"name": "read-then-write + outbox, 4 replicas", "replicas": 4, "writes": "naive", "outbox": "on"},
    {"name": "conditional + outbox, 2 replicas, one killed every 4 s", "replicas": 2, **DEPLOYED,
     "crashEvery": 4, "crashDown": 2},
]

_deserializer = TypeDeserializer()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def percentiles(values: list[float]) -> dict | None:
    """p50, p95, max (nearest rank), rounded."""
    if not values:
        return None
    s = sorted(values)
    at = lambda q: s[min(len(s) - 1, max(0, round(q * len(s)) - 1))]
    tidy = lambda v: round(v, 1) + 0.0  # + 0.0 turns -0.0 into 0.0
    return {"n": len(s), "p50": tidy(at(0.50)), "p95": tidy(at(0.95)), "max": tidy(s[-1])}


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------- the replicas
def table_env(tag: str) -> dict:
    """Fresh tables for one condition, so its ground truth is only its own."""
    return {"INCIDENTS_TABLE": f"eval-{tag}-incidents", "TRAVELLERS_TABLE": f"eval-{tag}-travellers",
            "CAREGIVER_LINKS_TABLE": f"eval-{tag}-links", "SAFE_LOCATIONS_TABLE": f"eval-{tag}-places",
            "DEVICE_TOKENS_TABLE": f"eval-{tag}-devices"}


class Cluster:
    """`n` replicas of the real service on 127.0.0.1:18101 and up, sharing one set of tables and Redis."""

    def __init__(self, n: int, env: dict, log_dir: Path):
        self.n = n
        self.env = env
        self.log_dir = log_dir
        self.procs: list[subprocess.Popen | None] = [None] * n
        self.urls = [f"http://127.0.0.1:{FIRST_PORT + i}" for i in range(n)]

    def _start(self, i: int, create: bool = False) -> None:
        env = {**os.environ, **self.env, "CREATE_RESOURCES": "true" if create else "false"}
        with open(self.log_dir / f"r{i + 1}.log", "a") as log:
            self.procs[i] = subprocess.Popen(
                [sys.executable, str(REPLICA), "--port", str(FIRST_PORT + i), "--name", f"r{i + 1}"],
                env=env, stdout=log, stderr=subprocess.STDOUT, cwd=REPO)

    def start(self) -> None:
        self._start(0, create=True)  # the first one creates the tables
        self.wait_ready(0)
        for i in range(1, self.n):
            self._start(i)
        for i in range(1, self.n):
            self.wait_ready(i)

    def wait_ready(self, i: int, timeout_s: float = 120) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.procs[i].poll() is not None:
                raise SystemExit(f"replica r{i + 1} stopped while starting: see {self.log_dir / f'r{i + 1}.log'}")
            try:
                if httpx.get(f"{self.urls[i]}/ready", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise SystemExit(f"replica r{i + 1} wasn't ready after {timeout_s:.0f} s: see {self.log_dir / f'r{i + 1}.log'}")

    def kill(self, i: int) -> None:
        """Like a pod that crashes: no shutdown, in-flight requests and background sends are lost."""
        self.procs[i].kill()
        self.procs[i].wait()

    def restart(self, i: int) -> None:
        self._start(i)

    def stop(self) -> None:
        for p in self.procs:
            if p and p.poll() is None:
                p.kill()
                p.wait()


class Crasher(threading.Thread):
    """Kills one replica every `every_s` s, taking turns, and starts it again `down_s` s later."""

    def __init__(self, cluster: Cluster, every_s: float, down_s: float):
        super().__init__(daemon=True)
        self.cluster, self.every_s, self.down_s = cluster, every_s, down_s
        self.kills: list[float] = []
        self._stop = threading.Event()

    def run(self) -> None:
        turn = 0
        while not self._stop.wait(self.every_s):
            i = turn % self.cluster.n
            turn += 1
            self.cluster.kill(i)
            self.kills.append(time.time())
            print(f"    killed r{i + 1}", flush=True)
            self._stop.wait(self.down_s)
            self.cluster.restart(i)

    def stop(self) -> None:
        self._stop.set()
        self.join()


class Sender:
    """Posts problem reports to the replicas in turn, as a Kubernetes Service spreads requests over pods. A request
    that fails (a replica down, or killed mid-request) is sent again to the next replica, as the Coordinator retries
    a report that got no answer (its SQS message comes back)."""

    def __init__(self, urls: list[str], attempts: int = 60, pause_s: float = 0.25):
        self.urls = urls
        self.attempts = attempts
        self.pause_s = pause_s
        self.client = httpx.Client(timeout=10, limits=httpx.Limits(max_connections=200, max_keepalive_connections=200))

    def post(self, turn: int, body: dict) -> dict:
        for attempt in range(self.attempts):
            url = self.urls[(turn + attempt) % len(self.urls)]
            started = time.perf_counter()
            try:
                resp = self.client.post(f"{url}/internal/problems", json=body)
                if resp.status_code == 200:
                    return {"ok": True, "created": resp.json()["created"], "attempts": attempt + 1,
                            "ms": (time.perf_counter() - started) * 1000}
            except httpx.HTTPError:
                pass
            time.sleep(self.pause_s)
        return {"ok": False, "created": False, "attempts": self.attempts, "ms": None}


def problem_report(traveller_id: str, trip_id: str, problem_type: str) -> dict:
    return {"reportId": f"eval-{uuid.uuid4().hex[:12]}", "source": "coordinator", "travellerId": traveller_id,
            "tripId": trip_id, "stepIndex": 2, "problemType": problem_type, "confidence": 0.9,
            "location": {"lat": 1.3521, "lng": 103.8198}, "batteryPct": 80, "timestamp": now_iso()}


class Stores:
    """Ground truth: the incidents table and the recorded pushes."""

    def __init__(self):
        self.db = boto3.client("dynamodb", region_name=REGION, endpoint_url=ENDPOINT,
                               aws_access_key_id=BASE_ENV["AWS_ACCESS_KEY_ID"],
                               aws_secret_access_key=BASE_ENV["AWS_SECRET_ACCESS_KEY"])
        self.redis = redis.Redis.from_url(REDIS_URL, socket_timeout=5)

    def check(self) -> None:
        try:
            self.redis.ping()
        except redis.RedisError as e:
            raise SystemExit(f"Can't reach Redis at {REDIS_URL} ({e}). Port-forward it: "
                             "kubectl port-forward svc/cloud-redis 6379:6379 -n csc3104")
        try:
            self.db.list_tables(Limit=1)
        except Exception as e:
            raise SystemExit(f"Can't reach DynamoDB at {ENDPOINT} ({e}). Port-forward LocalStack: "
                             "kubectl port-forward svc/cloud-localstack 4566:4566 -n csc3104")

    def incidents(self, table: str) -> list[dict]:
        found, kwargs = [], {"TableName": table, "ConsistentRead": True}
        while True:
            resp = self.db.scan(**kwargs)
            found += [_plain({k: _deserializer.deserialize(v) for k, v in item.items()}) for item in resp["Items"]]
            if "LastEvaluatedKey" not in resp:
                return found
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def clear_pushes(self) -> None:
        self.redis.delete(PUSHES_KEY)

    def pushes(self) -> list[dict]:
        return [json.loads(p) for p in self.redis.lrange(PUSHES_KEY, 0, -1)]

    def wait_outbox_empty(self, table: str, limit_s: float = 120) -> int:
        """Waits until no incident has a push still pending (the outbox, incidents.py). Returns how many still do."""
        deadline = time.time() + limit_s
        while True:
            pending = sum(1 for i in self.incidents(table) if i.get("pushPending"))
            if pending == 0 or time.time() > deadline:
                return pending
            time.sleep(1)

    def settled_pushes(self, quiet_s: float = 5, limit_s: float = 300) -> list[dict]:
        """The pushes once no new one has come for `quiet_s`: each replica sends on 2 background threads, so a burst
        (the naive writes' duplicates) can take a while to leave. Reading earlier would count queued pushes as lost."""
        count, changed, deadline = -1, time.time(), time.time() + limit_s
        while time.time() < deadline:
            now_count = self.redis.llen(PUSHES_KEY)
            if now_count != count:
                count, changed = now_count, time.time()
            elif time.time() - changed >= quiet_s:
                break
            time.sleep(0.5)
        return self.pushes()

    def delete_tables(self, env: dict) -> None:
        for name in table_env_names(env):
            try:
                self.db.delete_table(TableName=name)
            except ClientError:
                pass


def table_env_names(env: dict) -> list[str]:
    return [v for k, v in env.items() if k.endswith("_TABLE")]


def setup_traveller(url: str) -> str:
    """One traveller with one caregiver, whose one browser is registered for push."""
    caregiver = f"G-eval{uuid.uuid4().hex[:8]}"
    headers = {"X-Dev-User": caregiver}
    traveller = httpx.post(f"{url}/api/travellers", json={"name": "Eval"}, headers=headers, timeout=10)
    traveller.raise_for_status()
    device = httpx.post(f"{url}/api/devices", json={"token": TOKEN}, headers=headers, timeout=10)
    device.raise_for_status()
    if not device.json()["pushEnabled"]:
        raise SystemExit("push is off in the replica: replica.py should have put the recorder in")
    return traveller.json()["travellerId"]


def run_condition(cond: dict, tag: str, stores: Stores, keep: bool, extra_env: dict, body) -> dict:
    """Starts the replicas for one condition, runs `body(cluster, traveller, crasher)`, and always cleans up."""
    env = {**BASE_ENV, **table_env(tag), "EVAL_WRITES": cond["writes"], "EVAL_OUTBOX": cond["outbox"], **extra_env}
    log_dir = RESULTS / "logs" / tag
    log_dir.mkdir(parents=True, exist_ok=True)
    stores.clear_pushes()
    cluster = Cluster(cond["replicas"], env, log_dir)
    crasher = None
    try:
        cluster.start()
        traveller = setup_traveller(cluster.urls[0])
        if cond.get("crashEvery"):
            crasher = Crasher(cluster, cond["crashEvery"], cond["crashDown"])
        out = body(cluster, traveller, crasher, env["INCIDENTS_TABLE"])
        out["kills"] = len(crasher.kills) if crasher else 0
        if cond["outbox"] == "off":
            out["takenOver"] = out["pendingAtEnd"] = None  # no outbox to read them from
        return {**cond, **out}
    finally:
        if crasher and crasher.is_alive():
            crasher.stop()
        cluster.stop()
        if not keep:
            stores.delete_tables(env)


# ---------------------------------------------------------------------------- A1: duplicates
def a1(args, stores: Stores, run_id: str) -> list[dict]:
    """The same problem reported `copies` times at once, for `problems` different problems, spread over the
    replicas. As deployed, each problem should give exactly one incident, one "created" answer and one push, and
    its detectionCount should count every report."""
    problems, copies = (20, 10) if args.quick else (args.problems, args.copies)
    out = []
    for n, cond in enumerate(selected(A1_CONDITIONS, args.only)):
        tag = f"{run_id}-a1-{n}".lower()
        print(f"A1 {cond['name']}: {problems} problems x {copies} copies", flush=True)

        def body(cluster, traveller, crasher, table):
            sender = Sender(cluster.urls)
            trips = [f"T-{tag}-{p}" for p in range(problems)]
            first_sent: dict[int, float] = {}

            def one(p: int, turn: int):
                first_sent.setdefault(p, time.time())
                return p, sender.post(turn, problem_report(traveller, trips[p], "off_route"))

            started = time.time()
            if crasher:
                crasher.start()
            # `copies` threads: the copies of one problem are in flight together, each to the next replica
            with ThreadPoolExecutor(copies) as pool:
                rows = list(pool.map(lambda pc: one(*pc), [(p, p * copies + c) for p in range(problems)
                                                           for c in range(copies)]))
            duration = time.time() - started
            if crasher:
                crasher.stop()
            if cond["outbox"] == "on":
                stores.wait_outbox_empty(table)  # pushes a killed replica left are sent by another (PUSH_LEASE_S)
            pushes = stores.settled_pushes()
            return {"durationS": round(duration, 1),
                    **a1_summary(rows, trips, first_sent, stores.incidents(table), pushes)}

        result = run_condition(cond, tag, stores, args.keep, {"ESCALATION_ENABLED": "false"}, body)
        print_a1(result)
        out.append(result)
    return out


def a1_summary(rows: list[tuple[int, dict]], trips: list[str], first_sent: dict[int, float],
               incidents: list[dict], pushes: list[dict]) -> dict:
    by_trip = {i["tripId"]: i for i in incidents}
    per = []
    for p, trip in enumerate(trips):
        mine = [r for q, r in rows if q == p]
        incident = by_trip.get(trip)
        pushed = sorted(x["t"] for x in pushes if x["kind"] == "alert" and f"#{trip}#" in (x["incidentId"] or ""))
        per.append({
            "accepted": sum(r["ok"] for r in mine),
            "created": sum(r["created"] for r in mine),
            "detections": incident["detectionCount"] if incident else 0,
            "pushes": len(pushed),
            "firstPushMs": (pushed[0] - first_sent[p]) * 1000 if pushed else None,
        })
    sent = len(rows)
    accepted = sum(x["accepted"] for x in per)
    return {
        "problems": len(trips),
        "reportsSent": sent,
        "accepted": accepted,
        "failed": sent - accepted,
        "retries": sum(r["attempts"] - 1 for _, r in rows),
        "incidents": len(incidents),
        "missingIncidents": sum(1 for t in trips if t not in by_trip),
        "createdAnswers": sum(x["created"] for x in per),
        "extraCreated": sum(max(0, x["created"] - 1) for x in per),
        "pushes": sum(x["pushes"] for x in per),
        "duplicatePushes": sum(max(0, x["pushes"] - 1) for x in per),
        "missingPushes": sum(1 for x in per if x["pushes"] == 0),
        "problemsAlertedTwice": sum(1 for x in per if x["pushes"] > 1),
        **outbox_summary(incidents),
        "detectionsRecorded": sum(x["detections"] for x in per),
        # a lost update: two writers both read N and both wrote N+1, or a racing create reset the count to 1
        "lostDetections": sum(max(0, x["accepted"] - x["detections"]) for x in per),
        # a report saved, then its answer lost to a crash, so it was sent again (at-least-once)
        "extraDetections": sum(max(0, x["detections"] - x["accepted"]) for x in per),
        "requestMs": percentiles([r["ms"] for _, r in rows if r["ok"]]),
        "firstPushMs": percentiles([x["firstPushMs"] for x in per if x["firstPushMs"] is not None]),
        "perProblem": per,
    }


def outbox_summary(incidents: list[dict]) -> dict:
    """From the push outbox on each incident: pushes another replica had to take over (claimed more than once), and
    pushes still not sent when the condition ended."""
    return {"takenOver": sum(1 for i in incidents if i.get("pushAttempts", 0) > 1),
            "pendingAtEnd": sum(1 for i in incidents if i.get("pushPending"))}


def print_a1(r: dict) -> None:
    print(f"    incidents {r['incidents']}/{r['problems']}, 'created' answers {r['createdAnswers']}, "
          f"pushes {r['pushes']} (duplicates {r['duplicatePushes']}, missing {r['missingPushes']}, "
          f"taken over {r['takenOver']}, still pending {r['pendingAtEnd']}), "
          f"detections {r['detectionsRecorded']}/{r['accepted']} (lost {r['lostDetections']}, "
          f"extra {r['extraDetections']}), failed {r['failed']}, kills {r['kills']}, "
          f"request p95 {(r['requestMs'] or {}).get('p95')} ms, {r['durationS']} s", flush=True)


# ---------------------------------------------------------------------------- A2: reminders
def a2(args, stores: Stores, run_id: str) -> list[dict]:
    """`alerts` urgent alerts nobody acknowledges, with every replica checking for due reminders. Each alert should
    get exactly ESCALATE_MAX reminders, each about ESCALATE_AFTER_S after the one before."""
    alerts = 20 if args.quick else args.alerts
    after, check, most = args.after, args.check, args.max
    # Scaled like the reminders (deployed: a 15 s lease, checked every 5 s)
    timing = {"ESCALATION_ENABLED": "true", "ESCALATE_AFTER_S": str(after), "ESCALATION_CHECK_S": str(check),
              "ESCALATE_MAX": str(most), "ESCALATE_WITHIN_S": "3600",
              "PUSH_LEASE_S": str(args.lease), "PUSH_RETRY_CHECK_S": str(args.lease / 2)}
    out = []
    for n, cond in enumerate(selected(A2_CONDITIONS, args.only)):
        tag = f"{run_id}-a2-{n}".lower()
        print(f"A2 {cond['name']}: {alerts} urgent alerts, a reminder {after} s apart up to {most}", flush=True)

        def body(cluster, traveller, crasher, table):
            sender = Sender(cluster.urls)
            trips = [f"T-{tag}-{k}" for k in range(alerts)]
            if crasher:
                crasher.start()
            with ThreadPoolExecutor(10) as pool:
                answers = list(pool.map(lambda k: sender.post(k, problem_report(traveller, trips[k], "no_response")),
                                        range(alerts)))
            # Wait until every alert has had all its reminders, or until it surely should have...
            deadline = time.time() + after * (most + 1) + check * (most + 2) + 30
            while time.time() < deadline:
                if all(i.get("escalationLevel") == most for i in stores.incidents(table)):
                    break
                time.sleep(1)
            # ...then long enough again for any extra reminder to show
            time.sleep(after + 3 * check)
            if crasher:
                crasher.stop()
            if cond["outbox"] == "on":
                stores.wait_outbox_empty(table)
            pushes = stores.settled_pushes(quiet_s=2)
            return a2_summary(trips, answers, stores.incidents(table), pushes, after, most)

        result = run_condition(cond, tag, stores, args.keep, timing, body)
        result.update({"afterS": after, "checkS": check, "max": most, "leaseS": args.lease})
        print_a2(result)
        out.append(result)
    return out


def a2_summary(trips: list[str], answers: list[dict], incidents: list[dict], pushes: list[dict],
               after: float, most: int) -> dict:
    by_trip = {i["tripId"]: i for i in incidents}
    per, lateness = [], []
    for trip in trips:
        mine = sorted((x for x in pushes if x["kind"] == "alert" and f"#{trip}#" in (x["incidentId"] or "")),
                      key=lambda x: x["t"])
        first = [x for x in mine if not x["title"].startswith("Still")]
        reminders = [x for x in mine if x["title"].startswith("Still")]
        # How late each reminder was: its gap from the alert before it, minus ESCALATE_AFTER_S. A duplicate (within
        # 1 s of the one before) isn't a new level, so it's counted as extra, not timed.
        previous = first[0]["t"] if first else None
        for x in reminders:
            if previous is not None and x["t"] - previous > 1:
                lateness.append(x["t"] - previous - after)
                previous = x["t"]
        incident = by_trip.get(trip)
        per.append({"initial": len(first), "reminders": len(reminders),
                    "level": incident.get("escalationLevel") if incident else None})
    return {
        "alerts": len(trips),
        "alertsCreated": sum(1 for a in answers if a["ok"] and a["created"]),
        "initialPushes": sum(x["initial"] for x in per),
        "reminders": sum(x["reminders"] for x in per),
        "expectedReminders": most * len(trips),
        "extraReminders": sum(max(0, x["reminders"] - most) for x in per),
        "missingReminders": sum(max(0, most - x["reminders"]) for x in per),
        "alertsWithExtra": sum(1 for x in per if x["reminders"] > most),
        "alertsWithMissing": sum(1 for x in per if x["reminders"] < most),
        "atFinalLevel": sum(1 for x in per if x["level"] == most),
        **outbox_summary(incidents),
        "lateS": percentiles(lateness),
        "perAlert": per,
    }


def print_a2(r: dict) -> None:
    print(f"    reminders {r['reminders']}/{r['expectedReminders']} (extra {r['extraReminders']} on "
          f"{r['alertsWithExtra']} alerts, missing {r['missingReminders']}, taken over {r['takenOver']}, "
          f"still pending {r['pendingAtEnd']}), at level {r['max']}: "
          f"{r['atFinalLevel']}/{r['alerts']}, late {r['lateS']}, kills {r['kills']}", flush=True)


# ---------------------------------------------------------------------------- main
def selected(conditions: list[dict], only: str | None) -> list[dict]:
    chosen = [c for c in conditions if not only or only.lower() in c["name"].lower()]
    if not chosen:
        raise SystemExit(f"no condition's name contains {only!r}")
    return chosen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("experiments", nargs="+", choices=["A1", "A2"])
    parser.add_argument("--quick", action="store_true", help="fewer problems and alerts, for a first try")
    parser.add_argument("--only", help="run only the conditions whose name contains this")
    parser.add_argument("--keep", action="store_true", help="keep each condition's tables (to look at them)")
    parser.add_argument("--problems", type=int, default=300, help="A1: different problems")
    parser.add_argument("--copies", type=int, default=20, help="A1: reports of each problem, sent at once")
    parser.add_argument("--alerts", type=int, default=50, help="A2: urgent alerts")
    parser.add_argument("--after", type=int, default=8, help="A2: ESCALATE_AFTER_S (deployed: 120)")
    parser.add_argument("--check", type=float, default=1, help="A2: ESCALATION_CHECK_S (deployed: 15)")
    parser.add_argument("--max", type=int, default=3, help="A2: ESCALATE_MAX (deployed: 3)")
    parser.add_argument("--lease", type=float, default=1, help="A2: PUSH_LEASE_S (deployed: 15)")
    args = parser.parse_args()

    stores = Stores()
    stores.check()
    RESULTS.mkdir(exist_ok=True)
    for experiment in args.experiments:
        run_id = datetime.now().strftime("%m%d%H%M%S")
        conditions = (a1 if experiment == "A1" else a2)(args, stores, run_id)
        path = RESULTS / f"{experiment}-{run_id}.json"
        path.write_text(json.dumps({"experiment": experiment, "runId": run_id, "quick": args.quick,
                                    "endpoint": ENDPOINT, "conditions": conditions}, indent=1))
        print(f"saved {path.relative_to(REPO)}", flush=True)


if __name__ == "__main__":
    main()
