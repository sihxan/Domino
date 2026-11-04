"""UTC storage helpers and KST report_date / asof conversions."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from domino_core.errors import PermanentError
from domino_core.settings import get_report_hour_kst

KST = ZoneInfo("Asia/Seoul")


def _require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise PermanentError("naive datetime not allowed; pass a timezone-aware datetime")
    return dt


def now_utc() -> datetime:
    return datetime.now(UTC)


def report_date(now: datetime | None = None) -> date:  # KST 기준 오늘
    return to_kst(now if now is not None else now_utc()).date()


def asof_for(report_date: date) -> datetime:  # report_hour_kst(기본 6)시 KST → UTC; 그 값만 읽는다
    hour = get_report_hour_kst()
    if not 0 <= hour <= 23:
        raise PermanentError(f"report_hour_kst must be 0..23, got {hour}")
    return datetime.combine(report_date, time(hour, 0), tzinfo=KST).astimezone(UTC)


def to_kst(dt: datetime) -> datetime:
    return _require_aware(dt).astimezone(KST)
