"""Fake Alert Service: stands in for Member 5's Alert Service until it's ready.

Same endpoint and formats as the real one (contracts/api.md):
    POST /internal/problems          problem-report.json -> {"incidentId", "created"}
    GET  /api/incidents?travellerId  incident.json list (newest first)

One incident per problem: the id is travellerId#tripId#problemType#stepIndex, so a repeat
report (an SQS redelivery, or two replicas) updates the same incident and answers
created = false. Each new incident is logged as ALERT, so it shows up in the logs.

No caregiver notifications, escalation or DynamoDB: that's the real service's job.
Incidents live in this process's memory: fine for a test double, which runs as one replica.

For fault-tolerance tests, POST /control makes it fail (HTTP 503) or slow down, and
POST /control/reset clears all incidents between demo runs.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("fake-alert")

REQUIRED = ("travellerId", "tripId", "problemType")


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def incident_id(report: dict) -> str:
    """contracts/dynamo-db-tables.md: travellerId#tripId#problemType#stepIndex (unknown step -> empty)."""
    step = report.get("stepIndex")
    return "#".join([report["travellerId"], report["tripId"], report["problemType"], "" if step is None else str(step)])


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class Control(BaseModel):
    mode: Literal["ok", "error"] | None = None
    delayMs: int | None = Field(default=None, ge=0)


def create_app(mode: str | None = None, delay_ms: int | None = None) -> FastAPI:
    app = FastAPI(title="Fake Alert Service")
    app.state.mode = mode or os.getenv("MODE", "ok")
    app.state.delay_ms = int(os.getenv("DELAY_MS", "0")) if delay_ms is None else delay_ms
    app.state.incidents = {}   # incidentId -> incident.json

    @app.post("/internal/problems")
    async def problems(report: dict):
        if app.state.delay_ms:
            await asyncio.sleep(app.state.delay_ms / 1000)
        if app.state.mode == "error":
            return error(503, "unavailable", "fake Alert Service in error mode")
        missing = [f for f in REQUIRED if not report.get(f)]
        if missing:
            return error(422, "invalid_request", f"missing {', '.join(missing)}")

        iid = incident_id(report)
        now = iso_z(datetime.now(timezone.utc))
        existing = app.state.incidents.get(iid)
        if existing:
            existing.update(
                detectionCount=existing["detectionCount"] + 1,
                confidence=report.get("confidence"),
                lastLocation=report.get("location"),
                batteryPct=report.get("batteryPct"),
                updatedAt=now,
            )
            log.info("repeat report for %s (seen %d times)", iid, existing["detectionCount"])
            return {"incidentId": iid, "created": False}

        app.state.incidents[iid] = {
            "incidentId": iid,
            "travellerId": report["travellerId"],
            "tripId": report["tripId"],
            "stepIndex": report.get("stepIndex"),
            "problemType": report["problemType"],
            "severity": "normal",
            "state": "open",
            "escalationLevel": 0,
            "detectionCount": 1,
            "confidence": report.get("confidence"),
            "lastLocation": report.get("location"),
            "batteryPct": report.get("batteryPct"),
            "notifiedChannels": [],
            "acknowledgedBy": None,
            "createdAt": now,
            "updatedAt": now,
        }
        log.warning("ALERT %s from %s (confidence %s, %s detectors)", iid, report.get("source"),
                    report.get("confidence"), report.get("detectorsUsed"))
        return {"incidentId": iid, "created": True}

    @app.get("/api/incidents")
    def incidents(travellerId: str | None = None):
        found = [i for i in app.state.incidents.values() if travellerId in (None, i["travellerId"])]
        return sorted(found, key=lambda i: i["createdAt"], reverse=True)

    @app.get("/control")
    def get_control():
        return {"mode": app.state.mode, "delayMs": app.state.delay_ms, "incidents": len(app.state.incidents)}

    @app.post("/control")
    def set_control(c: Control):
        if c.mode is not None:
            app.state.mode = c.mode
        if c.delayMs is not None:
            app.state.delay_ms = c.delayMs
        return get_control()

    @app.post("/control/reset")
    def reset():
        app.state.incidents.clear()
        return get_control()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
