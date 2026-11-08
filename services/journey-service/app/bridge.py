"""Bridging buses during a train breakdown: replacement steps from LTA's own arrangements, no planning needed.

    affected_step(steps, current, segment) -> the index of the first train step the traveller hasn't done that
                                              rides through the broken stretch, or None
    bridge_for(step, segment, free_bus, point) -> {"kind", "leaveAt", "target", "steps"} to replace that step, or None
    splice(steps, start, end, new)         -> steps[start..end] replaced by `new`, renumbered

LTA's TrainServiceAlerts (lta.train_alerts()) gives, for each broken stretch, its stations and where the free buses
can be taken: `freeMrtShuttle` (the bridging buses, going `shuttleDirection`: "Both" or the station that way ends at)
and `freePublicBus` (ordinary buses, free at those stations). So for a ride through the stretch:
  - leave the train at the station before the stretch, if the bridging bus calls there, else at the first broken
    station (or right away, if the ride starts inside the stretch);
  - take the free bus to the station after the stretch, if it calls there, else the last broken station (or the
    traveller's own stop, if it is inside the stretch);
  - and carry on by train from there.
The bridging bus is used when it calls at both ends going the traveller's way; else an ordinary free bus between
them (`free_bus`, which looks the bus routes up); else there is no bridge, and the caregiver can plan a new route.
Pure: no I/O, so it is easy to test and the wording is easy_read's (its `bridge` step kind).
"""
from typing import Callable

from .easy_read import LINE_NAMES, _line_key, going_to

FreeBus = Callable[[dict, dict], dict | None]          # (leave station, target station) -> {serviceNo, board, alight}
Point = Callable[[dict], tuple[float, float] | None]   # a station's (lat, lng), when the stop has none


def _ride(step: dict) -> list[dict]:
    return step.get("stops") or [step.get("from") or {}, step.get("to") or {}]


def through(step: dict, broken: set[str]) -> tuple[dict, dict] | None:
    """Where a train step rides with no trains running: the last station of its ride before the broken stations and
    the first one after them (or the ride's ends). None if it doesn't ride into them, including a ride that only
    starts or ends at the edge of the stretch (the train part of a bridge)."""
    ride = _ride(step)
    hit = [i for i, s in enumerate(ride) if s.get("code") in broken]
    if step.get("mode") != "train" or not hit or (len(ride) > 1 and hit in ([0], [len(ride) - 1])):
        return None
    return ride[max(hit[0] - 1, 0)], ride[min(hit[-1] + 1, len(ride) - 1)]


def affected_step(steps: list[dict], current: int, segment: dict) -> int | None:
    broken = set(segment["stations"])
    for i, step in enumerate(steps):
        if step.get("mode") == "train" and step.get("stepIndex", i) >= current and \
                any(s.get("code") in broken for s in _ride(step)):
            return i
    return None


def _ends(ride: list[dict], broken: set[str]) -> tuple[list[int], list[int]]:
    """Where to leave the train and where to get back on, in order of preference (positions in `ride`)."""
    hit = [i for i, s in enumerate(ride) if s.get("code") in broken]
    first, last = hit[0], hit[-1]
    leave = [0] if first == 0 else [first - 1, first]
    target = [len(ride) - 1] if last == len(ride) - 1 else [last + 1, last]
    return leave, target


def _goes_their_way(segment: dict, step: dict) -> bool:
    way = (segment.get("shuttleDirection") or "").lower()
    if way in ("", "both"):
        return True
    going = going_to(step)
    return bool(going) and (going.lower() in way or way in going.lower())


def bridge_for(step: dict, segment: dict, free_bus: FreeBus | None = None, point: Point | None = None) -> dict | None:
    ride = _ride(step)
    broken = set(segment["stations"])
    if not any(s.get("code") in broken for s in ride):
        return None
    leave_at, target = _ends(ride, broken)
    pairs = [(a, b) for a in leave_at for b in target if a < b]

    def stop(i: int) -> dict:
        s = {**ride[i]}
        if s.get("lat") is None and point:
            s["lat"], s["lng"] = point(s) or (None, None)
        return s

    shuttle = set(segment.get("freeMrtShuttle") or [])
    if shuttle and _goes_their_way(segment, step):
        for a, b in pairs:
            if ride[a].get("code") in shuttle and ride[b].get("code") in shuttle:
                return _bridge(step, ride, a, b, stop, {"kind": "shuttle"})
    free = set(segment.get("freePublicBus") or [])
    if free and free_bus:
        for a, b in pairs:
            if ride[a].get("code") in free and ride[b].get("code") in free:
                bus = free_bus(stop(a), stop(b))
                if bus:
                    return _bridge(step, ride, a, b, stop, {"kind": "bus", **bus})
    return None


def _bridge(step: dict, ride: list[dict], a: int, b: int, stop: Callable[[int], dict], how: dict) -> dict:
    line = _line_key(ride[a].get("code")) or step.get("line")
    steps = []
    if a > 0:
        steps.append(_train(step, ride, 0, a, stop))
    steps.append({
        "stepIndex": 0, "mode": "bus", "line": how.get("serviceNo"), "from": stop(a), "to": stop(b),
        "instruction": f"Free {'bridging bus' if how['kind'] == 'shuttle' else 'bus ' + how['serviceNo']} "
                       f"from {ride[a].get('name')} to {ride[b].get('name')}",
        "bridge": {**how, "line": line}, "easyRead": None, "expectedDurationS": None, "underground": False,
        "photoKey": None, "audioKey": None, "backup": None,
    })
    if b < len(ride) - 1:
        steps.append(_train(step, ride, b, len(ride) - 1, stop))
    return {"kind": how["kind"], "leaveAt": ride[a], "target": ride[b], "steps": steps}


def _train(step: dict, ride: list[dict], a: int, b: int, stop: Callable[[int], dict]) -> dict:
    """The part of the original train ride from ride[a] to ride[b]."""
    line = step.get("line")
    name = LINE_NAMES.get(line, f"{line} line")
    part = ride[a:b + 1]
    return {
        "stepIndex": 0, "mode": "train", "line": line, "from": stop(a), "to": stop(b), "stops": part,
        "stopCount": len(part) - 1,
        "instruction": f"Take the {name} from {ride[a].get('name')} to {ride[b].get('name')} ({len(part) - 1} stops)",
        "easyRead": None, "expectedDurationS": None, "underground": step.get("underground"),
        "photoKey": None, "audioKey": None, "backup": None,
    }


def splice(steps: list[dict], start: int, end: int, new: list[dict]) -> list[dict]:
    """steps[start..end] (inclusive) replaced by `new`, every step renumbered: the ones before `start` keep their
    index, so a traveller part-way through still points at the right step."""
    out = [*steps[:start], *new, *steps[end + 1:]]
    return [{**s, "stepIndex": i} for i, s in enumerate(out)]
