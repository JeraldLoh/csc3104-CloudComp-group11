"""Talks to the local stack (services/analysis-dev) the way the rest of CareRoute does.

Check-ins go in like the Check-in Service sends them: trip:{id}:recent and :context in Redis
(Member 4's simulator helpers), then the check-in on careroute-checkins. Results come out where
the system keeps them: the Coordinator's decision log in Redis and the Alert Service's incidents.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import boto3
import redis

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.simulator.geometry import distance_m                                  # noqa: E402
from tools.simulator.replay import record_checkin, seed_trip                     # noqa: E402  (Member 4's)
from tools.simulator.route import build_route, build_track                       # noqa: E402
from tools.simulator.scenarios import OFF_ROUTE_LABEL_M, generate, strip_labels   # noqa: E402

ROUTE_PATH = build_track().path   # the planned route, a point every 20 m

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SQS_URL = os.getenv("AWS_ENDPOINT_URL", "http://localhost:9324")
COORDINATOR_URL = os.getenv("COORDINATOR_URL", "http://localhost:8000")
ALERT_URL = os.getenv("ALERT_SERVICE_URL", "http://localhost:8004")
QUEUE = os.getenv("CHECKINS_QUEUE", "careroute-checkins")
TTL_S = 86400
DETECTORS = ("route-corridor", "heading-speed", "dwell-time", "route-progress")
# C2's fault switches (compose.c2.yaml), one in front of each real detector.
SWITCH_URLS = {"route-corridor": "http://localhost:9101", "dwell-time": "http://localhost:9102",
               "heading-speed": "http://localhost:9103", "route-progress": "http://localhost:9104"}
WEIGHTS_KEY = "detector:weights"


COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT", "careroute-analysis")
COMPOSE_FILE = ROOT / "services" / "analysis-dev" / "compose.yaml"


def container(service: str) -> str:
    """The container docker compose made for a service of the local stack."""
    return f"{COMPOSE_PROJECT}-{service}-1"


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


def get_json(url: str, timeout: float = 5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def journey(scenario: str, seed: int, run_id: str) -> list[dict]:
    """A labelled simulated journey (with phone-like gaps and missing fields) under its own
    trip and traveller, so runs never share risk, dedup markers or incidents."""
    trip, traveller = f"T-{run_id}-{scenario}-{seed}", f"U-{run_id}-{scenario}-{seed}"
    return [{**c, "tripId": trip, "travellerId": traveller} for c in generate(scenario, seed, browser=True)]


NEAR_LEG = "off_route_near"   # off_route, but never more than 100 m from some part of the route


def case_of(scenario: str, checkins: list[dict]) -> tuple[str, float | None]:
    """The case a journey is scored under, and (off_route only) the farthest it got from the route.

    The simulator labels off_route once the traveller is 100 m sideways from where they should
    be. The contract (and route-corridor) measure to the nearest point of the whole route. A walk
    towards another leg of the route (the road north to the bus stop) is labelled off_route but
    never leaves the corridor: route-corridor can't see it by definition, only route-progress
    can. Those journeys are scored as their own case, so the two kinds aren't mixed."""
    if scenario != "off_route":
        return scenario, None
    labelled = [c for c in checkins if c["expected"] == "off_route"]
    farthest = max(min(distance_m((c["lat"], c["lng"]), p) for p in ROUTE_PATH) for c in labelled)
    return (NEAR_LEG if farthest <= OFF_ROUTE_LABEL_M else scenario), round(farthest)


class Stack:
    def __init__(self):
        self.redis = redis.Redis.from_url(REDIS_URL, decode_responses=True)
        self.sqs = boto3.client("sqs", endpoint_url=SQS_URL, region_name="ap-southeast-1",
                                aws_access_key_id="local", aws_secret_access_key="local")
        self.queue_url = self.sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"]

    def check(self) -> None:
        """Fail early, with a useful message, if the stack isn't up."""
        self.redis.ping()
        for name, url in (("coordinator", f"{COORDINATOR_URL}/ready"), ("alert-service", f"{ALERT_URL}/health")):
            try:
                get_json(url)
            except Exception as e:  # noqa: BLE001
                raise SystemExit(f"{name} isn't answering at {url} ({e}). Start the stack: "
                                 "docker compose -f services/analysis-dev/compose.yaml up --build -d")

    def start_trip(self, first: dict) -> None:
        seed_trip(self.redis, first["tripId"], build_route(), strip_labels(first), ttl_s=TTL_S)

    def send(self, checkin: dict) -> None:
        body = strip_labels(checkin)
        record_checkin(self.redis, body, ttl_s=TTL_S)
        self.sqs.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(body))

    def wait_decided(self, trip: str, seq: int, timeout_s: float) -> bool:
        key = f"decided:{trip}:{seq}"
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if self.redis.exists(key):
                return True
            time.sleep(0.005)
        return False

    def latest_decision(self, traveller: str) -> dict | None:
        raw = self.redis.lindex(f"decisions:{traveller}", 0)
        return json.loads(raw) if raw else None

    def incidents(self, traveller: str) -> list[dict]:
        return get_json(f"{ALERT_URL}/api/incidents?travellerId={traveller}")

    def switch(self, detector: str, **control) -> dict:
        """Set one detector's fault switch, e.g. switch("dwell-time", mode="error")."""
        req = urllib.request.Request(f"{SWITCH_URLS[detector]}/control", data=json.dumps(control).encode(),
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.load(resp)
        except OSError as e:
            raise SystemExit(f"no fault switch for {detector} at {SWITCH_URLS[detector]} ({e}): start the stack "
                             "with evaluation/coordinator/compose.c2.yaml")

    # --- C4: crashing and restarting containers of the local stack ------------------------------

    def kill(self, service: str) -> None:
        """SIGKILL, no clean shutdown: like a pod that crashes or is OOM-killed."""
        docker("kill", container(service))

    def stop(self, service: str) -> None:
        docker("stop", "-t", "2", container(service))

    def start(self, service: str) -> None:
        docker("start", container(service))

    def running(self, service: str) -> bool:
        try:
            return docker("inspect", "-f", "{{.State.Running}}", container(service)).strip() == "true"
        except subprocess.CalledProcessError:
            return False

    def logs(self, service: str, since: str) -> str:
        """Everything the service logged since `since` (ISO 8601), across restarts."""
        out = subprocess.run(["docker", "logs", "--since", since, container(service)],
                             capture_output=True, text=True, check=True)
        return out.stdout + out.stderr

    def dlq_depth(self) -> int:
        url = self.sqs.get_queue_url(QueueName=f"{QUEUE}-dlq")["QueueUrl"]
        attrs = self.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])
        return int(attrs["Attributes"]["ApproximateNumberOfMessages"])

    # --- C5: the Coordinator in another REDUNDANCY_MODE -----------------------------------------

    def use_mode(self, mode: str, ready_s: float = 60) -> None:
        """Restart the Coordinator with REDUNDANCY_MODE=mode, as the only replica: --remove-orphans
        also stops C4's second replica and C2's switches, which would otherwise keep their mode."""
        subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "--remove-orphans", "coordinator"],
                       env={**os.environ, "REDUNDANCY_MODE": mode}, capture_output=True, text=True, check=True)
        end = time.monotonic() + ready_s
        while True:
            try:
                return self.check()
            except SystemExit:
                if time.monotonic() > end:
                    raise
                time.sleep(1)

    def decision_for(self, traveller: str, seq: int) -> dict | None:
        """The decision record of one check-in, if it's still in the traveller's recent list."""
        for raw in self.redis.lrange(f"decisions:{traveller}", 0, -1):
            d = json.loads(raw)
            if d.get("seq") == seq:
                return d
        return None

    def set_weight(self, detector: str, reliability: float) -> None:
        self.redis.hset(WEIGHTS_KEY, detector, reliability)

    def clear_weights(self) -> None:
        self.redis.delete(WEIGHTS_KEY)


def play(stack: Stack, checkins: list[dict], timeout_s: float = 10) -> list[dict]:
    """Send a journey one check-in at a time, each only after the previous one was decided, as
    a phone checking in every 10 s would (the detectors read the trip's recent check-ins, so
    they must not see later ones early). One row per check-in."""
    stack.start_trip(checkins[0])
    rows = []
    for c in checkins:
        start = time.perf_counter()
        stack.send(c)
        decided = stack.wait_decided(c["tripId"], c["seq"], timeout_s)
        took_ms = (time.perf_counter() - start) * 1000
        d = stack.latest_decision(c["travellerId"]) if decided else None
        if d is not None and d.get("seq") != c["seq"]:
            d = None
        rows.append({"seq": c["seq"], "timestamp": c["timestamp"], "expected": c["expected"],
                     "endToEndMs": round(took_ms, 1) if decided else None, "decision": d})
    return rows
