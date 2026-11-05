"""Langfuse v2 SDK wrapper: one trace + generation per LLM call; failures only log a warning (decision W2-7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from langfuse import Langfuse

from domino_core.clients import default_settings
from domino_core.clients.llm_backends import Usage
from domino_core.log import MASK, get_logger

__all__ = ["flush", "trace_generation"]

TIMEOUT_S = 5  # per ingestion request; the SDK sends from a background thread, never on the call path
MAX_RETRIES = 2

_clients: dict[tuple[str, str, str], Any] = {}  # (host, public key, secret key) → client; in-memory only


def _new_client(host: str, public_key: str, secret_key: str) -> Any:
    """Build an SDK client (the seam tests replace)."""
    return Langfuse(
        public_key=public_key, secret_key=secret_key, host=host, timeout=TIMEOUT_S, max_retries=MAX_RETRIES
    )


def _config() -> tuple[str, str, str] | None:
    """(host, public key, secret key) from settings, or None when any is blank (tests, CI): tracing is skipped."""
    settings = default_settings()
    host = settings.langfuse_host.strip()
    public_key = settings.langfuse_public_key.strip()
    secret_key = settings.langfuse_secret_key.get_secret_value().strip()
    return (host, public_key, secret_key) if host and public_key and secret_key else None


def _client(config: tuple[str, str, str]) -> Any:
    """The client for this configuration, built once per process."""
    if config not in _clients:
        _clients[config] = _new_client(*config)
    return _clients[config]


def trace_generation(
    task: str,
    model: str,
    input: Any,
    output: Any,
    usage: Usage | None,
    cost_usd: float,
    latency_ms: int,
    metadata: dict[str, Any] | None,
) -> str | None:
    """Send one trace named `task` with one generation; return its trace id, or None if skipped or failed.

    `metadata["prompt_version"]` becomes the generation version and a trace tag; `metadata["error"]` marks the
    generation as ERROR. Never raises: Langfuse problems are a warning log line (secret key masked).
    """
    secret = ""
    try:
        config = _config()
        if config is None:
            return None
        secret = config[2]
        client = _client(config)
        meta = dict(metadata or {})
        prompt_version = meta.get("prompt_version")
        error = meta.pop("error", None)
        meta["task"] = task
        meta.setdefault("prompt_version", None)
        u = usage or Usage(0, 0)
        meta["cache_write"] = u.cache_write
        meta["cache_read"] = u.cache_read
        prompt_tokens = u.input_tokens + u.cache_write + u.cache_read
        tags = [f"task={task}"] + ([f"prompt_version={prompt_version}"] if prompt_version else [])
        end = datetime.now(UTC)
        trace = client.trace(name=task, tags=tags, metadata=meta)
        trace.generation(
            name=task,
            model=model,
            input=input,
            output=output,
            usage={
                "unit": "TOKENS",
                "input": prompt_tokens,
                "output": u.output_tokens,
                "total": prompt_tokens + u.output_tokens,
                "total_cost": cost_usd,
            },
            metadata=meta,
            version=prompt_version,
            level="ERROR" if error else "DEFAULT",
            status_message=error,
            start_time=end - timedelta(milliseconds=latency_ms),
            end_time=end,
        )
        return str(trace.id)
    except Exception as e:  # observability must never stop the pipeline (W2-7)
        detail = str(e)[:300]
        if secret:
            detail = detail.replace(secret, MASK)
        get_logger("domino_core.clients.langfuse").warning(
            "langfuse_trace_failed", task=task, error=type(e).__name__, detail=detail
        )
        return None


def flush() -> None:
    """Block until queued events of every built client are sent (scripts and short-lived processes)."""
    for client in list(_clients.values()):
        try:
            client.flush()
        except Exception as e:
            get_logger("domino_core.clients.langfuse").warning("langfuse_flush_failed", error=type(e).__name__)
