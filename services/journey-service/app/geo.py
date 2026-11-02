"""Distances between nearby points and to a path (shared by journey_client.py, landmarks.py, replay.py and streetview.py)."""
import math

EARTH_RADIUS_M = 6_371_000.0


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Metres between two (lat, lng) points (flat-earth approximation: fine over a few kilometres)."""
    lat = math.radians((a[0] + b[0]) / 2)
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(lat)
    return math.hypot(dx, dy)


def path_between(path: list, a: tuple[float, float], b: tuple[float, float], near_m: float = 400) -> list[list[float]]:
    """The part of `path` from the point nearest `a` to the point nearest `b` after it (a step's piece of a route's
    joined path); just [a, b] when either end is more than `near_m` from the path."""
    if path:
        i = min(range(len(path)), key=lambda k: distance_m(a, path[k]))
        j = min(range(i, len(path)), key=lambda k: distance_m(b, path[k]))
        if j > i and distance_m(a, path[i]) <= near_m and distance_m(b, path[j]) <= near_m:
            return [list(p) for p in path[i:j + 1]]
    return [list(a), list(b)]


def _to_xy(lat: float, lng: float, ref_lat: float) -> tuple[float, float]:
    """Local flat-earth projection in metres; accurate enough over a few kilometres."""
    return (math.radians(lng) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat)),
            math.radians(lat) * EARTH_RADIUS_M)


def distance_to_path_m(lat: float, lng: float, path: list[tuple[float, float]]) -> float:
    """Shortest distance in metres from a point to a polyline (inf for an empty path)."""
    if not path:
        return math.inf
    px, py = _to_xy(lat, lng, lat)
    if len(path) == 1:
        ax, ay = _to_xy(*path[0], lat)
        return math.hypot(px - ax, py - ay)
    best = math.inf
    for (alat, alng), (blat, blng) in zip(path, path[1:]):
        ax, ay = _to_xy(alat, alng, lat)
        bx, by = _to_xy(blat, blng, lat)
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        best = min(best, math.hypot(px - (ax + t * dx), py - (ay + t * dy)))
    return best
