# synthetic trips as labelled check-ins planned 10 s apart with gps noise added after

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from tools.simulator.geometry import bearing_deg, distance_m, offset
from tools.simulator.route import (
    ALIGHT,
    BUS_SPEED_MPS,
    BUS_STOP,
    WALK_SPEED_MPS,
    Track,
    build_track
)

INTERVAL_S = 10
TRIP_START = datetime(2026, 10, 12, 8, 0, 0, tzinfo=timezone.utc)
TRAVELLER_ID = "U-042"
ROUTE_VERSION = 3

SCENARIOS = (
    "normal",
    "off_route",
    "wrong_direction",
    "long_dwell",
    "normal_bus_wait",
    "poor_gps"
)

# a drift only counts as off_route beyond the default corridor
OFF_ROUTE_LABEL_M = 100
# a stop only counts as long_dwell beyond the default walking limit
LONG_DWELL_LABEL_S = 300
FIXTURE_ONLY_FIELDS = ("scenario", "expected")


# one planned position
@dataclass
class _Tick:
    s: float  # metres along the route
    lateral_m: float = 0.0  # sideways from the route and positive is left of travel
    label: str = "normal"


# builds the list of ticks for a scenario
class _Planner:
    # start at the beginning of the route
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.ticks: List[_Tick] = [_Tick(0.0)]

    # metres along the route at the last tick
    @property
    def s(self) -> float:
        return self.ticks[-1].s

    # sideways offset at the last tick
    @property
    def lateral(self) -> float:
        return self.ticks[-1].lateral_m

    # random pace factor so ticks are not perfectly even
    def _jitter(self) -> float:
        return self.rng.uniform(0.85, 1.15)

    # walk or ride to target metres along the route
    def move_to(self, target: float, speed: float, label: str = "normal") -> None:
        while self.s != target:
            step = speed * INTERVAL_S * self._jitter()
            remaining = target - self.s
            s = target if abs(remaining) <= step else self.s + math.copysign(step, remaining)
            self.ticks.append(_Tick(s, self.lateral, label))

    # stand still and switch label after late_after_s
    def hold(self, seconds: int, late_label: str = "normal", late_after_s: int = 0) -> None:
        for k in range(1, seconds // INTERVAL_S + 1):
            label = late_label if k * INTERVAL_S > late_after_s else "normal"
            self.ticks.append(_Tick(self.s, self.lateral, label))

    # turn off the route and walk sideways then stand still
    def drift(self, distance: float, side: int) -> None:
        lateral = 0.0
        while abs(lateral) < distance:
            lateral = side * min(distance, abs(lateral) + WALK_SPEED_MPS * INTERVAL_S * self._jitter())
            label = "off_route" if abs(lateral) > OFF_ROUTE_LABEL_M else "normal"
            self.ticks.append(_Tick(self.s, lateral, label))
        for _ in range(12):
            self.ticks.append(_Tick(self.s, lateral, "off_route"))


# ticks for one scenario
def _plan(scenario: str, track: Track, rng: random.Random) -> List[_Tick]:
    p = _Planner(rng)
    stop = track.waypoint_s[BUS_STOP]
    alight = track.waypoint_s[ALIGHT]
    if scenario == "off_route":
        p.move_to(0.35 * stop, WALK_SPEED_MPS)
        p.drift(rng.uniform(150, 250), rng.choice((-1, 1)))
    elif scenario == "wrong_direction":
        p.move_to(0.4 * stop, WALK_SPEED_MPS)
        p.move_to(0.0, WALK_SPEED_MPS, label="wrong_direction")
    else:
        if scenario == "long_dwell":
            p.move_to(0.45 * stop, WALK_SPEED_MPS)
            p.hold(15 * 60, late_label="long_dwell", late_after_s=LONG_DWELL_LABEL_S)
        p.move_to(stop, WALK_SPEED_MPS)
        p.hold(8 * 60 if scenario == "normal_bus_wait" else 60)
        p.move_to(alight, BUS_SPEED_MPS)
        p.move_to(track.length, WALK_SPEED_MPS)
    return p.ticks


# lat lng of a tick before gps noise
def _true_position(track: Track, tick: _Tick):
    pos = track.position(tick.s)
    if tick.lateral_m:
        left = math.radians(track.bearing(tick.s) - 90)
        pos = offset(pos, tick.lateral_m * math.sin(left), tick.lateral_m * math.cos(left))
    return pos


# iso time of tick i
def _timestamp(i: int) -> str:
    return (TRIP_START + timedelta(seconds=i * INTERVAL_S)).strftime("%Y-%m-%dT%H:%M:%SZ")


# make clean check-ins look like a phone web page with gaps jitter and missing speed and heading
def _browserify(checkins: List[Dict], seed: int) -> List[Dict]:
    rng = random.Random(f"{seed}-browser")
    last = len(checkins) - 1
    kept = [c for i, c in enumerate(checkins) if i in (0, last) or rng.random() >= 0.2]
    out = []
    for seq, c in enumerate(kept, start=1):
        c = dict(c)
        at = datetime.strptime(c["timestamp"], "%Y-%m-%dT%H:%M:%SZ") + timedelta(seconds=rng.randint(-2, 2))
        still = c["speedMps"] < 0.5
        c["seq"] = seq
        c["timestamp"] = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if rng.random() < 0.5:
            c["speedMps"] = None
        if still or rng.random() < 0.3:
            c["headingDeg"] = None
        out.append(c)
    return out


# labelled check-ins for a scenario
def generate(scenario: str, seed: int = 42, browser: bool = False) -> List[Dict]:
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}, expected one of {', '.join(SCENARIOS)}")
    rng = random.Random(seed)
    track = build_track()
    ticks = _plan(scenario, track, rng)
    truth = [_true_position(track, t) for t in ticks]
    trip_id = "T-SIM-" + scenario.upper().replace("_", "-")
    poor_gps = scenario == "poor_gps"

    checkins = []
    heading = bearing_deg(truth[0], truth[1])
    for i, (tick, pos) in enumerate(zip(ticks, truth)):
        # movement since the previous tick and the first tick looks ahead
        a, b = (truth[i - 1], pos) if i else (truth[0], truth[1])
        moved = distance_m(a, b)
        if moved >= 2:
            heading = bearing_deg(a, b)

        accuracy = round(rng.uniform(80, 120) if poor_gps else rng.uniform(5, 15), 1)
        lat, lng = offset(pos, rng.gauss(0, accuracy / 2), rng.gauss(0, accuracy / 2))
        checkins.append(
            {
                "tripId": trip_id,
                "travellerId": TRAVELLER_ID,
                "seq": i + 1,
                "timestamp": _timestamp(i),
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "accuracyM": accuracy,
                "speedMps": round(max(0.0, moved / INTERVAL_S + rng.gauss(0, 0.1)), 2),
                "headingDeg": round((heading + rng.gauss(0, 5)) % 360, 1) % 360,
                "stepIndex": track.step_index(tick.s),
                "routeVersion": ROUTE_VERSION,
                "batteryPct": max(5, 90 - i // 30),
                "status": "active",
                "bufferedOffline": False,
                "scenario": scenario,
                "expected": "abstain" if poor_gps else tick.label
            }
        )
    return _browserify(checkins, seed) if browser else checkins


# check-in as the real app sends it without the fixture labels
def strip_labels(checkin: Dict) -> Dict:
    return {k: v for k, v in checkin.items() if k not in FIXTURE_ONLY_FIELDS}
