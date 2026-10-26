"""Read side for the dashboard: what the Aggregator has already written to Redis.

    decisions:recent         last decisions across all travellers (newest first)
    decisions:{travellerId}  one traveller's last decisions
    risk:{travellerId}       current risk score / level

Nothing here writes. Entries that can't be parsed are skipped, so one bad record
can't break the page.
"""
import json
import logging

from .risk import ELEVATED, Risk
from .service import RECENT_DECISIONS_KEY, risk_key, traveller_decisions_key

log = logging.getLogger("aggregator.dashboard")

RISK_PREFIX = "risk:"


def _parse(raw: str) -> dict | None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("skipping unreadable decision record")
        return None
    return value if isinstance(value, dict) else None


async def decisions(redis, traveller_id: str | None = None, limit: int = 50) -> list[dict]:
    """Newest first. All travellers, or just one."""
    key = traveller_decisions_key(traveller_id) if traveller_id else RECENT_DECISIONS_KEY
    raw = await redis.lrange(key, 0, limit - 1)
    return [d for d in map(_parse, raw) if d is not None]


async def traveller_status(redis) -> list[dict]:
    """GET /api/status/travellers (contracts/api.md): one row per traveller with a risk score.

    Contract fields travellerId, tripId, riskLevel, detectorsUsed, plus what the dashboard
    shows: score, calm streak, when it last changed and the latest decision.
    detectorsUsed is how many were asked last time; nextDetectors is how many the
    Coordinator will ask next (adaptive mode: 4 while elevated).
    """
    rows = []
    async for key in redis.scan_iter(match=f"{RISK_PREFIX}*", count=200):
        traveller_id = key[len(RISK_PREFIX):]
        h = await redis.hgetall(risk_key(traveller_id))
        if not h:
            continue
        risk = Risk.from_hash(h)
        latest = await decisions(redis, traveller_id, limit=1)
        last = latest[0] if latest else {}
        try:
            used = int(h.get("detectorsUsed", 0))
        except ValueError:
            used = None
        rows.append({
            "travellerId": traveller_id,
            "tripId": last.get("tripId"),
            "riskLevel": risk.level,
            "detectorsUsed": used,
            "riskScore": round(risk.score, 4),
            "calmStreak": risk.calm_streak,
            "nextDetectors": 4 if risk.level == ELEVATED else 2,
            "updatedAt": h.get("updatedAt"),
            "lastDecision": last.get("decision"),
            "lastSeq": last.get("seq"),
        })
    # Elevated first, then most recently updated.
    rows.sort(key=lambda r: r["updatedAt"] or "", reverse=True)
    rows.sort(key=lambda r: r["riskLevel"] != ELEVATED)
    return rows
