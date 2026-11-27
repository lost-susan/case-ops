from datetime import datetime, timezone


def utcnow() -> datetime:
    """Single source of 'now' (timezone-aware UTC) so tests can control time."""
    return datetime.now(timezone.utc)
