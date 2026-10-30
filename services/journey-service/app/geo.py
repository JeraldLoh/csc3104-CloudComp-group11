"""Distances between nearby points (shared by replay.py and streetview.py)."""
import math


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Metres between two (lat, lng) points (flat-earth approximation: fine over a few kilometres)."""
    lat = math.radians((a[0] + b[0]) / 2)
    dy = (a[0] - b[0]) * 111_320
    dx = (a[1] - b[1]) * 111_320 * math.cos(lat)
    return math.hypot(dx, dy)
