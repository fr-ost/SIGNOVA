"""UTC time helpers. All internal timestamps are timezone-aware UTC."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.enums import Timeframe


def utcnow() -> datetime:
    return datetime.now(UTC)


def from_ms(ms: int | float | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)


def from_seconds(seconds: int | float | str) -> datetime:
    return datetime.fromtimestamp(float(seconds), tz=UTC)


def to_ms(dt: datetime) -> int:
    return int(round(dt.timestamp() * 1000))


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_iso(value: str | None) -> datetime | None:
    """Parse ISO-8601 strings (including a trailing 'Z'). Returns None when unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def floor_to_timeframe(dt: datetime, timeframe: Timeframe) -> datetime:
    ms = to_ms(dt)
    return from_ms(ms - ms % timeframe.ms)


def age_seconds(then: datetime | None, now: datetime) -> float | None:
    if then is None:
        return None
    return (now - then).total_seconds()
