# the simulated route home to bus stop by walk then bus then walk to the clinic

import bisect
import math
from typing import Dict, List

from tools.simulator.geometry import Point, bearing_deg, distance_m

WALK_SPEED_MPS = 1.3
BUS_SPEED_MPS = 8.0
SPACING_M = 20.0

# waypoints in travel order through punggol and scenarios rely on these indexes
HOME, CORNER, BUS_STOP, BUS_BEND, ALIGHT, CLINIC_CORNER, CLINIC = range(7)
WAYPOINTS: List[Point] = [
    (1.4025, 103.8995),  # home
    (1.4025, 103.9024),  # corner east along the road then north
    (1.4052, 103.9024),  # punggol interchange stop 65009
    (1.3990, 103.8990),  # bus bend
    (1.3935, 103.8962),  # opposite block 221 stop 65221
    (1.3935, 103.8950),  # corner
    (1.3920, 103.8950)  # clinic
]


# dense path with cumulative distance so positions are looked up by s in metres
class Track:
    # fill in a point every spacing_m between waypoints
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
        # distance along the path at each waypoint
        self.waypoint_s = [self.cum[i] for i in waypoint_idx]
        self.length = self.cum[-1]

    # index of the path segment holding s
    def _segment(self, s: float) -> int:
        i = bisect.bisect_right(self.cum, s) - 1
        return min(max(i, 0), len(self.path) - 2)

    # lat lng at s along the path
    def position(self, s: float) -> Point:
        s = min(max(s, 0.0), self.length)
        i = self._segment(s)
        t = (s - self.cum[i]) / (self.cum[i + 1] - self.cum[i])
        a, b = self.path[i], self.path[i + 1]
        return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t

    # path bearing at s
    def bearing(self, s: float) -> float:
        i = self._segment(min(max(s, 0.0), self.length))
        return bearing_deg(self.path[i], self.path[i + 1])

    # step at s and waiting at the bus stop counts as the bus step
    def step_index(self, s: float) -> int:
        if s < self.waypoint_s[BUS_STOP]:
            return 0
        if s < self.waypoint_s[ALIGHT]:
            return 1
        return 2


# track over the fixed waypoints
def build_track() -> Track:
    return Track(WAYPOINTS)


# place dict in the route json shape
def _place(name: str, code, point: Point) -> Dict:
    return {"name": name, "code": code, "lat": point[0], "lng": point[1]}


# nearest multiple of 10
def _round10(value: float) -> int:
    return int(round(value / 10.0) * 10)


# route json in the contracts route shape with a dense path
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

    # one route step dict
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
            "backup": backup
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
                 alight, clinic, None, _round10(walk2 / WALK_SPEED_MPS))
        ],
        "path": [[round(lat, 6), round(lng, 6)] for lat, lng in track.path],
        "updatedAt": "2026-10-10T14:00:00Z"
    }
