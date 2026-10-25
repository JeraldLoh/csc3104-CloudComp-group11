"""Small local-plane geometry for the simulator (points are ``(lat, lng)`` degrees).

Equirectangular maths is plenty at Singapore scale. The simulator is kept
independent of the detectors package so it only ever tests them from outside.
"""

import math
from typing import Tuple

Point = Tuple[float, float]

M_PER_DEG = math.radians(1) * 6_371_000.0


def delta_m(a: Point, b: Point) -> Tuple[float, float]:
    """(east, north) metres from ``a`` to ``b``."""
    cos_lat = math.cos(math.radians(a[0]))
    return (b[1] - a[1]) * M_PER_DEG * cos_lat, (b[0] - a[0]) * M_PER_DEG


def distance_m(a: Point, b: Point) -> float:
    return math.hypot(*delta_m(a, b))


def bearing_deg(a: Point, b: Point) -> float:
    """0 = north, clockwise, in [0, 360)."""
    east, north = delta_m(a, b)
    return math.degrees(math.atan2(east, north)) % 360.0


def offset(p: Point, east_m: float, north_m: float) -> Point:
    return (
        p[0] + north_m / M_PER_DEG,
        p[1] + east_m / (M_PER_DEG * math.cos(math.radians(p[0]))),
    )
