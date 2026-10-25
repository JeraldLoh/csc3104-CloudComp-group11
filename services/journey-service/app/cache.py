# To be on AWS Lambda, the service needs only the files in app/ (plus config.py, cache.py, lta.py, disruption.py and aws_resources.py).

"""A small JSON cache in front of the external APIs, kept in Redis.

A Redis outage must not break the service, so every failure here is logged and treated as a cache miss.
With no Redis client at all, nothing is cached.
"""
import json
import logging
from typing import Any, Callable

log = logging.getLogger("journey.cache")


class JsonCache:
    def __init__(self, redis_client=None, prefix: str = "journey:cache:"):
        self._r = redis_client
        self._prefix = prefix

    def cached(self, key: str, ttl_s: int, load: Callable[[], Any]) -> Any:
        """The cached value for `key`, or `load()` (stored for `ttl_s` seconds) on a miss."""
        full = self._prefix + key
        if self._r is not None:
            try:
                raw = self._r.get(full)
                if raw is not None:
                    return json.loads(raw)
            except Exception as e:  # noqa: BLE001
                log.warning("cache get %s failed: %s", key, type(e).__name__)
        value = load()
        if self._r is not None:
            try:
                self._r.set(full, json.dumps(value), ex=ttl_s)
            except Exception as e:  # noqa: BLE001
                log.warning("cache set %s failed: %s", key, type(e).__name__)
        return value
