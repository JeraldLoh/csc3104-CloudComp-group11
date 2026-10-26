import json
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from pydantic import ValidationError

from detectors.models import CheckIn, DetectorRequest

log = logging.getLogger(__name__)


# everything a detector needs about the trip
@dataclass
class TripContext:
    # None when the route is missing or malformed
    steps: Optional[List[dict]] = None
    path: Optional[List[Tuple[float, float]]] = None
    # from trip context and falls back to the check-in step
    step_index: Optional[int] = None
    route_version: Optional[int] = None
    status: Optional[str] = None
    # oldest to newest including this check-in
    recent: List[CheckIn] = field(default_factory=list)
    # check-in and context disagree on route version
    route_version_mismatch: bool = False


# redis may return bytes
def _text(value: Any) -> Any:
    return value.decode() if isinstance(value, bytes) else value


# route steps and path or None when missing or malformed
def _parse_route(raw: Any) -> Tuple[Optional[List[dict]], Optional[List[Tuple[float, float]]]]:
    if raw is None:
        return None, None
    try:
        data = json.loads(raw)
        steps = data["steps"]
        path = [(float(p[0]), float(p[1])) for p in data["path"]]
        if not isinstance(steps, list):
            raise TypeError("steps is not a list")
    except (ValueError, KeyError, IndexError, TypeError):
        log.warning("malformed route JSON, treating as missing")
        return None, None
    return steps, path


# int or None when missing or not a number
def _parse_int(value: Any) -> Optional[int]:
    try:
        return int(_text(value))
    except (TypeError, ValueError):
        return None


# bad entries are skipped not fatal
def _parse_recent(items: List[Any]) -> List[CheckIn]:
    parsed = []
    for item in items:
        try:
            parsed.append(CheckIn.model_validate_json(item))
        except ValidationError:
            log.warning("skipping malformed entry in recent list")
    return parsed


# read route context and recent check-ins from redis
def load_context(redis, request: DetectorRequest) -> TripContext:
    checkin = request.checkin
    trip = checkin.trip_id

    # route steps and path
    steps, path = _parse_route(redis.get(f"trip:{trip}:route"))

    # current step and route version
    raw_ctx = {_text(k): _text(v) for k, v in redis.hgetall(f"trip:{trip}:context").items()}
    step_index = _parse_int(raw_ctx.get("stepIndex"))
    route_version = _parse_int(raw_ctx.get("routeVersion"))

    # merge this check-in since it may not be in redis yet
    by_seq = {c.seq: c for c in _parse_recent(redis.lrange(f"trip:{trip}:recent", 0, -1))}
    by_seq[checkin.seq] = checkin

    return TripContext(
        steps=steps,
        path=path,
        step_index=step_index if step_index is not None else checkin.step_index,
        route_version=route_version,
        status=raw_ctx.get("status"),
        recent=[by_seq[s] for s in sorted(by_seq)],
        # no route version on the check-in is unknown not a mismatch
        route_version_mismatch=route_version is not None
        and checkin.route_version is not None
        and route_version != checkin.route_version
    )
