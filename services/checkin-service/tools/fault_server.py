"""Run the Check-in Service with controlled faults, for the evaluation (evaluation/run.py).

The real service is built exactly as in production (app.main.build_service); only the
SQS step and the request time are wrapped, so the production code has no test hooks.

  FAULT_SEND_FAIL_RATE   probability (0-1) that step 4 (SQS send) fails *after* the
                         check-in was stored: the "server crashed part-way" case that
                         the phone's retry and the clean-up task must repair
  FAULT_LATENCY_MS       extra processing time per check-in, to give one replica a
                         fixed, known capacity (so overload experiments don't depend
                         on how fast the laptop is)
  FAULT_SEED             random seed, for repeatable runs
  PORT                   port to listen on (default 8000)

All normal settings (REDIS_URL, AWS_ENDPOINT_URL, MAX_IN_FLIGHT, RECONCILE_*, ...) apply.
Usage:  python -m tools.fault_server      (from services/checkin-service)
"""
import os
import random
import time

import uvicorn

from app.config import settings
from app.main import build_service, create_app


class FaultySend:
    def __init__(self, real, fail_rate: float, rng: random.Random):
        self.real, self.fail_rate, self.rng = real, fail_rate, rng

    def __call__(self, checkin):
        if self.rng.random() < self.fail_rate:
            raise ConnectionError("injected fault: SQS send failed after the check-in was stored")
        return self.real(checkin)


def apply_faults(service, fail_rate: float, latency_s: float, rng: random.Random):
    """Wrap a real CheckinService. Tested in tests/test_fault_server.py so the wrappers
    can't silently drift from the service's method signatures."""
    if fail_rate > 0:
        service.queue.send = FaultySend(service.queue.send, fail_rate, rng)
    if latency_s > 0:
        real_ingest = service.ingest

        def slow_ingest(*args, **kwargs):
            time.sleep(latency_s)
            return real_ingest(*args, **kwargs)

        service.ingest = slow_ingest
    return service


def main() -> None:
    rng = random.Random(int(os.getenv("FAULT_SEED", "1")))
    fail_rate = float(os.getenv("FAULT_SEND_FAIL_RATE", "0"))
    latency_s = float(os.getenv("FAULT_LATENCY_MS", "0")) / 1000
    service = apply_faults(build_service(settings), fail_rate, latency_s, rng)
    app = create_app(service=service, s=settings)
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")), log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
