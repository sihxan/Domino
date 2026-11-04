"""The single LLM entry point: backend choice, schema validation, API-equivalent cost and Redis budgets (code §0.4)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, overload

import yaml
from pydantic import BaseModel, ValidationError

from domino_core.clients import default_settings
from domino_core.clients.llm_backends import BackendReply, BackendRequest, Usage
from domino_core.clients.llm_backends import api as api_backend
from domino_core.clients.llm_backends import claude_code as claude_code_backend
from domino_core.clients.redis import get_redis, redis_errors
from domino_core.errors import BudgetExceeded, DominoError, PermanentError
from domino_core.log import get_logger
from domino_core.settings import Settings
from domino_core.timeutil import report_date

__all__ = ["LLMResult", "ModelPrice", "Pricing", "Usage", "budget_limit", "call", "cost_usd", "load_pricing"]

PRICING_PATH = Path(__file__).resolve().parent.parent / "pricing.yaml"
MONTH_CAP_TASKS = frozenset({"extract", "classify"})  # tech §6.4: only extraction stops at the monthly cap
DAY_TTL_S = 8 * 86400
MONTH_TTL_S = 40 * 86400

BACKENDS: dict[str, Callable[[BackendRequest, Settings], BackendReply]] = {
    "claude_code": claude_code_backend.run,
    "api": api_backend.run,
}



@dataclass
class LLMResult[T]:
    parsed: T
    raw: str
    input_tokens: int  # uncached input + cache writes; input_tokens + cache_read = whole prompt
    output_tokens: int
    cost_usd: float  # API-equivalent (pricing.yaml), also on the subscription backend
    cache_read: int


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    output: float
    cache_write: float
    cache_read: float


@dataclass(frozen=True)
class Pricing:
    models: dict[str, ModelPrice]
    budgets: dict[str, float]
    monthly_cap_usd: float


def load_pricing(path: Path | None = None) -> Pricing:
    """Parse pricing.yaml (cached per path); a malformed file is a PermanentError."""
    return _load_pricing(path or PRICING_PATH)


@lru_cache(maxsize=4)
def _load_pricing(path: Path) -> Pricing:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        models = {
            str(name): ModelPrice(**{k: float(p[k]) for k in ("input", "output", "cache_write", "cache_read")})
            for name, p in data["models"].items()
        }
        budgets = {str(k): float(v) for k, v in data["budgets"].items()}
        cap = float(data["monthly_cap_usd"])
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError) as e:
        raise PermanentError(f"invalid pricing file {path}: {type(e).__name__}: {e}") from e
    return Pricing(models, budgets, cap)


def cost_usd(price: ModelPrice, usage: Usage) -> float:
    """Tokens times per-MTok price, cache writes and reads priced separately."""
    return (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_write * price.cache_write
        + usage.cache_read * price.cache_read
    ) / 1_000_000


def budget_limit(budget_key: str, settings: Settings, pricing: Pricing) -> float:
    """Limit for `budget:<kind>:<…>` — kind from Settings or pricing.yaml `budgets` (W2-5); unknown → PermanentError."""
    parts = budget_key.split(":")
    if len(parts) < 3 or parts[0] != "budget" or not parts[1] or not parts[2]:
        raise PermanentError(f"malformed budget key {budget_key!r} (expected budget:<kind>:<date|run>)")
    kind = parts[1]
    from_settings = {
        "extract": settings.budget_extract_daily,
        "judge": settings.budget_judge_daily,
        "report": settings.budget_report_run,
    }
    if kind in from_settings:
        return from_settings[kind]
    if kind in pricing.budgets:
        return pricing.budgets[kind]
    raise PermanentError(f"unknown budget kind {kind!r} in {budget_key!r}")


@overload
def call[M: BaseModel](
    *,
    task: str,
    model: str,
    system: str,
    user: str,
    schema: type[M],
    temperature: float = ...,
    max_tokens: int = ...,
    budget_key: str | None = ...,
    cache_system: bool = ...,
    trace: dict[str, Any] | None = ...,
) -> LLMResult[M]: ...


@overload
def call(
    *,
    task: str,
    model: str,
    system: str,
    user: str,
    schema: None = ...,
    temperature: float = ...,
    max_tokens: int = ...,
    budget_key: str | None = ...,
    cache_system: bool = ...,
    trace: dict[str, Any] | None = ...,
) -> LLMResult[str]: ...


def call(
    *,
    task: str,
    model: str,
    system: str,
    user: str,
    schema: type[BaseModel] | None = None,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    budget_key: str | None = None,
    cache_system: bool = True,
    trace: dict[str, Any] | None = None,
) -> LLMResult[Any]:
    """429/5xx → TransientError (subscription limit → UsageLimitReached), schema mismatch → PermanentError,
    budget exceeded → BudgetExceeded. Spent tokens are added to the budget counters even when validation fails."""
    log = get_logger("domino_core.clients.llm")  # per call: the stderr target is resolved at bind time
    settings = default_settings()
    pricing = load_pricing()
    price = pricing.models.get(model)
    if price is None:
        raise PermanentError(f"model {model!r} has no entry in pricing.yaml")
    limit = budget_limit(budget_key, settings, pricing) if budget_key is not None else None
    month_key = f"budget:month:{report_date():%Y-%m}"
    redis = get_redis()

    with redis_errors():
        if budget_key is not None and limit is not None:
            spent = float(redis.get(budget_key) or 0.0)
            if spent >= limit:
                raise BudgetExceeded(f"{budget_key}: {spent:.4f} >= {limit} USD")
        month = float(redis.get(month_key) or 0.0)
    if month >= pricing.monthly_cap_usd:
        if task in MONTH_CAP_TASKS:
            raise BudgetExceeded(f"{month_key}: {month:.2f} >= monthly cap {pricing.monthly_cap_usd} USD")
        log.warning("llm_monthly_cap_exceeded", task=task, month_usd=month, cap_usd=pricing.monthly_cap_usd)
    if temperature != 0.0:
        log.debug("llm_temperature_ignored", task=task, temperature=temperature)

    backend = settings.llm_backend
    req = BackendRequest(model, system, user, schema, max_tokens, cache_system)
    meta = {"task": task, "backend": backend, "model": model, "prompt_version": (trace or {}).get("prompt_version")}
    started = time.monotonic()
    try:
        reply = BACKENDS[backend](req, settings)
    except DominoError as e:
        log.warning("llm_call_failed", **meta, error=type(e).__name__, latency_ms=_ms(started))
        raise

    usage = reply.usage
    if usage is None:
        log.warning("llm_usage_missing", **meta)
        usage = Usage(0, 0, 0, 0)
    cost = cost_usd(price, usage)
    with redis_errors():
        for key, ttl in ((budget_key, DAY_TTL_S), (month_key, MONTH_TTL_S)):
            if key is not None:
                redis.incrbyfloat(key, cost)
                redis.expire(key, ttl)
    log.info(
        "llm_call",
        **meta,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_write=usage.cache_write,
        cache_read=usage.cache_read,
        cost_usd=round(cost, 6),
        stop_reason=reply.stop_reason,
        latency_ms=_ms(started),
    )

    if reply.error is not None:
        raise PermanentError(reply.error)
    if reply.stop_reason == "refusal":
        raise PermanentError("refusal: the model declined the request")
    parsed: Any = reply.text
    if schema is not None:
        if reply.stop_reason == "max_tokens":
            raise PermanentError(f"schema: output truncated at max_tokens={max_tokens}")
        try:
            parsed = schema.model_validate_json(reply.text)
        except ValidationError as e:
            raise PermanentError(f"schema: {schema.__name__}: {e.error_count()} error(s): {_first_error(e)}") from e
    return LLMResult(
        parsed=parsed,
        raw=reply.text,
        input_tokens=usage.input_tokens + usage.cache_write,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
        cache_read=usage.cache_read,
    )


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    return f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
