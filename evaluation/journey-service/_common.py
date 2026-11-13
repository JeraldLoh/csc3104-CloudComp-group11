"""Shared by the benchmark scripts: timed HTTP calls, statistics, and the results file.

Standard library only (no pip install), so the scripts run with any Python 3.10+ on the host.
"""
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

RESULTS = Path(__file__).resolve().parent / "results"


class Reply(NamedTuple):
    status: int                 # 0: no HTTP answer at all (refused, timed out); see `error`
    headers: dict               # lower-case names
    data: bytes
    seconds: float
    error: str | None

    def json(self):
        return json.loads(self.data)


def call(method: str, url: str, body=None, timeout: float = 120) -> Reply:
    """One timed HTTP request. Never raises: a failure is a Reply with status 0 or the error status."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"} if data else {}
    # "localhost" is tried as IPv6 (::1) first, and on Windows that costs about 2 s per request when the port only
    # listens on IPv4 (kind, Docker). Connect to 127.0.0.1 and keep the Host header (a presigned S3 link signs it).
    parts = urllib.parse.urlsplit(url)
    if parts.hostname == "localhost":
        headers["Host"] = parts.netloc
        url = urllib.parse.urlunsplit(parts._replace(netloc=parts.netloc.replace("localhost", "127.0.0.1", 1)))
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            content = r.read()
            return Reply(r.status, {k.lower(): v for k, v in r.headers.items()}, content,
                         time.perf_counter() - start, None)
    except urllib.error.HTTPError as e:
        return Reply(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read(),
                     time.perf_counter() - start, f"HTTP {e.code}")
    except Exception as e:                                   # refused, reset, timed out
        return Reply(0, {}, b"", time.perf_counter() - start, type(e).__name__)


def pct(values: list[float], p: float) -> float:
    """The p-th percentile (0-100), interpolated between the two nearest values."""
    if not values:
        return math.nan
    ordered = sorted(values)
    at = (len(ordered) - 1) * p / 100
    low, high = math.floor(at), math.ceil(at)
    return ordered[low] + (ordered[high] - ordered[low]) * (at - low)


def summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {"n": len(values), "mean": sum(values) / len(values), "p50": pct(values, 50), "p95": pct(values, 95),
            "min": min(values), "max": max(values)}


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Correlation of two lists, -1 to 1; None when it can't be worked out (too few points, or no spread)."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def table(headers: list[str], rows: list[list]) -> str:
    cells = [[str(c) for c in row] for row in [headers, *rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in cells]
    return "\n".join([lines[0], "  ".join("-" * w for w in widths), *lines[1:]])


def finish(name: str, data: dict) -> None:
    """Writes the raw numbers to results/<name>-<time>.json."""
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"benchmark": name, "ranAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                               **data}, indent=2))
    print(f"\nRaw results: {out}")


def stop(message: str) -> None:
    print(f"\nCan't run: {message}", file=sys.stderr)
    sys.exit(2)
