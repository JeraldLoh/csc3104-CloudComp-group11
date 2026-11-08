"""The φ accrual function on its own: no Redis, no clock."""
import math

import pytest

from app.phi import Rhythm, phi, rhythm

TEN_S = Rhythm(mean_ms=10_000, std_ms=2_000)


def seconds_until(threshold: float, r: Rhythm, pause_ms: float = 0) -> float:
    """First whole tenth of a second at which φ reaches `threshold`."""
    t = 0
    while phi(t, r, pause_ms) < threshold:
        t += 100
    return t / 1000


def test_low_right_after_a_check_in():
    assert phi(0, TEN_S) < 0.01


def test_rises_steadily_with_the_silence():
    values = [phi(t, TEN_S) for t in range(0, 120_000, 1_000)]
    assert values == sorted(values)
    assert values[-1] > 8


def test_is_log10_2_exactly_at_the_expected_time():
    # At the mean, a normal distribution says "later" half the time: φ = -log10(0.5).
    assert phi(10_000, TEN_S) == pytest.approx(math.log10(2), abs=1e-9)


def test_matches_the_normal_distribution():
    # Logistic approximation of the normal tail: within 1.5e-4 of the exact probability.
    for y in (0.5, 1, 2, 3):
        exact_p_later = 0.5 * math.erfc(y / math.sqrt(2))
        approx_p_later = 10 ** -phi(10_000 + y * 2_000, TEN_S)
        assert approx_p_later == pytest.approx(exact_p_later, abs=1.5e-4)


def test_a_patchier_history_tolerates_longer_gaps():
    steady = Rhythm(10_000, 2_000)
    patchy = Rhythm(10_000, 8_000)
    assert phi(30_000, patchy) < phi(30_000, steady)
    assert seconds_until(8, patchy) > seconds_until(8, steady)


def test_acceptable_pause_shifts_the_curve_by_exactly_that_much():
    assert seconds_until(8, TEN_S, pause_ms=300_000) == pytest.approx(seconds_until(8, TEN_S) + 300, abs=0.1)


def test_never_overflows_however_long_the_silence():
    value = phi(24 * 3600 * 1000, Rhythm(10_000, 2_000))
    assert math.isfinite(value) and value > 1e6


def test_default_settings_alert_after_about_50_s_above_ground():
    # 10 s rhythm, std floor 2 s, base pause 30 s, threshold 8 (config.py).
    assert seconds_until(8, rhythm([10_000] * 20, expected_interval_ms=10_000, min_samples=5, min_std_ms=2_000),
                         pause_ms=30_000) == pytest.approx(50.4, abs=0.5)


class TestRhythm:
    def test_learns_mean_and_spread_from_the_history(self):
        r = rhythm([8_000, 10_000, 12_000, 10_000], expected_interval_ms=10_000, min_samples=3, min_std_ms=100)
        assert r.mean_ms == 10_000
        assert r.std_ms == pytest.approx(math.sqrt(2_000_000))

    def test_assumes_the_app_rhythm_until_there_is_enough_history(self):
        r = rhythm([60_000], expected_interval_ms=10_000, min_samples=5, min_std_ms=100)
        assert r == Rhythm(10_000, 2_500)

    def test_spread_never_goes_below_the_floor(self):
        r = rhythm([10_000] * 50, expected_interval_ms=10_000, min_samples=5, min_std_ms=2_000)
        assert r.std_ms == 2_000
