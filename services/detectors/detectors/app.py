import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Callable, Dict, Optional

import redis as redis_lib
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from detectors import dwell_time, heading_speed, route_corridor, route_progress
from detectors.config import Settings, get_settings
from detectors.context import TripContext, load_context
from detectors.models import DetectorRequest, DetectorVote

log = logging.getLogger("detectors")

# every detector has the same signature
Detect = Callable[[DetectorRequest, TripContext, Settings], DetectorVote]

# DETECTOR_ID picks one of these
REGISTRY: Dict[str, Detect] = {
    route_corridor.DETECTOR_ID: route_corridor.detect,
    heading_speed.DETECTOR_ID: heading_speed.detect,
    dwell_time.DETECTOR_ID: dwell_time.detect,
    route_progress.DETECTOR_ID: route_progress.detect
}


# one json log line per vote
def _log(**fields) -> None:
    log.info(json.dumps(fields))


# tests pass settings and a fake redis
def create_app(settings: Optional[Settings] = None, redis_client=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = settings or get_settings()
        # fail fast on an unknown detector
        if cfg.detector_id not in REGISTRY:
            raise RuntimeError(f"DETECTOR_ID must be one of {sorted(REGISTRY)}, got {cfg.detector_id!r}")
        app.state.settings = cfg
        app.state.detect = REGISTRY[cfg.detector_id]
        # short timeouts since the coordinator treats a slow detector as abstain
        app.state.redis = redis_client or redis_lib.Redis.from_url(
            cfg.redis_url, socket_connect_timeout=1, socket_timeout=1
        )
        yield

    app = FastAPI(title="CareRoute detector", lifespan=lifespan)

    # judge one check-in and stamp detector id and latency
    @app.post("/internal/detect", response_model=DetectorVote, response_model_by_alias=True)
    def detect(body: DetectorRequest, request: Request) -> DetectorVote:
        start = time.perf_counter()
        cfg: Settings = request.app.state.settings
        try:
            ctx = load_context(request.app.state.redis, body)
            vote = request.app.state.detect(body, ctx, cfg)
        except Exception as exc:  # any failure becomes abstain never a 500
            log.exception("detection failed")
            vote = DetectorVote.abstain(body.request_id, cfg.detector_id, f"internal error: {type(exc).__name__}")
        # stamp detector id and latency on every vote
        vote = vote.model_copy(
            update={"detector_id": cfg.detector_id, "latency_ms": round((time.perf_counter() - start) * 1000, 2)}
        )
        _log(requestId=vote.request_id, detectorId=vote.detector_id, verdict=vote.verdict, latencyMs=vote.latency_ms)
        return vote

    # liveness does not touch redis
    @app.get("/health")
    def health():
        return {"status": "ok"}

    # readiness needs redis
    @app.get("/ready")
    def ready(request: Request):
        try:
            request.app.state.redis.ping()
        except Exception as exc:
            return JSONResponse({"status": "unavailable", "reason": type(exc).__name__}, status_code=503)
        return {"status": "ok"}

    return app


app = create_app()
