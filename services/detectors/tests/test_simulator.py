import pytest

from detectors.models import CheckIn
from tools.simulator.geometry import distance_m
from tools.simulator.route import build_route
from tools.simulator.scenarios import SCENARIOS, generate, strip_labels


def off_path_m(route, checkin):
    """Distance from a check-in to the nearest dense-path vertex (path spacing is <= 20 m)."""
    p = (checkin["lat"], checkin["lng"])
    return min(distance_m(p, tuple(v)) for v in route["path"])


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_deterministic_for_a_seed(scenario):
    assert generate(scenario, seed=7) == generate(scenario, seed=7)


def test_different_seed_changes_output():
    assert generate("normal", seed=1) != generate("normal", seed=2)


def test_unknown_scenario_raises():
    with pytest.raises(ValueError):
        generate("teleport")


def test_route_path_is_dense():
    route = build_route()
    path = route["path"]
    gaps = [distance_m(tuple(a), tuple(b)) for a, b in zip(path, path[1:])]
    assert max(gaps) <= 20.5
    assert [s["stepIndex"] for s in route["steps"]] == [0, 1, 2]
    assert [s["mode"] for s in route["steps"]] == ["walk", "bus", "walk"]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_checkins_follow_the_contract(scenario):
    checkins = generate(scenario)
    assert [c["seq"] for c in checkins] == list(range(1, len(checkins) + 1))
    for c in checkins:
        CheckIn.model_validate(strip_labels(c))
        assert "scenario" not in strip_labels(c) and "expected" not in strip_labels(c)
    times = [CheckIn.model_validate(strip_labels(c)).timestamp for c in checkins]
    assert {(b - a).total_seconds() for a, b in zip(times, times[1:])} == {10}


def test_normal_and_bus_wait_never_expect_a_problem():
    for scenario in ("normal", "normal_bus_wait"):
        assert {c["expected"] for c in generate(scenario)} == {"normal"}


def test_normal_bus_wait_is_stationary_for_8_minutes():
    checkins = generate("normal_bus_wait")
    waiting = [c for c in checkins if c["stepIndex"] == 1 and c["speedMps"] < 0.5]
    assert len(waiting) * 10 >= 8 * 60


def test_off_route_drifts_150_to_250_m():
    route = build_route()
    checkins = generate("off_route")
    assert max(off_path_m(route, c) for c in checkins) > 130  # noise allows a little shrinkage
    labels = [c["expected"] for c in checkins]
    assert labels[0] == "normal" and labels[-1] == "off_route"


def test_wrong_direction_walks_back():
    checkins = generate("wrong_direction")
    labels = [c["expected"] for c in checkins]
    first = labels.index("wrong_direction")
    assert set(labels[first:]) == {"wrong_direction"}
    start = (checkins[0]["lat"], checkins[0]["lng"])
    turn = checkins[first - 1]
    end = checkins[-1]
    assert distance_m(start, (end["lat"], end["lng"])) < distance_m(start, (turn["lat"], turn["lng"]))


def test_long_dwell_is_stationary_for_15_minutes():
    checkins = generate("long_dwell")
    stationary = [c for c in checkins if c["stepIndex"] == 0 and c["speedMps"] < 0.5]
    assert len(stationary) * 10 >= 15 * 60
    assert "long_dwell" in {c["expected"] for c in checkins}


def test_poor_gps_accuracy_and_label():
    checkins = generate("poor_gps")
    assert all(80 <= c["accuracyM"] <= 120 for c in checkins)
    assert {c["expected"] for c in checkins} == {"abstain"}


def test_normal_gps_accuracy_range():
    assert all(5 <= c["accuracyM"] <= 15 for c in generate("normal"))
