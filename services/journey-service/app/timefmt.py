"""Times as the API writes them."""
from datetime import datetime, timezone


def iso_utc(when: datetime) -> str:
    """UTC, to the second, with a Z ("2026-10-22T00:00:00Z"), the same way in every field."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
