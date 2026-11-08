"""Fault switch: sits in front of one real detector and can make it misbehave, for the evaluation (C2).

Same endpoint and message formats as a real detector (contracts/api.md):
    POST /internal/detect   detector-request.json -> detector-vote.json

The real detector (UPSTREAM_URL) does the detecting; the switch decides what reaches the
Coordinator. MODE can be changed while it runs (POST /control):

    pass      the real detector's vote, unchanged (healthy)
    wrong     the real vote inverted: problem <-> normal (abstains stay abstains); a normal
              becomes a problem of PROBLEM_TYPE
    error     HTTP 500 without asking the real detector (the Coordinator records an abstain)

DELAY_MS makes every answer slow (e.g. 2000 -> the Coordinator's 500 ms timeout fires).
Run one switch per detector (evaluation/coordinator/compose.c2.yaml).

Settings live in this process's memory: fine for a test tool, which runs as one replica.
"""
import asyncio
import os
import time
from dataclasses import asdict, dataclass, replace
from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

Mode = Literal["pass", "wrong", "error"]


@dataclass(frozen=True)
class Behaviour:
    upstream: str                 # the real detector, e.g. http://detector-route:8000
    mode: str = "pass"
    problem_type: str = "off_route"   # what a "wrong" normal turns into
    delay_ms: int = 0


def from_env() -> Behaviour:
    upstream = os.getenv("UPSTREAM_URL")
    if not upstream:
        raise RuntimeError("UPSTREAM_URL is required: the real detector this switch sits in front of")
    return Behaviour(
        upstream=upstream,
        mode=os.getenv("MODE", "pass"),
        problem_type=os.getenv("PROBLEM_TYPE", "off_route"),
        delay_ms=int(os.getenv("DELAY_MS", "0")),
    )


def invert(vote: dict, b: Behaviour) -> dict:
    """A faulty detector: the real verdict turned around. Abstains (and anything else) pass."""
    if vote.get("verdict") == "problem":
        return {**vote, "verdict": "normal", "problemType": None, "reason": "switch wrong: real vote was problem"}
    if vote.get("verdict") == "normal":
        return {**vote, "verdict": "problem", "problemType": b.problem_type, "reason": "switch wrong: real vote was normal"}
    return vote


class Control(BaseModel):
    """Partial update for POST /control; fields left out keep their value."""
    mode: Mode | None = None
    problemType: str | None = None
    delayMs: int | None = Field(default=None, ge=0)


def create_app(behaviour: Behaviour | None = None, upstream_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """upstream_transport is for tests: a fake real detector."""
    app = FastAPI(title="Fault switch")
    app.state.behaviour = behaviour or from_env()
    app.state.http = httpx.AsyncClient(transport=upstream_transport, timeout=5)

    @app.post("/internal/detect")
    async def detect(request: dict):
        b: Behaviour = app.state.behaviour
        start = time.perf_counter()
        if b.delay_ms:
            await asyncio.sleep(b.delay_ms / 1000)
        if b.mode == "error":
            return JSONResponse(status_code=500, content={"error": {"code": "switch_error", "message": "MODE=error"}})
        try:
            resp = await app.state.http.post(f"{b.upstream.rstrip('/')}/internal/detect", json=request)
            resp.raise_for_status()
        except httpx.HTTPError as e:   # the real detector is down: fail like it would
            return JSONResponse(status_code=502, content={"error": {"code": "upstream_error", "message": str(e)}})
        vote = resp.json()
        vote = invert(vote, b) if b.mode == "wrong" else vote
        return {**vote, "latencyMs": int((time.perf_counter() - start) * 1000)}

    @app.get("/control")
    def get_control():
        return asdict(app.state.behaviour)

    @app.post("/control")
    def set_control(c: Control):
        changes = {"mode": c.mode, "problem_type": c.problemType, "delay_ms": c.delayMs}
        app.state.behaviour = replace(app.state.behaviour, **{k: v for k, v in changes.items() if v is not None})
        return asdict(app.state.behaviour)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app
# No module-level app: UPSTREAM_URL is required, so uvicorn builds it on start (--factory, Dockerfile).
