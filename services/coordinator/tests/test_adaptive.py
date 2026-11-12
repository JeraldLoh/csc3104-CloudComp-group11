"""The experiment (adaptive.py, evaluation C5): adaptive and fixed2 ask a pair of detectors first."""
import json

import pytest

from app.adaptive import (RIDING_PAIR, UNKNOWN_STEP_PAIR, WAITING_PAIR, WALKING_PAIR, choose_detectors,
                          counted_votes, needs_escalation, pair_for_step, remaining, step_mode)
from app.config import DEFAULT_PAIR
from app.decision import WEIGHTS_KEY
from app.detectors import ALL_DETECTORS
from tests.fakes import ALL_FOUR, CHECKIN, ELEVATED, HOSTS, ROUTE_KEY, World, new_redis, process


def vote(verdict: str) -> dict:
    return {"verdict": verdict}


def ask(world: World, redis=None, checkin=CHECKIN, **overrides):
    """One check-in in adaptive mode with the fixed pair (route-corridor + dwell-time), unless overridden."""
    return process(world, redis, checkin, **{"redundancy_mode": "adaptive", "pair_strategy": "fixed", **overrides})


# --- choose_detectors ---------------------------------------------------------

def test_adaptive_normal_risk_uses_the_diverse_pair():
    assert choose_detectors("normal") == ["route-corridor", "dwell-time"]


def test_adaptive_elevated_risk_uses_every_detector():
    assert choose_detectors("elevated") == list(ALL_DETECTORS)


def test_fixed2_ignores_risk():
    assert choose_detectors("elevated", mode="fixed2") == list(DEFAULT_PAIR)


def test_fixed4_ignores_risk():
    assert choose_detectors("normal", mode="fixed4") == list(ALL_DETECTORS)


def test_unknown_risk_level_is_treated_as_normal():
    assert choose_detectors("something-else") == list(DEFAULT_PAIR)


def test_custom_pair():
    assert choose_detectors("normal", pair=("heading-speed", "route-progress")) == ["heading-speed", "route-progress"]


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        choose_detectors("normal", mode="fixed3")


def test_default_pair_is_diverse():
    # Location-based and time-based, not two similar detectors.
    assert set(DEFAULT_PAIR) == {"route-corridor", "dwell-time"}


# --- votes and escalation -------------------------------------------------------

def test_abstains_are_not_counted():
    assert counted_votes([vote("problem"), vote("abstain"), vote("normal")]) == 2


def test_no_escalation_when_quorum_met():
    assert not needs_escalation([vote("problem"), vote("normal")], quorum=2)


def test_escalates_when_one_of_two_abstains():
    assert needs_escalation([vote("problem"), vote("abstain")], quorum=2)


def test_escalates_when_both_abstain():
    assert needs_escalation([vote("abstain"), vote("abstain")], quorum=2)


def test_never_escalates_beyond_every_detector():
    all_abstain = [vote("abstain")] * len(ALL_DETECTORS)
    assert not needs_escalation(all_abstain, quorum=2)


def test_remaining_detectors_keep_their_order():
    assert remaining(["route-corridor", "dwell-time"]) == ["heading-speed", "route-progress"]


def test_nothing_remaining_after_every_detector():
    assert remaining(list(ALL_DETECTORS)) == []


# --- step-aware pairs -------------------------------------------------------------------------

STEPS = json.dumps({"steps": [{"stepIndex": 0, "mode": "walk"}, {"stepIndex": 1, "mode": "Bus"}], "path": []})


@pytest.mark.parametrize("index, mode", [(0, "walk"), (1, "bus"), ("1", "bus"), (2, None), (-1, None), (None, None)])
def test_step_mode(index, mode):
    assert step_mode(STEPS, index) == mode


@pytest.mark.parametrize("route", [None, "", "{not json", '{"path": []}', '{"steps": "x"}', '{"steps": [5]}'])
def test_step_mode_of_a_bad_route_is_unknown(route):
    assert step_mode(route, 0) is None


@pytest.mark.parametrize("mode, speed, pair, situation", [
    ("walk", 1.3, WALKING_PAIR, "walking"),
    ("walk", None, WALKING_PAIR, "walking"),
    ("bus", 0.0, WAITING_PAIR, "waiting"),
    ("bus", 1.99, WAITING_PAIR, "waiting"),
    ("bus", 2.0, RIDING_PAIR, "riding"),
    ("train", 15, RIDING_PAIR, "riding"),
    ("mrt", 15, RIDING_PAIR, "riding"),
    ("bus", None, WAITING_PAIR, "waiting"),
    ("bus", "fast", WAITING_PAIR, "waiting"),
    ("ferry", 5, UNKNOWN_STEP_PAIR, "unknown step"),
    (None, 5, UNKNOWN_STEP_PAIR, "unknown step"),
])
def test_pair_for_step(mode, speed, pair, situation):
    assert pair_for_step(mode, speed) == (pair, situation)


def test_every_step_pair_includes_route_corridor_and_only_real_detectors():
    for pair in (WALKING_PAIR, WAITING_PAIR, RIDING_PAIR, UNKNOWN_STEP_PAIR):
        assert "route-corridor" in pair and len(pair) == 2 and set(pair) <= set(ALL_DETECTORS)


# --- the pipeline in adaptive / fixed2: a pair at normal risk -------------------------------------

def test_normal_risk_asks_the_diverse_pair_once():
    world = World()
    result, state = ask(world)
    assert sorted(world.asked) == ["dwell-time", "route-corridor"]
    assert result.outcome == "decided" and result.k == 2 and not result.escalated
    assert result.decision["decision"] == "normal"
    assert state["risk"]["detectorsUsed"] == "2"


def test_one_problem_from_the_pair_alerts_at_once():
    # Any detector that checks the type saying problem is enough: no second opinion first.
    world = World(detector_votes={"route-corridor": "problem"})
    result, _ = ask(world)
    assert result.k == 2 and not result.escalated
    assert len(world.reports) == 1 and world.reports[0]["detectorsUsed"] == 2


def test_decision_record_shows_the_pair_and_why():
    _, state = ask(World())
    record = state["decisions"][0]
    assert record["k"] == 2 and record["escalated"] is False and record["situation"] == "fixed"


def test_elevated_risk_asks_every_detector():
    world = World()
    result, _ = ask(world, new_redis(hashes=ELEVATED))
    assert sorted(world.asked) == ALL_FOUR
    assert result.k == 4 and not result.escalated
    assert world.detector_calls[0][1]["riskLevel"] == "elevated"


def test_fixed2_ignores_elevated_risk():
    world = World()
    ask(world, new_redis(hashes=ELEVATED), redundancy_mode="fixed2")
    assert len(world.asked) == 2


def test_start_of_trip_the_pair_cant_judge_so_everyone_is_asked():
    warming_up = ("abstain", "only 1 usable check-in(s), need 3")
    world = World(detector_votes={d: warming_up for d in HOSTS.values()} | {"dwell-time": "normal"})
    result, _ = ask(world)
    assert result.k == 4 and result.escalated                        # asked everyone first
    assert result.decision["decision"] == "inconclusive" and result.decision["riskLevel"] == "normal"


# --- escalation: the pair can't decide, so the rest are asked -----------------------------------

def test_too_few_real_votes_asks_the_rest_before_deciding():
    world = World(detector_votes={"dwell-time": "timeout"})
    result, state = ask(world)
    assert result.k == 4 and result.escalated
    assert len(state["decisions"]) == 1 and state["decisions"][0]["k"] == 4


def test_escalated_votes_keep_the_abstain():
    world = World(detector_votes={"dwell-time": "down"})
    _, state = ask(world)
    dwell = next(v for v in state["decisions"][0]["votes"] if v["detectorId"] == "dwell-time")
    assert dwell["verdict"] == "abstain" and dwell["reason"] == "error: ConnectError"


def test_answers_without_weight_ask_the_rest_and_decide_again():
    # Both of the pair are known to be faulty (reliability 0): their normals can't call it.
    world = World()
    redis = new_redis(hashes={WEIGHTS_KEY: {"route-corridor": "0", "dwell-time": "0"}})
    result, _ = ask(world, redis)
    assert sorted(world.asked) == ALL_FOUR
    assert result.escalated and result.k == 4
    assert result.decision["decision"] == "normal"


def test_no_one_left_to_ask_records_the_inconclusive():
    redis = new_redis(hashes={**ELEVATED, WEIGHTS_KEY: {d: "0" for d in HOSTS.values()}})
    result, state = ask(World(), redis)
    assert result.k == 4 and not result.escalated
    assert result.decision["decision"] == "inconclusive" and result.decision["reason"] == "quorum"
    assert state["decisions"][0]["decision"] == "inconclusive"


def test_no_detector_is_asked_twice():
    world = World(detector_votes={"dwell-time": "timeout"})
    ask(world)
    assert sorted(world.asked) == ALL_FOUR


# --- the pipeline with the step-aware pair (PAIR_STRATEGY=step) ----------------------------------

def at_step(step, speed=1.3):
    return {**CHECKIN, "stepIndex": step, "speedMps": speed}


@pytest.mark.parametrize("step, speed, pair, situation", [
    (0, 1.3, ["heading-speed", "route-corridor"], "walking"),
    (1, 0.2, ["dwell-time", "route-corridor"], "waiting"),
    (1, None, ["dwell-time", "route-corridor"], "waiting"),       # no speed: assume still at the stop
    (1, 9.0, ["route-corridor", "route-progress"], "riding"),
])
def test_pair_fits_the_journey_step(step, speed, pair, situation):
    world = World()
    result, state = ask(world, new_redis(keys=ROUTE_KEY), checkin=at_step(step, speed), pair_strategy="step")
    assert sorted(world.asked) == pair
    assert result.situation == situation and result.k == 2
    assert state["decisions"][0]["situation"] == situation


@pytest.mark.parametrize("keys, checkin", [
    ({}, at_step(0)),                                            # no route in Redis
    (ROUTE_KEY, at_step(7)),                                     # step index beyond the route
    (ROUTE_KEY, {**at_step(0), "stepIndex": None}),
    ({"trip:T-001:route": "{not json"}, at_step(0)),
])
def test_unknown_step_falls_back_to_route_corridor_and_route_progress(keys, checkin):
    world = World()
    result, _ = ask(world, new_redis(keys=keys), checkin=checkin, pair_strategy="step")
    assert sorted(world.asked) == ["route-corridor", "route-progress"]
    assert result.situation == "unknown step"


def test_elevated_risk_still_asks_everyone_and_skips_the_route_lookup():
    world = World()
    result, _ = ask(world, new_redis(hashes=ELEVATED), checkin=at_step(0), pair_strategy="step")
    assert result.k == 4 and result.situation is None


def test_fixed_strategy_ignores_the_step():
    world = World()
    result, _ = ask(world, new_redis(keys=ROUTE_KEY), checkin=at_step(1, 9.0), pair_strategy="fixed")
    assert sorted(world.asked) == ["dwell-time", "route-corridor"]
    assert result.situation == "fixed"


def test_fixed2_also_uses_the_step_pair():
    world = World()
    ask(world, new_redis(hashes=ELEVATED, keys=ROUTE_KEY), checkin=at_step(0), redundancy_mode="fixed2",
        pair_strategy="step")
    assert sorted(world.asked) == ["heading-speed", "route-corridor"]


def test_custom_waiting_speed():
    world = World()
    result, _ = ask(world, new_redis(keys=ROUTE_KEY), checkin=at_step(1, 1.5), pair_strategy="step",
                    waiting_below_mps=1.0)
    assert result.situation == "riding"


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError):
        ask(World(), new_redis(keys=ROUTE_KEY), checkin=at_step(0), pair_strategy="random")
