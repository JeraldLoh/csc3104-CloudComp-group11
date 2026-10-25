"""Live or saved data, switchable while the service runs (local demos only).

    mode = DataMode(settings, redis_client)
    mode.get("onemap") / mode.get("lta")      -> "live" | "recorded"
    mode.set(onemap="live", lta="live")        switch (both optional)

ONEMAP_MODE / LTA_MODE give the starting values. A switch is kept in the Redis hash `journey:data-mode`, so every
replica and app/demo_tools.py follow it. It is only honoured with DEMO_UI=true, so a deployment on AWS (DEMO_UI off)
always uses what its environment says. Standard library + a redis client only, so the Lambda can import it.
"""
import logging
import time

from .config import Settings

log = logging.getLogger("journey.data_mode")

KEY = "journey:data-mode"
MODES = ("live", "recorded")
APIS = ("onemap", "lta")
READ_EVERY_S = 1.0                  # how long a read of the switch is reused


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else value


class DataMode:
    def __init__(self, settings: Settings, redis_client=None):
        self._defaults = {"onemap": settings.onemap_mode, "lta": settings.lta_mode}
        self._r = redis_client if settings.demo_ui else None        # without DEMO_UI the switch doesn't exist
        self._seen: dict[str, str] = {}
        self._seen_until = 0.0

    @property
    def switchable(self) -> bool:
        return self._r is not None

    def get(self, api: str) -> str:
        return self._current().get(api) or self._defaults[api]

    def all(self) -> dict[str, str]:
        return {api: self.get(api) for api in APIS}

    def set(self, onemap: str | None = None, lta: str | None = None) -> dict[str, str]:
        """Switches the given APIs (the others keep their mode) and returns the modes now in use."""
        if not self.switchable:
            raise RuntimeError("the data mode can only be switched with DEMO_UI=true and Redis")
        changes = {api: value for api, value in (("onemap", onemap), ("lta", lta)) if value is not None}
        for api, value in changes.items():
            if value not in MODES:
                raise ValueError(f"{api} mode must be one of {MODES}, not {value!r}")
        if changes:
            self._r.hset(KEY, mapping=changes)
            self._seen_until = 0.0                                   # read it back now, not in a second
        return self.all()

    def _current(self) -> dict[str, str]:
        if self._r is None:
            return {}
        if time.monotonic() >= self._seen_until:
            try:
                raw = self._r.hgetall(KEY)
                self._seen = {_text(k): _text(v) for k, v in raw.items() if _text(v) in MODES}
            except Exception as e:  # noqa: BLE001 - Redis down: keep the last known switch, else the defaults
                log.warning("data mode read failed: %s", type(e).__name__)
            self._seen_until = time.monotonic() + READ_EVERY_S
        return self._seen
