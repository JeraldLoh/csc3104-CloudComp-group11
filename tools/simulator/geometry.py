# flat geometry for the simulator kept apart from the detectors so it tests them from outside

import math
from typing import Tuple

Point = Tuple[float, float]

M_PER_DEG = math.radians(1) * 6_371_000.0


# east and north metres from a to b
def delta_m(a: Point, b: Point) -> Tuple[float, float]:
    cos_lat = math.cos(math.radians(a[0]))
    return (b[1] - a[1]) * M_PER_DEG * cos_lat, (b[0] - a[0]) * M_PER_DEG


# metres from a to b
def distance_m(a: Point, b: Point) -> float:
    return math.hypot(*delta_m(a, b))


# bearing from a to b with 0 as north clockwise
def bearing_deg(a: Point, b: Point) -> float:
    east, north = delta_m(a, b)
    return math.degrees(math.atan2(east, north)) % 360.0


# point moved east_m and north_m from p
def offset(p: Point, east_m: float, north_m: float) -> Point:
    return (
        p[0] + north_m / M_PER_DEG,
        p[1] + east_m / (M_PER_DEG * math.cos(math.radians(p[0])))
    )
