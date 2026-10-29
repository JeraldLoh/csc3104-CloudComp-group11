"""Journey scenarios for the demo panel. Pure, no I/O.

Each scenario is a list of check-ins on the example trip (contracts/examples/route.json:
step 0 walk to Punggol Int, step 1 bus to Opp Block 221). Positions and speeds are chosen
so the Coordinator picks the matching pair: walking, waiting at the stop, riding.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

ON_ROUTE = (1.4052, 103.9024)      # on the path (the example check-in's position)
BUS_ROUTE = (1.3980, 103.8980)     # further along the path, on the bus leg
OFF_ROUTE = (1.4500, 103.9500)     # about 7 km away


@dataclass(frozen=True)
class Move:
    step: int
    lat: float
    lng: float
    speed_mps: float


WALK = Move(0, *ON_ROUTE, 1.3)
WAIT = Move(1, *ON_ROUTE, 0.2)
RIDE = Move(1, *BUS_ROUTE, 9.0)
OFF = Move(0, *OFF_ROUTE, 1.3)

SCENARIOS: dict[str, dict] = {
    "walk": {"label": "Walk on route", "moves": [WALK] * 3},
    "wait": {"label": "Wait at bus stop", "moves": [WAIT] * 2},
    "ride": {"label": "Ride the bus", "moves": [RIDE] * 2},
    "off-route": {"label": "Go off route", "moves": [OFF] * 3},
    "back": {"label": "Back on route", "moves": [WALK] * 4},
    "journey": {"label": "Full journey", "moves": [WALK] * 5 + [WAIT] * 2 + [RIDE] + [OFF] * 3 + [WALK] * 4},
}


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_checkin(trip_id: str, traveller_id: str, seq: int, move: Move, route_version: int,
                  now: datetime | None = None) -> dict:
    """checkin.json (contracts/examples) for one move."""
    return {
        "tripId": trip_id, "travellerId": traveller_id, "seq": seq,
        "timestamp": iso_z(now or datetime.now(timezone.utc)),
        "lat": move.lat, "lng": move.lng, "accuracyM": 12, "speedMps": move.speed_mps,
        "headingDeg": 85, "stepIndex": move.step, "routeVersion": route_version,
        "batteryPct": 64, "status": "active", "bufferedOffline": False,
    }
