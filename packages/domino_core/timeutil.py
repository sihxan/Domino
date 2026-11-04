"""UTC storage helpers and KST report_date / asof conversions."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from domino_core.errors import PermanentError

KST = ZoneInfo("Asia/Seoul")


def _require_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise PermanentError("naive datetime not allowed; pass a timezone-aware datetime")
    return dt


def now_utc() -> datetime:
    return datetime.now(UTC)


def report_date(now: datetime | None = None) -> date:  # KST 기준 오늘
    return to_kst(now if now is not None else now_utc()).date()


def asof_for(report_date: date) -> datetime:  # 06:00 KST → UTC
    return datetime.combine(report_date, time(6, 0), tzinfo=KST).astimezone(UTC)


def to_kst(dt: datetime) -> datetime:
    return _require_aware(dt).astimezone(KST)
