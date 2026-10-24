"""Pure geometry helpers (no I/O).

Points are ``(lat, lng)`` pairs in degrees, the same order as ``path`` in
``contracts/examples/route.json``. Distances are in metres, bearings in degrees.
"""

import math
from typing import Sequence, Tuple

EARTH_RADIUS_M = 6_371_000.0

Point = Sequence[float]


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lng2 - lng1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Initial bearing from point 1 to point 2: 0 = north, clockwise, in [0, 360)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lng2 - lng1)
    y = math.sin(dlmb) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlmb)
    return math.degrees(math.atan2(y, x)) % 360.0


def angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two angles, in [0, 180]."""
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d


def point_to_segment_m(p: Point, a: Point, b: Point) -> float:
    """Distance in metres from ``p`` to the segment ``a``-``b``.

    Uses a local equirectangular projection centred on ``p``; the error is
    negligible at Singapore scale (segments of a few hundred metres).
    """
    cos_lat = math.cos(math.radians(p[0]))
    m_per_deg = math.radians(1) * EARTH_RADIUS_M

    def project(q: Point) -> Tuple[float, float]:
        return (q[1] - p[1]) * m_per_deg * cos_lat, (q[0] - p[0]) * m_per_deg

    ax, ay = project(a)
    bx, by = project(b)
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq == 0:
        return math.hypot(ax, ay)
    # p is the origin; t is its projection onto the segment, clamped to [0, 1]
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / seg_len_sq))
    return math.hypot(ax + t * dx, ay + t * dy)


def nearest_segment(point: Point, path: Sequence[Point]) -> Tuple[int, float, float]:
    """Find the path segment closest to ``point``.

    Returns ``(index, distance_m, segment_bearing_deg)`` where ``index`` is the
    start of the segment (segment ``i`` runs from ``path[i]`` to ``path[i + 1]``).
    Ties go to the earliest segment. Raises ``ValueError`` if the path has fewer
    than 2 points.
    """
    if len(path) < 2:
        raise ValueError("path needs at least 2 points")
    best_i, best_d = 0, math.inf
    for i in range(len(path) - 1):
        d = point_to_segment_m(point, path[i], path[i + 1])
        if d < best_d:
            best_i, best_d = i, d
    a, b = path[best_i], path[best_i + 1]
    return best_i, best_d, bearing_deg(a[0], a[1], b[0], b[1])
