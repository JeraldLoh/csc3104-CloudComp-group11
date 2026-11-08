"""φ (phi) accrual failure detector (Hayashibara et al., 2004), as used by Akka and Cassandra.

A fixed timeout ("alert after 60 s of silence") is either too slow for a phone that checks in like
clockwork or too jumpy for one on a patchy connection. φ instead learns each trip's own rhythm from the
gaps between its recent check-ins and asks: given that rhythm, how unlikely is a silence this long?

    φ = -log10( P(the next check-in arrives later than now) )

so φ = 1 means a 10 % chance the phone is just late, φ = 2 means 1 %, φ = 8 means 1 in 100 million.
The caller picks a threshold; above it the traveller is suspected unreachable.

The gaps are modelled as a normal distribution (mean and standard deviation of the history), with
Akka's two practical additions:
- a floor on the standard deviation, so a perfectly regular history doesn't make the very first
  slightly-late check-in look like a failure;
- an "acceptable pause" added to the mean: silence we know to expect (a train underground, the app
  in the background) shifts the whole curve right instead of being treated as abnormal.

P(later) uses Akka's logistic approximation of the normal tail, 1 / (1 + e^z) with
z = y(1.5976 + 0.070566 y²), y = (t - mean) / std (max error about 1.4e-4). Then φ = log10(1 + e^z),
computed so it never overflows however long the silence.
"""
import math
from dataclasses import dataclass
from statistics import fmean, pstdev

LN10 = math.log(10)


@dataclass(frozen=True)
class Rhythm:
    """What the detector believes about a trip's check-in gaps, in milliseconds."""
    mean_ms: float
    std_ms: float


def rhythm(intervals_ms: list[float], *, expected_interval_ms: float, min_samples: int,
           min_std_ms: float) -> Rhythm:
    """Mean and spread of the recent gaps.

    With fewer than `min_samples` gaps (a trip that has only just started) there's no history to learn
    from yet, so assume the app's normal rhythm: `expected_interval_ms` with a quarter of it as spread
    (Akka's bootstrap). The spread never goes below `min_std_ms`.
    """
    if len(intervals_ms) < min_samples:
        mean, std = expected_interval_ms, expected_interval_ms / 4
    else:
        mean, std = fmean(intervals_ms), pstdev(intervals_ms)
    return Rhythm(mean, max(std, min_std_ms))


def phi(elapsed_ms: float, r: Rhythm, acceptable_pause_ms: float = 0.0) -> float:
    """Suspicion level after `elapsed_ms` of silence. 0 right after a check-in, rising without limit."""
    y = (elapsed_ms - (r.mean_ms + acceptable_pause_ms)) / r.std_ms
    z = y * (1.5976 + 0.070566 * y * y)
    # log10(1 + e^z) written as a softplus so neither branch overflows or loses precision.
    return (max(z, 0.0) + math.log1p(math.exp(-abs(z)))) / LN10
