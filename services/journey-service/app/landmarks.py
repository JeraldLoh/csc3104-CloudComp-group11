"""Landmarks along a route: named buildings the traveller will see, so they can tell they are going the right way.

    spots(route)                   -> {stepIndex: {spot: Spot}}, where along each step to look
    offer(route, spots, nearby)    -> {stepIndex: {spot: [landmark]}}, what is at each spot, each with an id ("L3")

A spot is a point on the route the AI may ask about (easy_read_ai.py sends them, the Easy-read Service lets the
model look them up with a tool and checks every landmark it names against what it was given):
  walk  "start" (where the walk begins), "middle" (walks over MIDDLE_FROM_M), "end" (where it ends)
  bus   "before_stop" (BEFORE_STOP_M before getting off, out of the window: "after you see X, press the bell"),
        "end" (the stop to get off at)
Train steps and changes of line get none: underground, or inside a station.

`nearby(lat, lng)` gives the named buildings around a point (OneMap's reverse geocode, journey_client.py); a
landmark is kept only within NEAR_PATH_M of the step's own path, and is told with where it is: how far, and on
which side for the direction of travel, worked out here, not guessed by the AI.

Privacy (contracts/api.md): nothing is looked up within PRIVATE_M of a place without a stop code (home, an
address someone typed), so the names sent can't give away where the traveller lives. The AI only ever gets
names, sides and distances, never coordinates.
"""
import math
import re
from typing import Callable, NamedTuple

from .geo import distance_m, distance_to_path_m
from .easy_read import changes_line

MIDDLE_FROM_M = 250                 # a walk this long also gets a spot halfway
BEFORE_STOP_M = 300                 # a bus ride's spot this far before the stop to get off at
NEAR_PATH_M = 100                   # a building further from the step's path than this isn't on the way
PRIVATE_M = 200                     # no spots this close to a home or a typed address
AT_SPOT_M = 25                      # this close: right there, no side
PER_SPOT = 5                        # landmarks offered per spot, the most obvious first

# Kinds of building a traveller can't miss, from words in the name; anything else is "building".
KINDS = [                                   # first match wins: "Park Place Residences" is a condominium
    (r"botanic gardens|gardens by the bay", "park"),
    (r"residences?\b|condo|\bvilla|suites|apartments?\b|\bgardens?\b", "condominium"),    # "Lew Lian Gardens"
    (r"car ?park|multi[- ]storey", "building"),                                             # not a park
    (r"\bmrt\b|\blrt\b", "station"), (r"\bmall\b|\bplaza\b|\bsquare\b|\bpoint\b|\bcity\b|shopping", "mall"),
    (r"hawker|food centre|market", "hawker centre"), (r"\binterchange\b|transport hub|bus hub", "bus interchange"),
    (r"school|college|institute|polytechnic|university", "school"), (r"hospital|polyclinic|clinic", "hospital"),
    (r"library", "library"), (r"community (club|centre)", "community centre"), (r"\bpark\b", "park"),
    (r"church|temple|mosque|masjid", "place of worship"), (r"stadium|sports|swimming", "sports centre"),
]
LESS_OBVIOUS = {"building"}                 # offered after the kinds above
HIDDEN = {"condominium"}                    # not offered: a name on a gate, and people's homes


class Spot(NamedTuple):
    lat: float
    lng: float
    heading: tuple[tuple[float, float], tuple[float, float]] | None    # (from, to) of the path here, for the side
    path: list[tuple[float, float]]                                    # the step's own path


def kind(name: str) -> str:
    low = name.lower()
    return next((k for pattern, k in KINDS if re.search(pattern, low)), "building")


def step_paths(route: dict) -> dict[int, list[tuple[float, float]]]:
    """Each step's stretch of the route's joined `path`: from the point nearest its start to the one nearest its
    end, searched forwards so a route that passes the same place twice is cut in order."""
    path = [tuple(p) for p in route.get("path") or []]
    out, cursor = {}, 0
    for step in route.get("steps") or []:
        a, b = step.get("from") or {}, step.get("to") or {}
        if a.get("lat") is None or b.get("lat") is None:
            continue
        ends = (a["lat"], a["lng"]), (b["lat"], b["lng"])
        if len(path) - cursor < 2:
            out[step["stepIndex"]] = list(ends)
            continue
        i = min(range(cursor, len(path)), key=lambda k: distance_m(path[k], ends[0]))
        j = min(range(i, len(path)), key=lambda k: distance_m(path[k], ends[1]))
        out[step["stepIndex"]] = path[i:j + 1] if j > i else list(ends)
        cursor = j
    return out


def _length(path: list[tuple[float, float]]) -> float:
    return sum(distance_m(a, b) for a, b in zip(path, path[1:]))


def _at(path: list[tuple[float, float]], metres: float) -> Spot:
    """The point `metres` along the path, with the direction of travel there."""
    gone = 0.0
    for a, b in zip(path, path[1:]):
        d = distance_m(a, b)
        if d and gone + d >= metres:
            t = (metres - gone) / d
            return Spot(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), (a, b), path)
        gone += d
    end = path[-1]
    return Spot(end[0], end[1], (path[-2], end) if len(path) > 1 else None, path)


def _private(route: dict) -> list[tuple[float, float]]:
    """Places without a stop code: the route's own ends and any step end like them."""
    points = [route.get("origin") or {}, route.get("destination") or {}]
    for step in route.get("steps") or []:
        points += [p for p in (step.get("from"), step.get("to")) if p and not p.get("code")]
    return [(p["lat"], p["lng"]) for p in points if p.get("lat") is not None and p.get("lng") is not None]


def spots(route: dict) -> dict[int, dict[str, Spot]]:
    paths, private = step_paths(route), _private(route)
    out: dict[int, dict[str, Spot]] = {}
    for step in route.get("steps") or []:
        path, mode = paths.get(step["stepIndex"]), step.get("mode")
        if not path or len(path) < 2 or mode == "train" or changes_line(step):
            continue
        length = _length(path)
        if mode == "walk":
            wanted = {"start": 0.0, "end": length}
            if length > MIDDLE_FROM_M:
                wanted["middle"] = length / 2
        elif mode == "bus":
            wanted = {"end": length}
            if length > 2 * BEFORE_STOP_M:
                wanted["before_stop"] = length - BEFORE_STOP_M
        else:
            continue
        found = {name: _at(path, m) for name, m in wanted.items()}
        found = {name: s for name, s in found.items()
                 if all(distance_m((s.lat, s.lng), p) > PRIVATE_M for p in private)}
        if found:
            out[step["stepIndex"]] = found
    return out


def side(heading: tuple[tuple[float, float], tuple[float, float]] | None, point: tuple[float, float]) -> str | None:
    """"left" or "right" of the direction of travel, or None if there's no direction."""
    if heading is None:
        return None
    (alat, alng), (blat, blng) = heading
    k = math.cos(math.radians(alat))
    dx, dy = (blng - alng) * k, blat - alat
    px, py = (point[1] - alng) * k, point[0] - alat
    cross = dx * py - dy * px
    return None if cross == 0 else ("left" if cross > 0 else "right")


def _same_place(name: str, step: dict) -> bool:
    """A landmark that is the stop or station the step already names adds nothing."""
    low = name.lower()
    for p in (step.get("from") or {}, step.get("to") or {}):
        other = (p.get("name") or "").lower()
        if other and (low in other or other in low):
            return True
    return False


def offer(route: dict, found_spots: dict[int, dict[str, Spot]],
          nearby: Callable[[float, float], list[dict]]) -> dict[int, dict[str, list[dict]]]:
    """{stepIndex: {spot: [{id, name, kind, metres, side}]}}. `nearby` gives [{name, lat, lng}] around a point.
    Ids are numbered across the whole route, so a check can tell which step a landmark was offered for."""
    steps = {s["stepIndex"]: s for s in route.get("steps") or []}
    out: dict[int, dict[str, list[dict]]] = {}
    n = 0
    for index, by_spot in found_spots.items():
        for name, spot in by_spot.items():
            here = (spot.lat, spot.lng)
            best: dict[str, dict] = {}
            for b in nearby(*here):
                where = (b["lat"], b["lng"])
                if (kind(b["name"]) in HIDDEN or _same_place(b["name"], steps[index])
                        or distance_to_path_m(*where, spot.path) > NEAR_PATH_M):
                    continue
                metres = distance_m(here, where)
                if b["name"] not in best or metres < best[b["name"]]["metres"]:
                    best[b["name"]] = {"name": b["name"], "kind": kind(b["name"]), "metres": metres,
                                       "side": None if metres <= AT_SPOT_M else side(spot.heading, where),
                                       "lat": b["lat"], "lng": b["lng"],
                                       # Where the traveller sees it from: its photo is taken from here.
                                       "seenFrom": [round(spot.lat, 6), round(spot.lng, 6)]}
            picked = sorted(best.values(), key=lambda b: (b["kind"] in LESS_OBVIOUS, b["metres"]))[:PER_SPOT]
            for b in picked:
                n += 1
                b["id"] = f"L{n}"
                b["metres"] = int(round(b["metres"] / 10) * 10)
            if picked:
                out.setdefault(index, {})[name] = picked
    return out
