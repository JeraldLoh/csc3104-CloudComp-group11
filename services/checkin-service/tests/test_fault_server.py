"""The experiment server's fault wrappers must keep working with the real service."""
import random

import pytest

from tests.test_reconciler import checkin, env  # noqa: F401  (fixture)
from tools.fault_server import apply_faults


def test_latency_wrapper_passes_every_argument_through(env):
    apply_faults(env.service, fail_rate=0, latency_s=0.001, rng=random.Random(1))
    assert env.service.ingest_one(checkin(1)).body()["accepted"] == 1
    counts = env.service.ingest_batch([checkin(2), checkin(3)], budget_s=5)   # batch path uses live=False
    assert counts.body() == {"accepted": 2, "duplicates": 0, "processed": 2}


def test_send_failures_happen_after_the_checkin_is_stored(env):
    apply_faults(env.service, fail_rate=1.0, latency_s=0, rng=random.Random(1))
    with pytest.raises(ConnectionError):
        env.service.ingest(checkin(4))
    assert env.service.store.get("T-001", 4)["pending"] == "P"   # stored, not queued: the case to repair
