"""Load a trip's context from Redis (read-only).

Keys are described in contracts/redis-keys.md:

- ``trip:{tripId}:route``    JSON string, route steps + ``path`` ([[lat, lng], ...])
- ``trip:{tripId}:context``  hash: ``stepIndex``, ``routeVersion``, ``status``
- ``trip:{tripId}:recent``   list of check-in JSON, newest first (max 10)

Detectors never write to Redis. Missing or malformed data does not raise: the
affected field is ``None`` and detectors decide whether that means "abstain".
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from pydantic import ValidationError

from detectors.models import CheckIn, DetectorRequest

log = logging.getLogger(__name__)


@dataclass
class TripContext:
    # None when trip:{id}:route is missing or malformed
    steps: Optional[List[dict]] = None
    path: Optional[List[Tuple[float, float]]] = None
    # From trip:{id}:context; step_index falls back to the request's check-in
    step_index: Optional[int] = None
    route_version: Optional[int] = None
    status: Optional[str] = None
    # Oldest -> newest, includes the request's own check-in
    recent: List[CheckIn] = field(default_factory=list)
    # True when the check-in and the context disagree on routeVersion
    route_version_mismatch: bool = False


def _text(value: Any) -> Any:
    return value.decode() if isinstance(value, bytes) else value


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


def _parse_int(value: Any) -> Optional[int]:
    try:
        return int(_text(value))
    except (TypeError, ValueError):
        return None


def _parse_recent(items: List[Any]) -> List[CheckIn]:
    parsed = []
    for item in items:
        try:
            parsed.append(CheckIn.model_validate_json(item))
        except ValidationError:
            log.warning("skipping malformed entry in recent list")
    return parsed


def load_context(redis, request: DetectorRequest) -> TripContext:
    """Read the trip's route, context and recent check-ins for ``request``.

    The request's own check-in is merged into ``recent`` (it may not be in Redis
    yet); on a duplicate ``seq`` the request's copy wins. Redis errors propagate
    so the caller can turn them into an abstain vote.
    """
    checkin = request.checkin
    trip = checkin.trip_id

    steps, path = _parse_route(redis.get(f"trip:{trip}:route"))

    raw_ctx = {_text(k): _text(v) for k, v in redis.hgetall(f"trip:{trip}:context").items()}
    step_index = _parse_int(raw_ctx.get("stepIndex"))
    route_version = _parse_int(raw_ctx.get("routeVersion"))

    by_seq = {c.seq: c for c in _parse_recent(redis.lrange(f"trip:{trip}:recent", 0, -1))}
    by_seq[checkin.seq] = checkin

    return TripContext(
        steps=steps,
        path=path,
        step_index=step_index if step_index is not None else checkin.step_index,
        route_version=route_version,
        status=raw_ctx.get("status"),
        recent=[by_seq[s] for s in sorted(by_seq)],
        route_version_mismatch=route_version is not None
        and route_version != checkin.route_version,
    )
