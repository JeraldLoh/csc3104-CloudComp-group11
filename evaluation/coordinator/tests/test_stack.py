"""The journey helpers in stack.py (no stack needed: nothing connects until Stack() is made)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stack import journey, off_contract  # noqa: E402


def test_each_journey_gets_its_own_trip_and_traveller():
    a, b = journey("normal", 1, "R1"), journey("normal", 2, "R1")
    assert {c["tripId"] for c in a} == {"T-R1-normal-1"} and {c["travellerId"] for c in a} == {"U-R1-normal-1"}
    assert a[0]["tripId"] != b[0]["tripId"]
    assert all("expected" in c for c in a)


def test_off_route_journeys_that_stay_near_the_route_are_rejected():
    # Seeds checked by hand: seed 1 drifts towards the road to the bus stop (at most ~73 m from
    # the route); seed 4 really leaves it (~170 m).
    assert "never more than" in off_contract("off_route", journey("off_route", 1, "R"))
    assert off_contract("off_route", journey("off_route", 4, "R")) is None


def test_other_scenarios_are_always_used():
    assert off_contract("wrong_direction", journey("wrong_direction", 1, "R")) is None
