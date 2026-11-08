"""Fake detector: stands in for Member 4's detectors until they're ready.

Same endpoint and message formats as a real detector (contracts/api.md):
    POST /internal/detect   detector-request.json -> detector-vote.json

but no real detection. What it answers is set by MODE and can be changed while it runs
(POST /control), so a demo can make a detector misbehave live:

    normal    always "normal"
    problem   always "problem" (PROBLEM_TYPE)
    auto      "problem" if the check-in is outside AUTO_BOX, else "normal"
    wrong     the opposite of auto: a faulty detector (Member 4's fault mode)
    abstain   always "abstain"
    error     HTTP 500 (the Coordinator records an abstain)

DELAY_MS makes every answer slow (e.g. 2000 -> the Coordinator's 500 ms timeout fires).
Run one copy per detector, each with its own DETECTOR_ID.

Settings live in this process's memory: fine for a test double, which runs as one replica.
"""
import asyncio
import os
import time
from dataclasses import asdict, dataclass, replace
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

Mode = Literal["normal", "problem", "auto", "wrong", "abstain", "error"]
# Around the contract example check-in (1.4052, 103.9024): it's "on route".
DEFAULT_BOX = "1.40,103.89,1.41,103.91"


@dataclass(frozen=True)
class Behaviour:
    detector_id: str = "route-corridor"
    mode: str = "auto"
    problem_type: str = "off_route"
    confidence: float = 0.8
    delay_ms: int = 0
    box: tuple[float, float, float, float] = (1.40, 103.89, 1.41, 103.91)  # minLat, minLng, maxLat, maxLng


def parse_box(spec: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in spec.split(",")]
    if len(parts) != 4 or parts[0] > parts[2] or parts[1] > parts[3]:
        raise ValueError(f"AUTO_BOX must be minLat,minLng,maxLat,maxLng, got {spec!r}")
    return tuple(parts)  # type: ignore[return-value]


def from_env() -> Behaviour:
    return Behaviour(
        detector_id=os.getenv("DETECTOR_ID", "route-corridor"),
        mode=os.getenv("MODE", "auto"),
        problem_type=os.getenv("PROBLEM_TYPE", "off_route"),
        confidence=float(os.getenv("CONFIDENCE", "0.8")),
        delay_ms=int(os.getenv("DELAY_MS", "0")),
        box=parse_box(os.getenv("AUTO_BOX", DEFAULT_BOX)),
    )


def inside(box: tuple[float, float, float, float], lat, lng) -> bool:
    try:
        return box[0] <= float(lat) <= box[2] and box[1] <= float(lng) <= box[3]
    except (TypeError, ValueError):
        return True   # no usable position: don't invent a problem


def verdict_for(b: Behaviour, checkin: dict) -> tuple[str, str]:
    """(verdict, reason) for modes that answer with a vote."""
    if b.mode in ("normal", "problem", "abstain"):
        return b.mode, f"fixed mode {b.mode}"
    on_route = inside(b.box, checkin.get("lat"), checkin.get("lng"))
    if b.mode == "wrong":
        on_route = not on_route
    return ("normal", "inside the demo area") if on_route else ("problem", "outside the demo area")


class Control(BaseModel):
    """Partial update for POST /control; fields left out keep their value."""
    mode: Mode | None = None
    problemType: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    delayMs: int | None = Field(default=None, ge=0)


def create_app(behaviour: Behaviour | None = None) -> FastAPI:
    app = FastAPI(title="Fake detector")
    app.state.behaviour = behaviour or from_env()

    @app.post("/internal/detect")
    async def detect(request: dict):
        b: Behaviour = app.state.behaviour
        start = time.perf_counter()
        if b.delay_ms:
            await asyncio.sleep(b.delay_ms / 1000)
        if b.mode == "error":
            return JSONResponse(status_code=500, content={"error": {"code": "fake_error", "message": "MODE=error"}})

        verdict, reason = verdict_for(b, request.get("checkin") or {})
        return {
            "requestId": request.get("requestId"),
            "detectorId": b.detector_id,
            "verdict": verdict,
            "problemType": b.problem_type if verdict == "problem" else None,
            "confidence": 0.0 if verdict == "abstain" else b.confidence,
            "reason": f"fake: {reason}",
            "latencyMs": int((time.perf_counter() - start) * 1000),
        }

    @app.get("/control")
    def get_control():
        return asdict(app.state.behaviour)

    @app.post("/control")
    def set_control(c: Control):
        changes = {"mode": c.mode, "problem_type": c.problemType, "confidence": c.confidence, "delay_ms": c.delayMs}
        app.state.behaviour = replace(app.state.behaviour, **{k: v for k, v in changes.items() if v is not None})
        return asdict(app.state.behaviour)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
