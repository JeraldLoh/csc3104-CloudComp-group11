import pytest

from app.risk import ELEVATED, NORMAL, Risk, RiskPolicy, detectors_failed, signal, update_risk
from app.voting import Decision

PROBLEM = Decision("problem", "off_route", 0.9, 0.9, 2)
NORMAL_CLEAR = Decision("normal", None, 1.0, 0.0, 2)
NORMAL_SHAKY = Decision("normal", None, 0.65, 0.35, 4)
NEEDS_MORE = Decision("inconclusive", None, 0.0, 0.5, 2, "split", needs_more_votes=True)
FINAL_INCONCLUSIVE = Decision("inconclusive", None, 0.0, None, 1, "quorum", needs_more_votes=False)


def run(decisions, start=Risk(), policy=RiskPolicy()) -> list[Risk]:
    """Apply decisions in order; return the risk after each one."""
    out, r = [], start
    for d in decisions:
        r = update_risk(r, d, policy)
        out.append(r)
    return out


# --- signal ---------------------------------------------------------------------

def test_signals():
    assert signal(PROBLEM) == 1.0
    assert signal(NORMAL_CLEAR) == 0.0
    assert signal(NORMAL_SHAKY) == pytest.approx(0.35)
    assert signal(FINAL_INCONCLUSIVE) == 1.0


# --- going up ----------------------------------------------------------------------

def test_new_traveller_starts_normal():
    assert Risk() == Risk(0.0, NORMAL, 0)


def test_one_problem_escalates_immediately():
    r = update_risk(Risk(), PROBLEM)
    assert r.score == pytest.approx(0.5)
    assert r.level == ELEVATED
    assert r.calm_streak == 0


def test_normals_keep_a_calm_traveller_normal():
    assert all(r.level == NORMAL and r.score == 0 for r in run([NORMAL_CLEAR] * 5))


def test_shaky_normals_slowly_build_up_risk():
    # Every "normal" says 0.35 problem: score 0.175, 0.2625, 0.306..., never reaches 0.4.
    risks = run([NORMAL_SHAKY] * 10)
    assert risks[0].score == pytest.approx(0.175)
    assert all(r.level == NORMAL for r in risks)


def test_final_inconclusive_escalates():
    # All four detectors were asked and still too few voted: we're blind, so look harder.
    assert update_risk(Risk(), FINAL_INCONCLUSIVE).level == ELEVATED


def test_inconclusive_that_needs_more_votes_changes_nothing():
    prev = Risk(0.3, NORMAL, 2)
    assert update_risk(prev, NEEDS_MORE) is prev


# --- coming down (hysteresis) ----------------------------------------------------------

def test_stays_elevated_until_three_calm_checkins():
    risks = run([PROBLEM, NORMAL_CLEAR, NORMAL_CLEAR, NORMAL_CLEAR, NORMAL_CLEAR])
    # scores: 0.5, 0.25, 0.125, 0.0625, 0.03125
    assert [r.level for r in risks] == [ELEVATED, ELEVATED, ELEVATED, NORMAL, NORMAL]


def test_low_score_alone_does_not_deescalate():
    # Score already tiny, but only 1 calm check-in so far.
    r = update_risk(Risk(0.01, ELEVATED, 0), NORMAL_CLEAR)
    assert r.score < 0.15
    assert r.level == ELEVATED


def test_calm_streak_alone_does_not_deescalate():
    # Many calm check-ins, but the score is still high.
    r = update_risk(Risk(0.9, ELEVATED, 10), NORMAL_CLEAR)
    assert r.level == ELEVATED


def test_a_problem_resets_the_calm_streak():
    risks = run([PROBLEM, NORMAL_CLEAR, NORMAL_CLEAR, PROBLEM, NORMAL_CLEAR, NORMAL_CLEAR])
    assert risks[2].calm_streak == 2
    assert risks[3].calm_streak == 0
    assert risks[-1].level == ELEVATED   # only 2 calm since the last problem


def test_no_flip_flop_on_alternating_results():
    risks = run([PROBLEM, NORMAL_CLEAR] * 5)
    assert all(r.level == ELEVATED for r in risks)


def test_zero_calm_checkins_deescalates_on_score_alone():
    policy = RiskPolicy(calm_checkins=0)
    assert update_risk(Risk(0.2, ELEVATED, 0), NORMAL_CLEAR, policy).level == NORMAL   # 0.1 < 0.15


# --- bounds and policy -------------------------------------------------------------------

def test_score_never_exceeds_one():
    assert run([PROBLEM] * 20)[-1].score <= 1.0


def test_alpha_one_follows_the_last_signal():
    policy = RiskPolicy(alpha=1.0)
    assert update_risk(Risk(0.9, ELEVATED, 0), NORMAL_SHAKY, policy).score == pytest.approx(0.35)


@pytest.mark.parametrize("kwargs", [
    {"alpha": 0},
    {"alpha": 1.5},
    {"escalate_at": 0.2, "deescalate_below": 0.3},
    {"escalate_at": 0.3, "deescalate_below": 0.3},
    {"calm_checkins": -1},
])
def test_invalid_policies_are_rejected(kwargs):
    with pytest.raises(ValueError):
        RiskPolicy(**kwargs)


# --- Redis hash round trip -------------------------------------------------------------------

def test_hash_round_trip():
    r = Risk(0.123456, ELEVATED, 2)
    h = r.to_hash()
    assert h == {"score": "0.1235", "level": "elevated", "calmStreak": "2"}
    assert Risk.from_hash(h) == Risk(0.1235, ELEVATED, 2)


def test_missing_hash_is_a_new_traveller():
    assert Risk.from_hash(None) == Risk()
    assert Risk.from_hash({}) == Risk()


def test_bad_hash_values_fall_back_safely():
    assert Risk.from_hash({"score": "abc", "level": "panic", "calmStreak": "x"}) == Risk()


def test_out_of_range_score_in_hash_is_clamped():
    assert Risk.from_hash({"score": "7"}).score == 1.0
    assert Risk.from_hash({"score": "-1"}).score == 0.0


def test_hash_from_an_older_writer_without_calm_streak():
    assert Risk.from_hash({"score": "0.5", "level": "elevated"}) == Risk(0.5, ELEVATED, 0)



# --- inconclusive: failed detectors vs "can't judge yet" ------------------------------------------

def test_inconclusive_without_failures_changes_nothing():
    prev = Risk(0.2, NORMAL, 2)
    assert update_risk(prev, FINAL_INCONCLUSIVE, failed=False) is prev


def test_inconclusive_with_failures_escalates():
    assert update_risk(Risk(), FINAL_INCONCLUSIVE, failed=True).level == ELEVATED


def test_failed_flag_only_matters_for_inconclusive():
    assert update_risk(Risk(), PROBLEM, failed=False).level == ELEVATED
    assert update_risk(Risk(), NORMAL_CLEAR, failed=True).level == NORMAL


def abstain(reason):
    return {"detectorId": "route-corridor", "verdict": "abstain", "confidence": 0.0, "reason": reason}


@pytest.mark.parametrize("reason", ["timeout", "error: HTTP 500", "error: ConnectError", "internal error: KeyError"])
def test_failures_are_recognised(reason):
    assert detectors_failed([abstain(reason)])


@pytest.mark.parametrize("reason", ["only 1 usable check-in(s), need 3", "no route in Redis for this trip",
                                    "GPS accuracy 80 m exceeds 50 m limit", "fake: fixed mode abstain", "", None])
def test_cant_judge_yet_is_not_a_failure(reason):
    assert not detectors_failed([abstain(reason)])


def test_only_abstains_count_as_failures():
    vote = {"detectorId": "route-corridor", "verdict": "normal", "confidence": 0.9, "reason": "timeout-ish"}
    assert not detectors_failed([vote])
    assert detectors_failed([vote, abstain("timeout")])
