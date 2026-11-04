"""domino_core.timeutil: UTC storage, KST report_date and asof."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from domino_core.errors import PermanentError
from domino_core.timeutil import KST, asof_for, now_utc, report_date, to_kst


def test_kst_is_asia_seoul() -> None:
    assert str(KST) == "Asia/Seoul"


def test_now_utc_is_aware_utc() -> None:
    now = now_utc()
    assert now.utcoffset() == timedelta(0)


def test_report_date_before_kst_midnight_is_previous_day() -> None:
    assert report_date(datetime(2026, 11, 3, 14, 59, 59, tzinfo=UTC)) == date(2026, 11, 3)


def test_report_date_at_kst_midnight_is_same_day() -> None:
    assert report_date(datetime(2026, 11, 3, 15, 0, 0, tzinfo=UTC)) == date(2026, 11, 4)


def test_report_date_accepts_other_offsets() -> None:
    est = timezone(timedelta(hours=-5))
    assert report_date(datetime(2026, 11, 3, 10, 0, 0, tzinfo=est)) == date(2026, 11, 4)


def test_report_date_default_uses_now() -> None:
    assert report_date() == now_utc().astimezone(KST).date()


def test_report_date_rejects_naive() -> None:
    with pytest.raises(PermanentError):
        report_date(datetime(2026, 11, 3, 15, 0, 0))


def test_asof_for_is_0600_kst_in_utc() -> None:
    asof = asof_for(date(2026, 11, 4))
    assert asof == datetime(2026, 11, 3, 21, 0, 0, tzinfo=UTC)
    assert asof.utcoffset() == timedelta(0)


def test_to_kst_converts_timezone() -> None:
    kst = to_kst(datetime(2026, 11, 3, 21, 0, 0, tzinfo=UTC))
    assert kst.utcoffset() == timedelta(hours=9)
    assert (kst.year, kst.month, kst.day, kst.hour) == (2026, 11, 4, 6)


def test_to_kst_rejects_naive() -> None:
    with pytest.raises(PermanentError):
        to_kst(datetime(2026, 11, 3, 21, 0, 0))
