"""Calls detectors: POST http://detector-<name>:8000/internal/detect (contracts/api.md).

All chosen detectors are called at the same time, so a decision waits for the slowest
one, not the sum. Each call has an overall deadline (500 ms by contract): httpx's own
timeout applies per phase (connect, read, ...), so a detector that trickles its answer
could otherwise take much longer.

A detector that is slow, down, errors or answers garbage becomes an "abstain" vote.
Nothing here raises: one bad detector can never block or crash the pipeline.
"""
import asyncio
import logging
import time

import httpx

log = logging.getLogger("coordinator.detectors")

VERDICTS = {"problem", "normal", "abstain"}


def abstain(request_id: str, detector_id: str, reason: str, latency_ms: int) -> dict:
    """detector-vote.json for a detector that didn't give a usable answer."""
    return {"requestId": request_id, "detectorId": detector_id, "verdict": "abstain",
            "problemType": None, "confidence": 0.0, "reason": reason, "latencyMs": latency_ms}


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
        try:
            vote = await asyncio.wait_for(self._post(detector_id, request), self.timeout_s)
        except Exception as e:  # noqa: BLE001 - any failure is an abstain, by design
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
