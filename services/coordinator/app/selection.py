"""Which detectors to ask about a check-in. Pure functions, no I/O.

k = 2 when the traveller's risk is normal, k = 4 (every detector) when it's elevated
(adaptive mode).

Which pair is asked at k = 2 depends on PAIR_STRATEGY:

    step   (default) route-corridor plus the detector that fits the current journey step:
               walking                      + heading-speed   (wrong direction)
               bus/train step, slow         + dwell-time      (waiting too long for it)
               bus/train step, moving       + route-progress  (missed stop)
               step unknown                 + route-progress  (covers the most problem types)
           A bus/train step covers both waiting at the stop and riding, so the check-in's
           speed tells them apart.
    fixed  the same pair for every check-in (NORMAL_PAIR, default route-corridor + dwell-time).
"""
import json

# detectorId (contracts/examples/detector-vote.json) -> service name (contracts/api.md)
DETECTOR_SERVICES = {
    "route-corridor": "detector-route",
    "heading-speed": "detector-heading",
    "dwell-time": "detector-dwell",
    "route-progress": "detector-progress",
}
ALL_DETECTORS = tuple(DETECTOR_SERVICES)
DEFAULT_PAIR = ("route-corridor", "dwell-time")

MODES = ("adaptive", "fixed2", "fixed4")
STRATEGIES = ("step", "fixed")

# Step modes in contracts/examples/route.json; "train" as the detectors use it, "mrt" just in case.
TRANSIT_MODES = frozenset({"bus", "train", "mrt"})
WALKING_PAIR = ("route-corridor", "heading-speed")
WAITING_PAIR = ("route-corridor", "dwell-time")
RIDING_PAIR = ("route-corridor", "route-progress")
UNKNOWN_STEP_PAIR = ("route-corridor", "route-progress")


def step_mode(route_json: str | None, step_index) -> str | None:
    """Mode ("walk", "bus", ...) of step `step_index` in trip:{tripId}:route, or None if unknown."""
    if not route_json or step_index is None:
        return None
    try:
        steps = json.loads(route_json)["steps"]
        i = int(step_index)
    except (ValueError, TypeError, KeyError):
        return None
    if not isinstance(steps, list) or not 0 <= i < len(steps) or not isinstance(steps[i], dict):
        return None
    mode = steps[i].get("mode")
    return mode.lower() if isinstance(mode, str) else None


def pair_for_step(mode: str | None, speed_mps, waiting_below_mps: float = 2.0) -> tuple[tuple[str, ...], str]:
    """(pair, situation) for the journey step. situation is for logs and the dashboard."""
    if mode == "walk":
        return WALKING_PAIR, "walking"
    if mode in TRANSIT_MODES:
        try:
            moving = speed_mps is not None and float(speed_mps) >= waiting_below_mps
        except (TypeError, ValueError):
            moving = False
        return (RIDING_PAIR, "riding") if moving else (WAITING_PAIR, "waiting")
    return UNKNOWN_STEP_PAIR, "unknown step"


def choose_detectors(risk_level: str, mode: str = "adaptive",
                     pair: tuple[str, ...] = DEFAULT_PAIR) -> list[str]:
    """Detectors to call first. `mode` is REDUNDANCY_MODE (the experiment's three arms)."""
    if mode not in MODES:
        raise ValueError(f"unknown REDUNDANCY_MODE {mode!r}; expected one of {MODES}")
    if mode == "fixed4" or (mode == "adaptive" and risk_level == "elevated"):
        return list(ALL_DETECTORS)
    return list(pair)


def counted_votes(votes: list[dict]) -> int:
    """Votes that take part in the decision (abstains don't)."""
    return sum(1 for v in votes if v.get("verdict") != "abstain")


def needs_escalation(votes: list[dict], quorum: int) -> bool:
    """Too few real votes to trust the result: ask the remaining detectors too."""
    return counted_votes(votes) < quorum and len(votes) < len(ALL_DETECTORS)


def remaining(already_called: list[str]) -> list[str]:
    return [d for d in ALL_DETECTORS if d not in already_called]


def parse_urls(spec: str) -> dict[str, str]:
    """DETECTOR_URLS="route-corridor=http://a:8000,dwell-time=http://b:8000" -> dict.
    Detectors not listed use their in-cluster service name."""
    urls = {d: f"http://{svc}:8000" for d, svc in DETECTOR_SERVICES.items()}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        name, _, url = part.partition("=")
        if name.strip() not in DETECTOR_SERVICES or not url.strip():
            raise ValueError(f"bad DETECTOR_URLS entry {part!r}")
        urls[name.strip()] = url.strip().rstrip("/")
    return urls
