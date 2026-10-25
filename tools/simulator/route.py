"""The simulated trip's route: Home -> bus stop (walk) -> bus -> Clinic (walk).

``build_route()`` returns a dict in the ``contracts/examples/route.json`` shape,
with a dense ``path`` (a point at most every 20 m). ``Track`` walks that path by
distance along it, which is how the scenarios place the traveller.
"""

import bisect
import math
from typing import Dict, List, Tuple

from tools.simulator.geometry import Point, bearing_deg, distance_m

WALK_SPEED_MPS = 1.3
BUS_SPEED_MPS = 8.0
SPACING_M = 20.0

# Waypoints along the route (Punggol), in travel order. The scenarios rely on
# the indexes below: walk to the stop, bus to the next stop, walk to the clinic.
HOME, CORNER, BUS_STOP, BUS_BEND, ALIGHT, CLINIC_CORNER, CLINIC = range(7)
WAYPOINTS: List[Point] = [
    (1.4025, 103.8995),  # Home
    (1.4025, 103.9024),  # corner: east along the road, then north
    (1.4052, 103.9024),  # Punggol Int, bus stop 65009
    (1.3990, 103.8990),  # bus bend
    (1.3935, 103.8962),  # Opp Block 221, stop 65221
    (1.3935, 103.8950),  # corner
    (1.3920, 103.8950),  # Clinic
]


class Track:
    """A dense polyline with cumulative distance, so positions are looked up by ``s`` (metres)."""

    def __init__(self, waypoints: List[Point], spacing_m: float = SPACING_M):
        self.path: List[Point] = [waypoints[0]]
        waypoint_idx = [0]
        for a, b in zip(waypoints, waypoints[1:]):
            n = max(1, math.ceil(distance_m(a, b) / spacing_m))
            for k in range(1, n + 1):
                self.path.append((a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n))
            waypoint_idx.append(len(self.path) - 1)
        self.cum = [0.0]
        for p, q in zip(self.path, self.path[1:]):
            self.cum.append(self.cum[-1] + distance_m(p, q))
        # Distance along the path at each waypoint
        self.waypoint_s = [self.cum[i] for i in waypoint_idx]
        self.length = self.cum[-1]

    def _segment(self, s: float) -> int:
        i = bisect.bisect_right(self.cum, s) - 1
        return min(max(i, 0), len(self.path) - 2)

    def position(self, s: float) -> Point:
        s = min(max(s, 0.0), self.length)
        i = self._segment(s)
        t = (s - self.cum[i]) / (self.cum[i + 1] - self.cum[i])
        a, b = self.path[i], self.path[i + 1]
        return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t

    def bearing(self, s: float) -> float:
        i = self._segment(min(max(s, 0.0), self.length))
        return bearing_deg(self.path[i], self.path[i + 1])

    def step_index(self, s: float) -> int:
        """Step the traveller is on at ``s``. Waiting at the bus stop counts as the bus step."""
        if s < self.waypoint_s[BUS_STOP]:
            return 0
        if s < self.waypoint_s[ALIGHT]:
            return 1
        return 2


def build_track() -> Track:
    return Track(WAYPOINTS)


def _place(name: str, code, point: Point) -> Dict:
    return {"name": name, "code": code, "lat": point[0], "lng": point[1]}


def _round10(value: float) -> int:
    return int(round(value / 10.0) * 10)


def build_route() -> Dict:
    track = build_track()
    ws = track.waypoint_s
    walk1 = ws[BUS_STOP] - ws[HOME]
    ride = ws[ALIGHT] - ws[BUS_STOP]
    walk2 = ws[CLINIC] - ws[ALIGHT]
    home = _place("Home", None, WAYPOINTS[HOME])
    stop = _place("Punggol Int", "65009", WAYPOINTS[BUS_STOP])
    alight = _place("Opp Block 221", "65221", WAYPOINTS[ALIGHT])
    clinic = _place("Clinic", None, WAYPOINTS[CLINIC])

    def step(index, mode, instruction, easy_read, src, dst, line, duration_s, backup=None):
        return {
            "stepIndex": index,
            "mode": mode,
            "instruction": instruction,
            "easyRead": easy_read,
            "from": src,
            "to": dst,
            "line": line,
            "expectedDurationS": duration_s,
            "underground": False,
            "photoKey": f"media/R-007/step{index}.jpg",
            "audioKey": f"media/R-007/step{index}.mp3",
            "backup": backup,
        }

    return {
        "routeId": "R-007",
        "travellerId": "U-042",
        "caregiverId": "G-003",
        "name": "Home to Clinic",
        "version": 3,
        "approved": True,
        "origin": {**{k: home[k] for k in ("name", "lat", "lng")}, "safeLocationId": "L-001"},
        "destination": {**{k: clinic[k] for k in ("name", "lat", "lng")}, "safeLocationId": "L-004"},
        "steps": [
            step(0, "walk", f"Walk {_round10(walk1)} m to Bus Stop 65009",
                 "Walk to the bus stop near the playground.",
                 home, stop, None, _round10(walk1 / WALK_SPEED_MPS)),
            step(1, "bus", "Take bus 190 to Stop 65221 (6 stops)",
                 "Get on bus number 190. Tap your card.",
                 stop, alight, "190", _round10(ride / BUS_SPEED_MPS),
                 backup={"instruction": "If bus 190 does not come in 20 minutes, take bus 83 from the same stop.",
                         "line": "83"}),
            step(2, "walk", f"Walk {_round10(walk2)} m to the Clinic",
                 "Walk to the clinic.",
                 alight, clinic, None, _round10(walk2 / WALK_SPEED_MPS)),
        ],
        "path": [[round(lat, 6), round(lng, 6)] for lat, lng in track.path],
        "updatedAt": "2026-10-10T14:00:00Z",
    }
