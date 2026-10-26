"""CareRoute Aggregator (Member 3).

POST /internal/votes   {"requestId", "checkin", "votes", "riskLevel"?, "escalated"?}
                       -> {"decision", "problemType", "confidence", "needsMoreVotes", "reason", "riskLevel"}
GET  /health           liveness: the process is up (contract endpoint)
GET  /ready            readiness: Redis is reachable

Dashboard (read-only, from what this service has written to Redis):
GET  /api/status/travellers          risk per traveller
GET  /api/decisions?travellerId=     recent decisions, newest first
GET  /dashboard                      the page (polls the two endpoints above)

Any failure answers 503 in the contract error format. The Coordinator then leaves the
check-in on the queue and it is retried, which service.py makes safe.
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import dashboard
from .config import Settings, settings as default_settings
from .service import AggregatorService

DASHBOARD_PAGE = Path(__file__).parent / "static" / "dashboard.html"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("aggregator")


class CheckinRef(BaseModel):
    """The check-in the votes are about; only these fields are required here."""
    model_config = ConfigDict(extra="allow")
    tripId: str = Field(min_length=1)
    travellerId: str = Field(min_length=1)


class VotesRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    requestId: str = Field(min_length=1)
    checkin: CheckinRef
    votes: list[dict]
    riskLevel: str | None = None
    escalated: bool = False
    situation: str | None = None   # why the Coordinator asked that pair ("walking", "riding", ...)


def error(status: int, code: str, message: str, details=None) -> JSONResponse:
    """Contract error format: {"error": {"code": ..., "message": ...}}"""
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


def create_app(service: AggregatorService | None = None, s: Settings = default_settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.service is not None:   # injected (tests)
            yield
            return
        redis = aioredis.from_url(s.redis_url, decode_responses=True)
        async with httpx.AsyncClient() as http:
            app.state.service = AggregatorService(redis, http, s)
            yield
        await redis.aclose()

    app = FastAPI(title="CareRoute Aggregator", lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, e: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in d["loc"][1:]), "problem": d["msg"]} for d in e.errors()]
        message = ("Body must be {requestId, checkin, votes}" if request.url.path == "/internal/votes"
                   else "Invalid query parameters")
        return error(422, "invalid_request", message, details)

    @app.exception_handler(httpx.HTTPError)
    async def _alert_down(_: Request, e: httpx.HTTPError):
        log.warning("Alert Service unavailable: %s", e)
        return error(503, "alert_unavailable", "Couldn't report the problem to the Alert Service. Please retry.")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, e: Exception):
        log.exception("aggregation failed: %s", e)
        return error(503, "unavailable", "Couldn't record the decision right now. Please retry.")

    @app.post("/internal/votes")
    async def votes(body: VotesRequest, request: Request):
        svc: AggregatorService = request.app.state.service
        return await svc.handle(body.requestId, body.checkin.model_dump(), body.votes, body.escalated,
                                situation=body.situation)

    @app.get("/api/status/travellers")
    async def status_travellers(request: Request):
        return await dashboard.traveller_status(request.app.state.service.redis)

    @app.get("/api/decisions")
    async def decisions(request: Request, travellerId: str | None = Query(default=None, min_length=1),
                        limit: int = Query(default=50, ge=1, le=200)):
        return await dashboard.decisions(request.app.state.service.redis, travellerId, limit)

    @app.get("/dashboard", include_in_schema=False)
    def dashboard_page():
        return FileResponse(DASHBOARD_PAGE, media_type="text/html")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request):
        try:
            await request.app.state.service.redis.ping()
        except Exception as e:  # noqa: BLE001 - report, don't crash the probe
            return error(503, "not_ready", f"redis: {type(e).__name__}")
        return {"status": "ready"}

    return app


app = create_app()
