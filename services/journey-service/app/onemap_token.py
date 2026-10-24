"""Keeps a valid OneMap access token without anyone pasting one in.

OneMap tokens last 3 days. If ONEMAP_API_EMAIL and ONEMAP_API_PASSWORD (the OneMap account) are set, this logs in
(POST /api/auth/post/getToken -> {access_token, expiry_timestamp}), remembers the token until shortly before it
expires, and logs in again when it has, or when OneMap rejects it. Redis (when there is one) holds it so every replica
shares one token instead of each logging in. Without an account the caller falls back to the static ONEMAP_API_KEY.
The password and the token are never logged.
"""
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Callable

import httpx

from .config import Settings

log = logging.getLogger("journey.onemap_token")

CACHE_KEY = "journey:onemap-token"
RENEW_BEFORE_S = 300            # use a fresh token when the current one has less than this left


class OneMapToken:
    def __init__(self, settings: Settings, redis_client=None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 http: httpx.Client | None = None):
        self._s = settings
        self._r = redis_client
        self._clock = clock
        self._http = http or httpx.Client(base_url=settings.onemap_base_url, timeout=settings.onemap_timeout_s)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0                      # unix seconds

    @property
    def enabled(self) -> bool:
        return bool(self._s.onemap_email and self._s.onemap_password)

    def get(self, refresh: bool = False) -> str | None:
        """A token that is good for at least a few more minutes, or None when logging in isn't possible
        (not configured, or OneMap refused or didn't answer: the caller then tries the static key)."""
        if not self.enabled:
            return None
        with self._lock:
            now = self._clock().timestamp()
            if not refresh and self._token and self._expires - now > RENEW_BEFORE_S:
                return self._token
            if not refresh and self._load_shared(now):
                return self._token
            return self._login(now)

    def _load_shared(self, now: float) -> bool:
        if self._r is None:
            return False
        try:
            raw = self._r.get(CACHE_KEY)
            if raw:
                saved = json.loads(raw)
                if saved["exp"] - now > RENEW_BEFORE_S:
                    self._token, self._expires = saved["token"], saved["exp"]
                    return True
        except Exception as e:  # noqa: BLE001 - a Redis problem just means we log in ourselves
            log.warning("OneMap token cache read failed: %s", type(e).__name__)
        return False

    def _login(self, now: float) -> str | None:
        try:
            r = self._http.post("/api/auth/post/getToken",
                                json={"email": self._s.onemap_email, "password": self._s.onemap_password})
            if r.status_code != 200:
                log.error("OneMap login failed with HTTP %s (check ONEMAP_API_EMAIL / ONEMAP_API_PASSWORD)", r.status_code)
                return None
            body = r.json()
            token, expires = body["access_token"], float(body["expiry_timestamp"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.error("OneMap login failed: %s", type(e).__name__)
            return None
        self._token, self._expires = token, expires
        log.info("OneMap token renewed; valid for about %d hours", (expires - now) // 3600)
        if self._r is not None:
            try:
                self._r.set(CACHE_KEY, json.dumps({"token": token, "exp": expires}),
                            ex=max(1, int(expires - now - RENEW_BEFORE_S)))
            except Exception as e:  # noqa: BLE001
                log.warning("OneMap token cache write failed: %s", type(e).__name__)
        return token
