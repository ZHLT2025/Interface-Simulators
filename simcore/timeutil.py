from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FMT_SPACE = "%Y-%m-%d %H:%M:%S"  # yyyy-MM-dd HH:mm:ss (MOM / IoT)


def parse_space(s: str) -> datetime:
    """Parse 'yyyy-MM-dd HH:mm:ss' strictly. Raises ValueError."""
    if not isinstance(s, str):
        raise ValueError("not a string")
    return datetime.strptime(s, FMT_SPACE)


def is_space_format(s) -> bool:
    try:
        parse_space(s)
        return True
    except (ValueError, TypeError):
        return False


def now_in(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)


def fmt_space(dt: datetime) -> str:
    return dt.strftime(FMT_SPACE)


def iso_utc_ms(dt: datetime | None = None) -> str:
    """ISO 8601 UTC with millisecond precision: YYYY-MM-DDTHH:mm:ss.SSSZ (CV contract)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso_utc_ms(s: str) -> datetime:
    if not isinstance(s, str):
        raise ValueError("not a string")
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def shift(dt: datetime, seconds: float) -> datetime:
    return dt + timedelta(seconds=seconds)
