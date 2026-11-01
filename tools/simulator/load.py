# load test with many travellers at once and each detector gets travellers over interval requests per second

import heapq
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from tools.simulator.replay import Post, ask, post_json, record_checkin, seed_trip
from tools.simulator.route import build_route
from tools.simulator.scenarios import SCENARIOS, generate, strip_labels

# load keys start with trip LOAD and expire so redis does not fill up
KEY_TTL_S = 3600
# enough threads that a slow detector does not hold up the schedule
MAX_WORKERS = 512


# nearest rank percentile or None for no values
def percentile(values: Sequence[float], p: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(p / 100 * len(ordered))))
    return ordered[rank - 1]


# counts for one reporting window or the whole run
@dataclass
class Window:
    sent: int = 0
    verdicts: Dict[str, int] = field(default_factory=dict)
    latencies_ms: List[float] = field(default_factory=list)

    # one detector answer
    def add(self, verdict: str, latency_ms: float) -> None:
        self.sent += 1
        self.verdicts[verdict] = self.verdicts.get(verdict, 0) + 1
        self.latencies_ms.append(latency_ms)

    # fold another window into this one
    def merge(self, other: "Window") -> None:
        self.sent += other.sent
        for k, v in other.verdicts.items():
            self.verdicts[k] = self.verdicts.get(k, 0) + v
        self.latencies_ms.extend(other.latencies_ms)


# workers add results and the main loop takes a window at a time
class Stats:
    # empty window and per detector totals
    def __init__(self, detector_urls: Sequence[str]):
        self._lock = threading.Lock()
        self._window = Window()
        self.total = Window()
        self.per_detector = {u: Window() for u in detector_urls}

    # record one detector answer
    def add(self, url: str, verdict: str, latency_ms: float) -> None:
        with self._lock:
            self._window.add(verdict, latency_ms)
            self.per_detector[url].add(verdict, latency_ms)

    # current window and start a new one
    def take_window(self) -> Window:
        with self._lock:
            w, self._window = self._window, Window()
            self.total.merge(w)
            return w


# one simulated traveller walking through generated trips
class Traveller:
    # traveller with its own scenario and seed
    def __init__(self, index: int, run_id: str, scenario: str, seed: int, browser: bool):
        self.index = index
        self.traveller_id = f"U-LOAD-{index:04d}"
        self._run_id = run_id
        self._scenario = scenario
        self._seed = seed
        self._browser = browser
        self._lap = 0
        self._checkins: List[dict] = []
        self._pos = 0

    # trip id for the current lap
    @property
    def trip_id(self) -> str:
        return f"LOAD-{self._run_id}-{self.index:04d}-{self._lap}"

    # next check-in and whether it starts a new trip
    def next_checkin(self) -> Tuple[dict, bool]:
        new_trip = self._pos >= len(self._checkins)
        if new_trip:
            self._lap += 1
            # new seed per lap so repeated trips differ
            self._checkins = generate(self._scenario, self._seed + 1000 * self._lap, browser=self._browser)
            self._pos = 0
        c = strip_labels(self._checkins[self._pos])
        self._pos += 1
        c["tripId"] = self.trip_id
        c["travellerId"] = self.traveller_id
        return c, new_trip


# ask one detector and record the verdict and latency
def _ask(post: Post, url: str, body: dict, stats: Stats) -> None:
    start = time.perf_counter()
    verdict = ask(post, url, body)["verdict"]
    stats.add(url, verdict, (time.perf_counter() - start) * 1000)


# drive the travellers for duration_s seconds and return the stats
def run_load(
    redis_client,
    detector_urls: Sequence[str],
    travellers: int,
    interval_s: float,
    duration_s: float,
    scenarios: Sequence[str] = SCENARIOS,
    seed: int = 42,
    browser: bool = True,
    post: Post = post_json,
    report_every_s: float = 10,
    on_report: Optional[Callable[[float, Window], None]] = None,
    run_id: Optional[str] = None
) -> Stats:
    if travellers < 1 or interval_s <= 0 or duration_s <= 0:
        raise ValueError("travellers must be >= 1, interval and duration > 0")
    if not detector_urls:
        raise ValueError("need at least one detector URL")
    for s in scenarios:
        if s not in SCENARIOS:
            raise ValueError(f"unknown scenario {s!r}, expected one of {', '.join(SCENARIOS)}")
    run_id = run_id or datetime.now(timezone.utc).strftime("%H%M%S")
    route = build_route()
    people = [Traveller(i, run_id, scenarios[i % len(scenarios)], seed + i, browser) for i in range(travellers)]
    stats = Stats(detector_urls)

    start = time.monotonic()
    end = start + duration_s
    next_report = start + report_every_s
    # due time and traveller index, starts spread over the first interval so load is smooth
    queue = [(start + interval_s * i / travellers, i) for i in range(travellers)]
    heapq.heapify(queue)

    workers = min(MAX_WORKERS, max(4, travellers * len(detector_urls)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while queue:
            due, i = queue[0]
            now = time.monotonic()
            if now >= next_report:
                w = stats.take_window()
                if on_report:
                    on_report(now - start, w)
                next_report += report_every_s
                continue
            if due >= end:
                break
            if due > now:
                time.sleep(min(due, next_report) - now)
                continue
            heapq.heappop(queue)
            checkin, new_trip = people[i].next_checkin()
            if new_trip:
                seed_trip(redis_client, checkin["tripId"], route, checkin, ttl_s=KEY_TTL_S)
            record_checkin(redis_client, checkin, ttl_s=KEY_TTL_S)
            body = {"requestId": f"{checkin['tripId']}#{checkin['seq']}", "riskLevel": "normal", "checkin": checkin}
            for url in detector_urls:
                pool.submit(_ask, post, url, body, stats)
            heapq.heappush(queue, (due + interval_s, i))
    # requests still in flight finished when the pool closed
    last = stats.take_window()
    if on_report and last.sent:
        on_report(time.monotonic() - start, last)
    return stats


# one progress line for a window
def format_window(elapsed_s: float, w: Window, window_s: float) -> str:
    p50, p95 = percentile(w.latencies_ms, 50), percentile(w.latencies_ms, 95)
    verdicts = " ".join(f"{k}={v}" for k, v in sorted(w.verdicts.items())) or "-"
    lat = f"p50 {p50:6.1f} ms  p95 {p95:6.1f} ms" if p50 is not None else "no answers"
    return f"{elapsed_s:6.0f}s  {w.sent / window_s:7.1f} req/s  {lat}  {verdicts}"


# per detector table for the whole run
def format_summary(stats: Stats, duration_s: float) -> str:
    lines = [f"{'detector':<40} {'requests':>8} {'req/s':>7} {'p50 ms':>7} {'p95 ms':>7} {'errors':>7}"]
    for url, w in stats.per_detector.items():
        p50, p95 = percentile(w.latencies_ms, 50), percentile(w.latencies_ms, 95)
        lines.append(
            f"{url:<40} {w.sent:>8} {w.sent / duration_s:>7.1f} "
            f"{(p50 or 0):>7.1f} {(p95 or 0):>7.1f} {w.verdicts.get('error', 0):>7}"
        )
    over = sum(1 for x in stats.total.latencies_ms if x > 500)
    lines.append(f"total {stats.total.sent} requests, {over} over the Coordinator's 500 ms budget")
    return "\n".join(lines)


# progress rows as a csv in out_dir
def write_csv(rows: List[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"load-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    cols = ["elapsedS", "reqPerS", "p50Ms", "p95Ms", "normal", "problem", "abstain", "error"]
    with path.open("w") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join("" if r.get(c) is None else str(r[c]) for c in cols) + "\n")
    return path
