"""domino_core.timeutil: UTC storage, KST report_date and asof."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from domino_core.errors import PermanentError
from domino_core.settings import get_settings
from domino_core.timeutil import KST, asof_for, now_utc, report_date, to_kst

REQUIRED_ENV = {
    "PG_DSN": "postgresql+psycopg://u:p@localhost:5432/domino",
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "x",
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_ENDPOINT": "http://localhost:9000",
    "S3_KEY": "x",
    "S3_SECRET": "x",
    "LANGFUSE_HOST": "http://localhost:3000",
    "LANGFUSE_PUBLIC_KEY": "pk-test",
    "LANGFUSE_SECRET_KEY": "x",
    "ANTHROPIC_API_KEY": "x",
    "RESEND_API_KEY": "x",
    "FROM_EMAIL": "report@example.com",
}


@pytest.fixture
def hour_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Fixed environment for get_settings(); the autouse conftest fixture clears the cache around each test."""
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("REPORT_HOUR_KST", raising=False)
    return monkeypatch


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


def test_asof_for_default_hour_is_0600_kst_in_utc(hour_env: pytest.MonkeyPatch) -> None:
    asof = asof_for(date(2026, 11, 4))
    assert asof == datetime(2026, 11, 3, 21, 0, 0, tzinfo=UTC)
    assert asof.utcoffset() == timedelta(0)


def test_asof_for_reads_report_hour_kst_6(hour_env: pytest.MonkeyPatch) -> None:
    hour_env.setenv("REPORT_HOUR_KST", "6")
    assert asof_for(date(2026, 11, 4)) == datetime(2026, 11, 3, 21, 0, 0, tzinfo=UTC)


def test_asof_for_reads_report_hour_kst_7(hour_env: pytest.MonkeyPatch) -> None:
    hour_env.setenv("REPORT_HOUR_KST", "7")
    asof = asof_for(date(2026, 11, 4))
    assert asof == datetime(2026, 11, 3, 22, 0, 0, tzinfo=UTC)
    assert asof.utcoffset() == timedelta(0)


def test_asof_for_hour_zero_is_previous_day_1500_utc(hour_env: pytest.MonkeyPatch) -> None:
    hour_env.setenv("REPORT_HOUR_KST", "0")
    assert asof_for(date(2026, 11, 4)) == datetime(2026, 11, 3, 15, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("hour", ["24", "-1"])
def test_asof_for_rejects_out_of_range_hour(hour_env: pytest.MonkeyPatch, hour: str) -> None:
    hour_env.setenv("REPORT_HOUR_KST", hour)
    with pytest.raises(PermanentError):
        asof_for(date(2026, 11, 4))


def test_asof_for_without_other_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """⑥/L10: asof_for is pure — only REPORT_HOUR_KST is read, no other required setting."""
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here either
    monkeypatch.setenv("REPORT_HOUR_KST", "6")
    assert asof_for(date(2026, 11, 4)) == datetime(2026, 11, 3, 21, 0, 0, tzinfo=UTC)


def test_asof_for_default_hour_without_any_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in [*REQUIRED_ENV, "REPORT_HOUR_KST"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    assert asof_for(date(2026, 11, 4)) == datetime(2026, 11, 3, 21, 0, 0, tzinfo=UTC)


def test_asof_for_empty_hour_keeps_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPORT_HOUR_KST", "")
    assert asof_for(date(2026, 11, 4)).hour == 21


def test_asof_for_reads_dotenv_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The light path reads `.env` like Settings does, so a dev's file value is honored."""
    monkeypatch.delenv("REPORT_HOUR_KST", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("REPORT_HOUR_KST=7\n", encoding="utf-8")
    assert asof_for(date(2026, 11, 4)).hour == 22


def test_asof_for_matches_settings_field(hour_env: pytest.MonkeyPatch) -> None:
    """Settings.report_hour_kst and asof_for read the same variable (decision ⑥ keeps one source)."""
    hour_env.setenv("REPORT_HOUR_KST", "7")
    assert get_settings().report_hour_kst == 7
    assert asof_for(date(2026, 11, 4)).hour == 22


def test_asof_for_garbage_hour_is_permanent(hour_env: pytest.MonkeyPatch) -> None:
    hour_env.setenv("REPORT_HOUR_KST", "six")
    with pytest.raises(PermanentError):
        asof_for(date(2026, 11, 4))


def test_to_kst_converts_timezone() -> None:
    kst = to_kst(datetime(2026, 11, 3, 21, 0, 0, tzinfo=UTC))
    assert kst.utcoffset() == timedelta(hours=9)
    assert (kst.year, kst.month, kst.day, kst.hour) == (2026, 11, 4, 6)


def test_to_kst_rejects_naive() -> None:
    with pytest.raises(PermanentError):
        to_kst(datetime(2026, 11, 3, 21, 0, 0))
