"""CareRoute Easy-read Service (Member 6): AI rewording of route steps for travellers who need Easy Read.

POST /internal/easyread   steps -> simpler wording per step (cluster-only; called by journey-service)
GET  /health              liveness: the process is up
GET  /ready               readiness: Ollama answers and the model is pulled

journey-service sends each step's own template wording (`text`) and the facts that must survive (`keep`), never
names, addresses or labels like "Home" (those arrive as [place N] tokens). Steps may also carry `spots`: landmarks
along them (names, kinds, sides, distances; never coordinates) that the model looks up and may mention, and the
ids of those it used come back in `landmarks` (ollama_client.py). Each AI step is checked
(hallucination_check.py); a step that fails, or every step when Ollama is down, gets its template text back with
`fallback: true`. So this endpoint always answers 200 with usable wording, and `provider` says who wrote it.
"""
import logging
import time
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, settings as default_settings
from .hallucination_check import checked
from .ollama_client import OllamaClient, OllamaUnavailable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("easyread")


class LandmarkIn(BaseModel):
    id: str = Field(pattern=r"^L\d{1,4}$")
    name: str = Field(min_length=1, max_length=100)
    kind: str | None = Field(default=None, max_length=40)
    metres: int | None = Field(default=None, ge=0, le=1000)
    side: Literal["left", "right"] | None = None


class StepIn(BaseModel):
    stepIndex: int = Field(ge=0)
    mode: str | None = Field(default=None, max_length=20)
    text: str = Field(min_length=1, max_length=1000)       # the template wording, also the fallback
    keep: list[str] = Field(default_factory=list, max_length=20)
    spots: dict[Literal["start", "middle", "end", "before_stop"], list[LandmarkIn]] = Field(default_factory=dict)


class EasyReadIn(BaseModel):
    steps: list[StepIn] = Field(min_length=1, max_length=50)


def error(status: int, code: str, message: str, details=None) -> JSONResponse:
    """Contract error format: {"error": {"code": ..., "message": ...}}"""
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


def create_app(ollama: OllamaClient | None = None, s: Settings = default_settings) -> FastAPI:
    app = FastAPI(title="CareRoute Easy-read Service")
    app.state.ollama = ollama or OllamaClient(s)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in d["loc"][1:]), "problem": d["msg"]} for d in e.errors()]
        return error(422, "invalid_request", "The request doesn't match the contract", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException):
        return error(e.status_code, "not_found" if e.status_code == 404 else "http_error", str(e.detail))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, e: Exception):
        log.exception("unhandled error: %s", e)
        return error(500, "internal_error", "Something went wrong")

    # Plain `def`: the Ollama call is synchronous and slow, so FastAPI runs it in its thread pool.
    @app.post("/internal/easyread")
    def easyread(body: EasyReadIn, request: Request):
        steps = [step.model_dump() for step in body.steps]
        start = time.perf_counter()
        try:
            ai, provider = request.app.state.ollama.rewrite(steps), "ollama"
        except OllamaUnavailable as e:
            log.warning("Ollama unavailable (%s): answering with the template wording", e)
            ai, provider = None, "template"
        results = checked(steps, ai, s.max_chars)
        for r in results:
            if provider == "ollama" and r["problems"]:
                log.info("step %d: AI text not used: %s", r["stepIndex"], "; ".join(r["problems"]))
        log.info("easyread: %d steps, provider=%s, %d fell back, %d landmarks, %.1fs", len(results), provider,
                 sum(r["fallback"] for r in results), sum(len(r["landmarks"]) for r in results),
                 time.perf_counter() - start)
        return {"steps": [{k: r[k] for k in ("stepIndex", "easyRead", "fallback", "landmarks")} for r in results],
                "provider": provider, "model": s.ollama_model if provider == "ollama" else None}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready(request: Request):
        if not request.app.state.ollama.ready():
            return error(503, "not_ready", f"ollama: not reachable or model {s.ollama_model} not pulled yet")
        return {"status": "ready"}

    return app


app = create_app()
