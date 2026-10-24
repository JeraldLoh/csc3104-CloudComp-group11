"""Synthetic trips: ``generate(scenario, seed)`` returns check-ins in the ``checkin.json`` shape.

``browser=True`` degrades them the way a web page on a phone would (gaps, jittery
timestamps, missing speed and heading); see :func:`_browserify`.

Each check-in also carries two fixture-only fields, ``scenario`` and ``expected``
(``normal``, a problem type, or ``abstain``). Strip them with :func:`strip_labels`
before sending a check-in anywhere.

A trip is planned as a list of ticks (distance along the route, sideways offset,
label), 10 s apart. The check-ins are then made from the planned positions plus
GPS noise, so speed and heading follow the real movement rather than the noise.
"""

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
    build_track,
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
    "poor_gps",
)

# A drift only counts as off_route once it is beyond the default corridor
OFF_ROUTE_LABEL_M = 100
# A stop only counts as long_dwell beyond the default walking limit
LONG_DWELL_LABEL_S = 300
FIXTURE_ONLY_FIELDS = ("scenario", "expected")


@dataclass
class _Tick:
    s: float  # metres along the route
    lateral_m: float = 0.0  # sideways from the route; + is left of travel
    label: str = "normal"


class _Planner:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.ticks: List[_Tick] = [_Tick(0.0)]

    @property
    def s(self) -> float:
        return self.ticks[-1].s

    @property
    def lateral(self) -> float:
        return self.ticks[-1].lateral_m

    def _jitter(self) -> float:
        return self.rng.uniform(0.85, 1.15)

    def move_to(self, target: float, speed: float, label: str = "normal") -> None:
        while self.s != target:
            step = speed * INTERVAL_S * self._jitter()
            remaining = target - self.s
            s = target if abs(remaining) <= step else self.s + math.copysign(step, remaining)
            self.ticks.append(_Tick(s, self.lateral, label))

    def hold(self, seconds: int, late_label: str = "normal", late_after_s: int = 0) -> None:
        for k in range(1, seconds // INTERVAL_S + 1):
            label = late_label if k * INTERVAL_S > late_after_s else "normal"
            self.ticks.append(_Tick(self.s, self.lateral, label))

    def drift(self, distance: float, side: int) -> None:
        """Turn away from the route and walk off sideways, then stand still there."""
        lateral = 0.0
        while abs(lateral) < distance:
            lateral = side * min(distance, abs(lateral) + WALK_SPEED_MPS * INTERVAL_S * self._jitter())
            label = "off_route" if abs(lateral) > OFF_ROUTE_LABEL_M else "normal"
            self.ticks.append(_Tick(self.s, lateral, label))
        for _ in range(12):
            self.ticks.append(_Tick(self.s, lateral, "off_route"))


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


def _true_position(track: Track, tick: _Tick):
    pos = track.position(tick.s)
    if tick.lateral_m:
        left = math.radians(track.bearing(tick.s) - 90)
        pos = offset(pos, tick.lateral_m * math.sin(left), tick.lateral_m * math.cos(left))
    return pos


def _timestamp(i: int) -> str:
    return (TRIP_START + timedelta(seconds=i * INTERVAL_S)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _browserify(checkins: List[Dict], seed: int) -> List[Dict]:
    """Make clean check-ins look like they came from a web page on a phone.

    Mobile browsers throttle background tabs and often report no speed or heading,
    so: ~20% of check-ins go missing (gaps of 20 s or more), timestamps wobble by
    up to 2 s, about half the speeds are null, and heading is null when standing
    still or ~30% of the time otherwise. ``seq`` is renumbered to stay contiguous
    (it counts what the page sent). Uses its own RNG so the clean data is untouched.
    """
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
        # Movement since the previous tick (the first tick looks ahead instead)
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
                "expected": "abstain" if poor_gps else tick.label,
            }
        )
    return _browserify(checkins, seed) if browser else checkins


def strip_labels(checkin: Dict) -> Dict:
    """The check-in as the real app would send it, without the fixture-only labels."""
    return {k: v for k, v in checkin.items() if k not in FIXTURE_ONLY_FIELDS}
