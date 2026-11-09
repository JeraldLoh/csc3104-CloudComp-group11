"""The detectors: which exist, where they are, and calling them:
POST http://detector-<name>:8000/internal/detect (contracts/api.md).

All chosen detectors are called at the same time, so a decision waits for the slowest
one, not the sum. Each call has an overall deadline (500 ms by contract): httpx's own
timeout applies per phase (connect, read, ...), so a detector that trickles its answer
could otherwise take much longer.

The deadline only stops the waiting: the request itself is never cancelled. Cancelling an
httpx request part-way leaves its pooled connection marked in use for good (httpcore 1.0), so
a burst of slow detectors used to use up the pool, after which every call and every alert
failed until the Coordinator restarted. A late request instead runs on in the background
until its own httpx timeout ends it, and its connection goes back to the pool.

A detector that is slow, down, errors or answers garbage becomes an "abstain" vote.
Nothing here raises: one bad detector can never block or crash the pipeline.
"""
import asyncio
import logging
import time

import httpx

log = logging.getLogger("coordinator.detectors")

VERDICTS = {"problem", "normal", "abstain"}

# detectorId (contracts/examples/detector-vote.json) -> service name (contracts/api.md)
DETECTOR_SERVICES = {
    "route-corridor": "detector-route",
    "heading-speed": "detector-heading",
    "dwell-time": "detector-dwell",
    "route-progress": "detector-progress",
}
ALL_DETECTORS = tuple(DETECTOR_SERVICES)


def parse_urls(spec: str) -> dict[str, str]:
    """DETECTOR_URLS="route-corridor=http://a:8000,dwell-time=http://b:8000" -> dict.
    Detectors not listed use their in-cluster service name."""
    urls = {d: f"http://{svc}:8000" for d, svc in DETECTOR_SERVICES.items()}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        name, _, url = part.partition("=")
        if name.strip() not in DETECTOR_SERVICES or not url.strip():
            raise ValueError(f"bad DETECTOR_URLS entry {part!r}")
        urls[name.strip()] = url.strip().rstrip("/")
    return urls


def abstain(request_id: str, detector_id: str, reason: str, latency_ms: int) -> dict:
    """detector-vote.json for a detector that didn't give a usable answer."""
    return {"requestId": request_id, "detectorId": detector_id, "verdict": "abstain",
            "problemType": None, "confidence": 0.0, "reason": reason, "latencyMs": latency_ms}


def _ignore_late(task: asyncio.Task) -> None:
    """A request answered or failed after its vote was already an abstain: nothing to do."""
    if not task.cancelled():
        task.exception()   # retrieved, so asyncio doesn't log "exception was never retrieved"


def _reason(e: Exception) -> str:
    if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(e, httpx.HTTPStatusError):
        return f"error: HTTP {e.response.status_code}"
    return f"error: {type(e).__name__}"


class DetectorClient:
    def __init__(self, http: httpx.AsyncClient, urls: dict[str, str], timeout_s: float = 0.5):
        self.http = http
        self.urls = urls
        self.timeout_s = timeout_s

    async def _post(self, detector_id: str, request: dict) -> dict:
        resp = await self.http.post(f"{self.urls[detector_id]}/internal/detect", json=request,
                                    timeout=self.timeout_s)
        resp.raise_for_status()
        vote = resp.json()
        if not isinstance(vote, dict) or vote.get("verdict") not in VERDICTS:
            raise ValueError("invalid vote")
        return vote

    async def call(self, detector_id: str, request: dict) -> dict:
        request_id = request["requestId"]
        start = time.perf_counter()
        task = asyncio.ensure_future(self._post(detector_id, request))
        task.add_done_callback(_ignore_late)
        try:
            # Wait up to the deadline without cancelling the request (see the top of this file).
            done, _ = await asyncio.wait({task}, timeout=self.timeout_s)
            if not done:
                raise asyncio.TimeoutError
            vote = task.result()
        except Exception as e:  #any failure is an abstain
            ms = int((time.perf_counter() - start) * 1000)
            reason = _reason(e)
            log.warning("detector %s abstained for %s (%s, %d ms)", detector_id, request_id, reason, ms)
            return abstain(request_id, detector_id, reason, ms)
        # Our own record of who was asked about what, and the round trip as we saw it.
        vote.update(requestId=request_id, detectorId=detector_id,
                    latencyMs=int((time.perf_counter() - start) * 1000))
        return vote

    async def call_many(self, detector_ids: list[str], request: dict) -> list[dict]:
        """Votes in the same order as detector_ids."""
        return list(await asyncio.gather(*(self.call(d, request) for d in detector_ids)))
