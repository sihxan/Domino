"""Structured JSON logging (structlog) with run_id/flow/key context binding."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from pydantic import SecretBytes, SecretStr
from structlog.typing import EventDict, FilteringBoundLogger, WrappedLogger

CONTEXT_KEYS = ("run_id", "flow", "key")
MASK = "**********"


def _mask_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    def mask(value: Any) -> Any:
        if isinstance(value, SecretStr | SecretBytes):
            return MASK
        if isinstance(value, dict):
            return {k: mask(v) for k, v in value.items()}
        if isinstance(value, list | tuple | set):
            return [mask(v) for v in value]
        return value

    return {k: mask(v) for k, v in event_dict.items()}


def _ensure_context_keys(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for name in CONTEXT_KEYS:
        event_dict.setdefault(name, None)
    return event_dict


def _stderr_logger(*_args: Any) -> WrappedLogger:
    # Resolve sys.stderr per logger so redirection (pytest capsys) takes effect; stdout stays free for CLI output.
    return structlog.PrintLogger(file=sys.stderr)


def configure_logging(level: int | str = "INFO") -> None:
    """Configure structlog to emit one JSON object per line on stderr; safe to call repeatedly."""
    numeric = logging.getLevelNamesMapping()[level.upper()] if isinstance(level, str) else level
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _ensure_context_keys,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _mask_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=_stderr_logger,
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> FilteringBoundLogger:
    """Return a logger whose lines always carry run_id, flow and key (null when unbound)."""
    if not structlog.is_configured():
        configure_logging()
    log: FilteringBoundLogger = structlog.get_logger()
    return log.bind(logger=name) if name else log


@contextmanager
def bind_context(*, run_id: str | None = None, flow: str | None = None, key: str | None = None) -> Iterator[None]:
    """Bind the given run_id/flow/key to every log line in the block; outer bindings are kept and restored."""
    given = {"run_id": run_id, "flow": flow, "key": key}
    with structlog.contextvars.bound_contextvars(**{k: v for k, v in given.items() if v is not None}):
        yield
