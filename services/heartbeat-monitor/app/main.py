"""CareRoute Heartbeat Monitor (Member 1, issue 1.4).

No API apart from health checks (contracts/api.md). A background loop checks every active trip every
TICK_S seconds and sends problem-report-no-response.json to the Alert Service when a traveller's
check-ins go silent for longer than their usual rhythm allows (monitor.py, phi.py).

GET /health   liveness: the process is up (contract endpoint)
GET /ready    readiness: Redis is reachable
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .alerts import AlertClient
from .config import Settings, settings as default_settings
from .lease import LeaderLease, replica_id
from .monitor import HeartbeatMonitor

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("heartbeat")


class MonitorLoop:
    """Runs HeartbeatMonitor.tick every `interval_s`, but only while this replica holds the lease."""

    def __init__(self, monitor: HeartbeatMonitor, lease: LeaderLease, interval_s: float):
        self.monitor = monitor
        self.lease = lease
        self.interval_s = interval_s
        self.is_leader = False
        self._task: asyncio.Task | None = None

    def tick(self) -> list[dict]:
        """Take/renew the lease; if leader, check the trips. Blocking."""
        try:
            leader = self.lease.acquire_or_renew()
        except redis.RedisError as e:
            # Can't tell who leads, and checking needs Redis anyway: skip this round.
            log.warning("lease check failed: %s", e)
            self.is_leader = False
            return []
        if leader != self.is_leader:
            log.info("this replica is %s the leader", "now" if leader else "no longer")
        self.is_leader = leader
        if not leader:
            return []
        try:
            return self.monitor.tick()
        except Exception as e:  # noqa: BLE001 - a bad round must not kill the loop
            log.warning("heartbeat round failed (will retry next round): %s", e)
            return []

    async def run(self) -> None:
        while True:
            await run_in_threadpool(self.tick)
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="heartbeat-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        try:
            await run_in_threadpool(self.lease.release)  # hand over straight away
        except redis.RedisError:
            pass  # it will expire on its own


def create_app(client: redis.Redis | None = None, alerts: AlertClient | None = None,
               s: Settings = default_settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        r = client or redis.Redis.from_url(s.redis_url, decode_responses=True)
        a = alerts or AlertClient(s.alert_service_url, s.alert_timeout_s)
        app.state.redis = r
        app.state.loop = None
        if s.monitor_enabled:
            loop = MonitorLoop(HeartbeatMonitor(r, a, s), LeaderLease(r, replica_id(), s.leader_lease_ms), s.tick_s)
            loop.start()
            app.state.loop = loop
        yield
        if app.state.loop:
            await app.state.loop.stop()
        a.close()

    app = FastAPI(title="CareRoute Heartbeat Monitor", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/ready")
    def ready():
        try:
            app.state.redis.ping()
        except Exception as e:  # noqa: BLE001
            return JSONResponse(status_code=503, content={
                "error": {"code": "not_ready", "message": f"redis: {type(e).__name__}"}})
        return {"status": "ready"}

    return app


app = create_app()
