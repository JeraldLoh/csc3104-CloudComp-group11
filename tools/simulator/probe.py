# steady calls to every detector with one line of counts per second, to measure availability while faults happen

import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Sequence

from tools.simulator.replay import post_json, record_checkin, seed_trip
from tools.simulator.route import build_route
from tools.simulator.scenarios import generate, strip_labels

TRIP_CHECKINS = 20  # check-ins of a normal trip kept in redis so detectors give real votes
BUDGET_MS = 500  # the coordinator treats a slower answer as an abstain
TIMEOUT_S = 2.0
KEY_TTL_S = 600


# answers counted in one second
class Second:
    def __init__(self):
        self.sent = 0
        self.in_budget = 0  # any answer inside the budget
        self.votes = 0  # normal or problem inside the budget
        self.abstains = 0
        self.errors = 0  # no answer, an http error or a timeout
        self.latencies: List[float] = []

    def add(self, verdict: str, latency_ms: float) -> None:
        self.sent += 1
        if verdict == "error":
            self.errors += 1
            return
        self.latencies.append(latency_ms)
        if latency_ms <= BUDGET_MS:
            self.in_budget += 1
            if verdict == "abstain":
                self.abstains += 1
            else:
                self.votes += 1

    def counts(self) -> Dict:
        p95 = statistics.quantiles(self.latencies, n=20)[-1] if len(self.latencies) >= 2 else None
        return {"sent": self.sent, "inBudget": self.in_budget, "votes": self.votes,
                "abstains": self.abstains, "errors": self.errors, "p95Ms": None if p95 is None else round(p95, 1)}


# one second for every detector with the totals and each detector on its own
def second_line(t: int, by_url: Dict[str, Second]) -> Dict:
    total = Second()
    for s in by_url.values():
        total.sent += s.sent
        total.in_budget += s.in_budget
        total.votes += s.votes
        total.abstains += s.abstains
        total.errors += s.errors
        total.latencies += s.latencies
    return {"t": t, **total.counts(), "byDetector": {url: s.counts() for url, s in by_url.items()}}


# keeps a normal trip in redis and calls each detector rate times a second
class Probe:
    def __init__(self, redis_client, detector_urls: Sequence[str], rate: float,
                 post: Callable[[str, dict, float], dict] = post_json, clock=time.monotonic):
        self.redis = redis_client
        self.urls = list(detector_urls)
        self.rate = rate
        self.post = post
        self.clock = clock
        rows = [strip_labels(c) for c in generate("normal")[:TRIP_CHECKINS]]
        self.rows = rows
        self.body = {"requestId": f"{rows[-1]['tripId']}#probe", "riskLevel": "normal", "checkin": rows[-1]}
        self.route = build_route()
        self._lock = threading.Lock()
        self._seconds: Dict[int, Dict[str, Second]] = {}

    # write the trip again since a redis restart loses it, failures are expected during an outage
    def seed(self) -> None:
        try:
            seed_trip(self.redis, self.rows[0]["tripId"], self.route, self.rows[0], ttl_s=KEY_TTL_S)
            for c in self.rows:
                record_checkin(self.redis, c, ttl_s=KEY_TTL_S)
        except Exception:  # noqa: BLE001 - redis may be down, the probe keeps measuring
            pass

    def _call(self, url: str, second: int, start: float) -> None:
        try:
            verdict = self.post(url, self.body, TIMEOUT_S).get("verdict", "error")
        except Exception:  # noqa: BLE001 - refused, timed out or an http error all count as no answer
            verdict = "error"
        latency = (self.clock() - start) * 1000
        with self._lock:
            self._seconds.setdefault(second, {u: Second() for u in self.urls})[url].add(verdict, latency)

    # seed every second on its own thread so a hanging redis never slows the calls
    def _keep_seeding(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.seed()
            stop.wait(1.0)

    # run for duration seconds and hand each finished second to on_second
    def run(self, duration_s: float, on_second: Callable[[Dict], None]) -> None:
        self.seed()
        stop = threading.Event()
        threading.Thread(target=self._keep_seeding, args=(stop,), daemon=True).start()
        start = self.clock()
        gap = 1.0 / self.rate
        next_call, reported = start, 0
        with ThreadPoolExecutor(max_workers=64) as pool:
            while (now := self.clock()) < start + duration_s:
                if now >= next_call:
                    second = int(now - start)
                    for url in self.urls:
                        pool.submit(self._call, url, second, self.clock())
                    next_call += gap
                # report a second once its calls have had time to finish
                while reported < int(now - start) - TIMEOUT_S:
                    with self._lock:
                        done = self._seconds.pop(reported, {u: Second() for u in self.urls})
                    on_second(second_line(reported, done))
                    reported += 1
                time.sleep(max(0.0, next_call - self.clock()))
        stop.set()
        for t in sorted(self._seconds):
            on_second(second_line(t, self._seconds[t]))


# print one json line per second so a log can be read back later
def print_line(line: Dict) -> None:
    print(json.dumps(line), flush=True)
