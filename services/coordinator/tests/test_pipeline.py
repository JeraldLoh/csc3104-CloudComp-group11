"""The pipeline in fixed4, the mode the Coordinator runs in: every check-in, all 4 detectors.
The world (fake detectors, fake Alert Service, fakeredis) is in fakes.py; the experiment's
adaptive and fixed2 modes are tested in test_adaptive.py."""
import asyncio
import json

import httpx
import pytest

from app.decision import WEIGHTS_KEY, decided_key
from tests.fakes import (ALL_FOUR, CHECKIN, DETECTOR_REQUEST_FIELDS, HOSTS, REPORT_FIELDS, ROUTE, ROUTE_KEY, World,
                         new_redis, process, process_all, seq)


# --- normal path ----------------------------------------------------------------------------

def test_asks_every_detector_once():
    world = World()
    result, state = process(world)
    assert sorted(world.asked) == ALL_FOUR
    assert result.outcome == "decided" and result.k == 4 and not result.escalated and result.situation is None
    assert result.decision == {"decision": "normal", "problemType": None, "confidence": 1.0, "reason": None,
                               "riskLevel": "normal"}
    assert world.reports == []
    assert state["risk"]["level"] == "normal" and state["risk"]["detectorsUsed"] == "4"
    assert len(state["decisions"]) == 1 and len(state["recent"]) == 1


def test_marks_the_checkin_as_decided_for_a_day():
    _, state = process(World())
    value, ttl = state["decided"][decided_key("T-001", 17)]
    assert value == "1" and 0 < ttl <= 86400


def test_detectors_receive_a_contract_shaped_request():
    world = World()
    process(world)
    for _, body in world.detector_calls:
        assert set(body) == DETECTOR_REQUEST_FIELDS
        assert body["requestId"] == "T-001#17"
        assert body["riskLevel"] == "normal"
        assert body["checkin"] == CHECKIN


# --- problems are reported ---------------------------------------------------------------------------

def test_problem_is_reported_to_the_alert_service_and_raises_risk():
    world = World(detector_votes={"route-corridor": "problem"})
    result, state = process(world)
    assert result.decision["decision"] == "problem" and result.decision["problemType"] == "off_route"
    assert result.decision["riskLevel"] == "elevated"
    [report] = world.reports
    assert set(report) == REPORT_FIELDS
    assert report["reportId"] == "T-001#17" and report["detectorsUsed"] == 4
    record = state["decisions"][0]
    assert record["incidentId"] == "U-042#T-001#off_route#2" and record["alertCreated"] is True


def test_two_problem_types_at_once_are_both_reported():
    # Walking sideways off the road can look like the wrong direction too: the caregiver gets both.
    world = World(detector_votes={"route-corridor": "problem", "heading-speed": "problem"})
    result, state = process(world)
    assert sorted(r["problemType"] for r in world.reports) == ["off_route", "wrong_direction"]
    assert world.incidents == {"U-042#T-001#off_route#2", "U-042#T-001#wrong_direction#2"}
    record = state["decisions"][0]
    assert [a["problemType"] for a in record["alerts"]] == ["off_route", "wrong_direction"]   # equal: alphabetical
    assert record["incidentId"] == "U-042#T-001#off_route#2"
    assert result.decision["riskLevel"] == "elevated"


def test_a_failed_second_report_retries_the_checkin_without_a_second_incident():
    world = World(detector_votes={"route-corridor": "problem", "heading-speed": "problem"},
                  alert_fails_for="wrong_direction")
    redis = new_redis()
    with pytest.raises(httpx.HTTPStatusError):
        process(world, redis)
    assert not asyncio.run(redis.exists(decided_key("T-001", 17)))   # not decided: SQS will retry
    world.alert_fails_for = None
    result, state = process(world, redis)
    assert result.outcome == "decided"
    assert world.incidents == {"U-042#T-001#off_route#2", "U-042#T-001#wrong_direction#2"}
    assert [a["created"] for a in state["decisions"][0]["alerts"]] == [False, True]   # off_route already existed
    assert len(state["decisions"]) == 1


def test_decision_record_has_what_the_dashboard_needs():
    world = World(detector_votes={"route-corridor": "problem"})
    _, state = process(world)
    record = state["decisions"][0]
    assert record["requestId"] == "T-001#17" and record["seq"] == 17 and record["stepIndex"] == 2
    assert record["k"] == 4 and record["escalated"] is False and record["situation"] is None
    assert record["votes"][0] == {"detectorId": "route-corridor", "verdict": "problem",
                                  "problemType": "off_route", "confidence": 0.8,
                                  "latencyMs": record["votes"][0]["latencyMs"], "reason": "test"}
    assert record["riskBefore"] == {"level": "normal", "score": 0.0}
    assert record["riskAfter"] == {"level": "elevated", "score": 0.5}
    assert state["recent"][0] == record


# --- too few votes, failures, silencing ---------------------------------------------------------

def test_too_few_votes_is_inconclusive_without_asking_again():
    world = World(detector_votes={"route-corridor": "abstain", "heading-speed": "abstain",
                                  "dwell-time": "abstain"})
    result, state = process(world)
    assert sorted(world.asked) == ALL_FOUR
    assert result.k == 4 and not result.escalated
    assert result.decision["decision"] == "inconclusive"
    assert state["decided"]   # still marked decided: a redelivery isn't asked again


def test_a_failed_detector_is_recorded_as_an_abstain_with_its_reason():
    world = World(detector_votes={"dwell-time": "down"})
    result, state = process(world)
    dwell = next(v for v in state["decisions"][0]["votes"] if v["detectorId"] == "dwell-time")
    assert dwell["verdict"] == "abstain" and dwell["reason"] == "error: ConnectError"
    assert result.decision["decision"] == "normal"   # the other three still decide


def test_zero_reliability_silences_a_faulty_detector():
    world = World(detector_votes={"route-corridor": "problem"})
    plain, _ = process(world)
    silenced, _ = process(World(detector_votes={"route-corridor": "problem"}),
                          new_redis(hashes={WEIGHTS_KEY: {"route-corridor": "0"}}))
    assert plain.decision["decision"] == "problem"
    assert silenced.decision["decision"] == "normal"


def test_every_detector_silenced_is_inconclusive():
    redis = new_redis(hashes={WEIGHTS_KEY: {d: "0" for d in HOSTS.values()}})
    result, state = process(World(), redis)
    assert result.k == 4 and not result.escalated
    # No trusted answer at all, so too few to call it normal.
    assert result.decision["decision"] == "inconclusive" and result.decision["reason"] == "quorum"
    assert state["decisions"][0]["decision"] == "inconclusive"


# --- risk (recorded and shown on the dashboard; adaptive also uses it, test_adaptive.py) ----------

def test_risk_over_a_journey():
    redis = new_redis()
    [problem], _ = process_all(World(detector_votes={"route-corridor": "problem"}), [seq(17)], redis)
    calm, state = process_all(World(), [seq(18), seq(19), seq(20)], redis)
    assert problem.decision["riskLevel"] == "elevated"
    # Hysteresis: back to normal only after 3 calm check-ins in a row.
    assert [r.decision["riskLevel"] for r in calm] == ["elevated", "elevated", "normal"]
    assert state["risk"]["calmStreak"] == "3"


def test_start_of_trip_inconclusive_leaves_risk_unchanged():
    # The detectors need history before they can judge (their own reasons), so nobody can call it.
    warming_up = ("abstain", "only 1 usable check-in(s), need 3")
    world = World(detector_votes={d: warming_up for d in HOSTS.values()} | {"dwell-time": "normal"})
    result, state = process(world)
    assert result.decision["decision"] == "inconclusive" and result.decision["riskLevel"] == "normal"
    assert world.reports == []
    assert state["decisions"][0]["decision"] == "inconclusive"         # still recorded for the dashboard
    assert state["decisions"][0]["riskAfter"] == {"level": "normal", "score": 0.0}


def test_inconclusive_because_detectors_failed_raises_risk_without_alert():
    world = World(detector_votes={"dwell-time": "timeout", "heading-speed": "down", "route-progress": "timeout"})
    result, state = process(world)
    assert result.decision["decision"] == "inconclusive"
    assert world.reports == []
    assert state["risk"]["level"] == "elevated"


def test_decision_lists_are_trimmed():
    _, state = process_all(World(), [seq(s) for s in range(1, 6)], decisions_per_traveller=3, decisions_recent=4)
    assert [d["seq"] for d in state["decisions"]] == [5, 4, 3]
    assert len(state["recent"]) == 4


# --- duplicates ------------------------------------------------------------------------------------

def test_already_decided_checkin_is_skipped():
    world = World()
    result, _ = process(world, new_redis(keys={decided_key("T-001", 17): "1"}))
    assert result.outcome == "duplicate"
    assert world.detector_calls == [] and world.reports == []


def test_processing_the_same_checkin_twice_decides_once():
    world = World(detector_votes={"route-corridor": "problem"})
    (first, second), state = process_all(world, [CHECKIN, CHECKIN])
    assert (first.outcome, second.outcome) == ("decided", "duplicate")
    assert len(world.reports) == 1
    assert len(state["decisions"]) == 1
    assert state["risk"]["score"] == "0.5000"   # not raised twice


def test_different_checkins_are_both_decided():
    results, state = process_all(World(), [seq(17), seq(18)])
    assert [r.outcome for r in results] == ["decided", "decided"]
    assert [d["seq"] for d in state["decisions"]] == [18, 17]   # newest first


# --- failures: raise so the message is retried -----------------------------------------------------------

@pytest.mark.parametrize("world", [World(detector_votes={"route-corridor": "problem"}, alert_status=503),
                                   World(detector_votes={"route-corridor": "problem"}, alert_down=True)])
def test_alert_service_failure_raises_and_changes_nothing(world):
    redis = new_redis()
    with pytest.raises(httpx.HTTPError):
        process(world, redis)

    async def state():
        return await redis.keys("*")
    assert asyncio.run(state()) == []   # not decided, no risk, no record: the retry starts clean


def test_after_an_alert_failure_the_retry_decides_once():
    redis = new_redis()
    world = World(detector_votes={"route-corridor": "problem"}, alert_status=503)
    with pytest.raises(httpx.HTTPStatusError):
        process(world, redis)
    world.alert_status = 200
    result, state = process(world, redis)
    assert result.decision["decision"] == "problem"
    assert len(state["decisions"]) == 1 and len(world.incidents) == 1


def test_redis_down_raises():
    with pytest.raises(Exception):
        process(World(), new_redis(connected=False))


@pytest.mark.parametrize("bad", [
    {k: v for k, v in CHECKIN.items() if k != "tripId"},
    {k: v for k, v in CHECKIN.items() if k != "travellerId"},
    {**CHECKIN, "seq": "seventeen"},
])
def test_invalid_checkin_raises_before_calling_anyone(bad):
    world = World()
    with pytest.raises(ValueError):
        process(world, checkin=bad)
    assert world.detector_calls == [] and world.reports == []


# --- tracking lost (tracking.py) --------------------------------------------------------------------------

def checkin_at(n: int, seconds: int, accuracy=90, step=2) -> dict:
    """Check-in n, `seconds` after 08:00:00; by default with GPS too poor for any detector."""
    return {**CHECKIN, "seq": n, "accuracyM": accuracy, "stepIndex": step,
            "timestamp": f"2026-10-12T08:{seconds // 60:02d}:{seconds % 60:02d}Z"}


ALL_ABSTAIN = {d: "abstain" for d in HOSTS.values()}
TWO_MINUTES = (0, 60, 120, 130)


def track(world, seconds=TWO_MINUTES, redis=None, **checkin):
    """Check-ins at these times, on a trip whose route is in Redis unless `redis` says otherwise."""
    return process_all(world, [checkin_at(i + 1, s, **checkin) for i, s in enumerate(seconds)],
                       redis if redis is not None else new_redis(keys=ROUTE_KEY))


def test_poor_gps_for_two_minutes_reports_tracking_lost_once_it_lasts():
    world = World(detector_votes=ALL_ABSTAIN)
    results, state = track(world)
    assert [r.decision["decision"] for r in results] == ["inconclusive"] * 4
    assert [r["problemType"] for r in world.reports] == ["tracking_lost", "tracking_lost"]   # from 2 min on
    assert world.reports[0]["reason"] == "poor GPS"
    assert world.incidents == {"U-042#T-001#tracking_lost#None"}                           # one incident
    assert state["decisions"][0]["alerts"][0]["problemType"] == "tracking_lost"
    assert state["decisions"][0]["unchecked"] == ["long_dwell", "missed_stop", "off_route", "wrong_direction"]
    assert state["risk"]["level"] == "normal"   # couldn't judge: risk unchanged


def test_shorter_poor_gps_is_not_reported():
    world = World(detector_votes=ALL_ABSTAIN)
    _, state = track(world, seconds=(0, 110))
    assert world.reports == []
    assert state["decisions"][0]["uncheckedReason"] == "poor GPS"   # counted, just not long enough yet


def test_a_trackable_checkin_ends_the_streak():
    redis = new_redis(keys=ROUTE_KEY)
    track(World(detector_votes=ALL_ABSTAIN), seconds=(0,), redis=redis)
    _, state = process_all(World(), [checkin_at(2, 60, accuracy=10)], redis)   # normal
    assert state["decisions"][0]["unchecked"] == [] and state["decisions"][0]["uncheckedReason"] is None
    world = World(detector_votes=ALL_ABSTAIN)
    process_all(world, [checkin_at(3, 150)], redis)
    assert world.reports == []   # the streak restarted at 150 s
    assert asyncio.run(redis.hget("untracked:T-001", "since")) == "2026-10-12T08:02:30Z"


def test_no_tracking_lost_on_an_underground_step():
    tunnel = {"trip:T-001:route": json.dumps({"steps": [{"stepIndex": i, "mode": "train", "underground": None}
                                                        for i in range(3)], "path": ROUTE["path"]})}
    world = World(detector_votes=ALL_ABSTAIN)
    track(world, seconds=(0, 300), redis=new_redis(keys=tunnel))
    assert world.reports == []


def test_one_tracking_lost_incident_per_trip_across_steps():
    world = World(detector_votes=ALL_ABSTAIN)
    process_all(world, [checkin_at(1, 0, step=0), checkin_at(2, 130, step=0), checkin_at(3, 200, step=1)],
                new_redis(keys=ROUTE_KEY))
    assert len(world.reports) == 2 and world.incidents == {"U-042#T-001#tracking_lost#None"}


def test_route_missing_for_two_minutes_reports_tracking_lost():
    # Good GPS, but without the route no detector can judge anything.
    world = World(detector_votes={d: ("abstain", "no route in Redis for this trip") for d in HOSTS.values()})
    track(world, redis=new_redis(), accuracy=10)
    assert [r["reason"] for r in world.reports] == ["route missing", "route missing"]


def test_every_detector_down_for_two_minutes_reports_tracking_lost(caplog):
    world = World(detector_votes={d: "down" for d in HOSTS.values()})
    results, state = track(world, accuracy=10)
    assert [r.decision["decision"] for r in results] == ["inconclusive"] * 4
    assert [r["reason"] for r in world.reports] == ["detectors failed"] * 2
    assert world.reports[0]["unchecked"] == ["long_dwell", "missed_stop", "off_route", "wrong_direction"]
    assert "unchecked since 2026-10-12T08:00:00Z (detectors failed)" in caplog.text   # for whoever runs it


def test_the_only_detector_for_a_type_down_reports_tracking_lost_while_the_rest_decide():
    world = World(detector_votes={"dwell-time": "down"})
    results, state = track(world, accuracy=10)
    assert [r.decision["decision"] for r in results] == ["normal"] * 4   # the others still decide
    assert [r["problemType"] for r in world.reports] == ["tracking_lost", "tracking_lost"]
    assert world.reports[0]["unchecked"] == ["long_dwell"]
    assert state["decisions"][0]["unchecked"] == ["long_dwell"]


def test_the_only_detector_for_a_type_silenced_reports_tracking_lost():
    # An operator silenced dwell-time (wrong answers): long_dwell is then checked by nobody.
    world = World()
    results, state = track(world, redis=new_redis(keys=ROUTE_KEY, hashes={WEIGHTS_KEY: {"dwell-time": "0"}}),
                           accuracy=10)
    assert [r.decision["decision"] for r in results] == ["normal"] * 4
    assert [r["reason"] for r in world.reports] == ["detectors silenced"] * 2
    assert world.reports[0]["unchecked"] == ["long_dwell"]


def test_a_silenced_detector_with_a_second_judge_is_not_unchecked():
    world = World()
    _, state = track(world, redis=new_redis(keys=ROUTE_KEY, hashes={WEIGHTS_KEY: {"route-corridor": "0"}}),
                     accuracy=10)
    assert world.reports == [] and state["decisions"][0]["unchecked"] == []


def test_a_type_with_another_detector_still_answering_is_not_unchecked():
    world = World(detector_votes={"route-corridor": "down"})   # route-progress still checks off_route
    _, state = track(world, accuracy=10)
    assert world.reports == [] and state["decisions"][0]["unchecked"] == []
