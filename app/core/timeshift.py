"""Wall-clock time shifting for the live replay (keeps clock times in texts and labels consistent across DST)."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.core.i18n import TZ

SHIFT_GRANULARITY = 300  # 5 minutes


def wall(ts: datetime) -> datetime:
    return ts.astimezone(TZ).replace(tzinfo=None)


def shift_ts(ts: datetime | None, seconds: int) -> datetime | None:
    if ts is None:
        return None
    if not seconds:
        return ts
    return (wall(ts) + timedelta(seconds=seconds)).replace(tzinfo=TZ)


def unshift_ts(ts: datetime, seconds: int) -> datetime:
    return (wall(ts) - timedelta(seconds=seconds)).replace(tzinfo=TZ)


def aligned_shift(now: datetime, source_ts: datetime, extra_minutes: int = 0) -> int:
    """Wall-clock shift (s) mapping ``source_ts`` to just before ``now``, a multiple of 5 minutes."""
    raw = int((wall(now) - wall(source_ts)).total_seconds())
    return (raw // SHIFT_GRANULARITY) * SHIFT_GRANULARITY - extra_minutes * 60
