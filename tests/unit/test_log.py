"""domino_core.log: structlog JSON lines with run_id/flow/key context binding (code §0.1)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
import structlog
from pydantic import SecretStr

from domino_core.log import bind_context, configure_logging, get_logger


@pytest.fixture(autouse=True)
def clean_logging() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    configure_logging()
    yield
    structlog.contextvars.clear_contextvars()


def lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    err = capsys.readouterr().err
    return [json.loads(line) for line in err.splitlines() if line]


def test_emits_one_json_line_per_event(capsys: pytest.CaptureFixture[str]) -> None:
    log = get_logger("t")
    log.info("first", n=1)
    log.warning("second")
    err = capsys.readouterr().err
    raw = err.splitlines()
    assert len(raw) == 2
    first, second = (json.loads(line) for line in raw)
    assert first["event"] == "first"
    assert first["n"] == 1
    assert first["level"] == "info"
    assert second["level"] == "warning"


def test_timestamp_is_iso_utc(capsys: pytest.CaptureFixture[str]) -> None:
    get_logger("t").info("x")
    (rec,) = lines(capsys)
    ts = datetime.fromisoformat(rec["timestamp"])
    assert ts.utcoffset() is not None
    assert ts.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_logger_name_is_included(capsys: pytest.CaptureFixture[str]) -> None:
    get_logger("domino_ingest.crawl").info("x")
    (rec,) = lines(capsys)
    assert rec["logger"] == "domino_ingest.crawl"


def test_bound_run_id_flow_key_are_in_output(capsys: pytest.CaptureFixture[str]) -> None:
    log = get_logger("t")
    with bind_context(run_id="run_01", flow="ingest_flow", key="src_us_fed_press"):
        log.info("fetched")
    (rec,) = lines(capsys)
    assert rec["run_id"] == "run_01"
    assert rec["flow"] == "ingest_flow"
    assert rec["key"] == "src_us_fed_press"


def test_binding_is_released_after_block(capsys: pytest.CaptureFixture[str]) -> None:
    log = get_logger("t")
    with bind_context(run_id="run_01", flow="ingest_flow", key="k"):
        log.info("inside")
    log.info("outside")
    inside, outside = lines(capsys)
    assert inside["run_id"] == "run_01"
    assert outside["run_id"] is None
    assert outside["flow"] is None
    assert outside["key"] is None


def test_binding_is_released_when_block_raises(capsys: pytest.CaptureFixture[str]) -> None:
    log = get_logger("t")
    with pytest.raises(RuntimeError), bind_context(run_id="run_01"):
        raise RuntimeError("boom")
    log.info("after")
    (rec,) = lines(capsys)
    assert rec["run_id"] is None


def test_unbound_keys_are_null_not_missing(capsys: pytest.CaptureFixture[str]) -> None:
    get_logger("t").info("bare")
    (rec,) = lines(capsys)
    for name in ("run_id", "flow", "key"):
        assert name in rec
        assert rec[name] is None


def test_nested_binding_adds_and_restores(capsys: pytest.CaptureFixture[str]) -> None:
    log = get_logger("t")
    with bind_context(run_id="run_01", flow="ingest_flow"):
        with bind_context(key="src_a"):
            log.info("per-source")
        log.info("flow-level")
    per_source, flow_level = lines(capsys)
    assert (per_source["run_id"], per_source["flow"], per_source["key"]) == ("run_01", "ingest_flow", "src_a")
    assert (flow_level["run_id"], flow_level["flow"], flow_level["key"]) == ("run_01", "ingest_flow", None)


def test_secretstr_value_is_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    secret = SecretStr("super-secret-token-123")
    log = get_logger("t")
    log.info("auth", token=secret, nested={"k": secret, "items": [secret]})
    out = capsys.readouterr().err
    assert "super-secret-token-123" not in out
    rec = json.loads(out)
    assert rec["token"] == "**********"
    assert rec["nested"] == {"k": "**********", "items": ["**********"]}


def test_secretstr_in_bound_context_is_not_logged(capsys: pytest.CaptureFixture[str]) -> None:
    with bind_context(run_id=SecretStr("super-secret-token-123")):  # type: ignore[arg-type]
        get_logger("t").info("x")
    assert "super-secret-token-123" not in capsys.readouterr().err


def test_exception_is_rendered_into_json(capsys: pytest.CaptureFixture[str]) -> None:
    log = get_logger("t")
    try:
        raise ValueError("bad")
    except ValueError:
        log.exception("failed")
    (rec,) = lines(capsys)
    assert rec["level"] == "error"
    assert "ValueError: bad" in rec["exception"]


def test_info_level_by_default_filters_debug(capsys: pytest.CaptureFixture[str]) -> None:
    log = get_logger("t")
    log.debug("hidden")
    assert lines(capsys) == []


def test_get_logger_configures_lazily(capsys: pytest.CaptureFixture[str]) -> None:
    structlog.reset_defaults()
    get_logger("t").info("lazy")
    (rec,) = lines(capsys)
    assert rec["event"] == "lazy"
    assert rec["run_id"] is None
