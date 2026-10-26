"""Which detectors to ask about a check-in. Pure functions, no I/O.

k = 2 when the traveller's risk is normal, k = 4 (every detector) when it's elevated
(adaptive mode). The pair used for k = 2 is deliberately diverse: route-corridor looks
at *where* the traveller is, dwell-time at *how long* they've been there, so one blind
spot doesn't hide a problem from both.
"""

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
