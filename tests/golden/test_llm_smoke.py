"""Real-call smoke tests for `llm.call`, run once by hand.

`uv run pytest tests/golden/test_llm_smoke.py -m golden -s -k <name>`.

Needs the compose Redis (`REDIS_URL` in `.env`). claude_code uses the host's logged-in Claude subscription
(or `CLAUDE_CODE_OAUTH_TOKEN`); api needs a real `ANTHROPIC_API_KEY` and is skipped without one.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from domino_core.clients import llm
from domino_core.clients.redis import get_redis
from domino_core.settings import get_settings
from domino_core.timeutil import report_date

pytestmark = pytest.mark.golden


class Capital(BaseModel):
    city: str
    country: str


def _budget_value(key: str) -> float:
    return float(get_redis().get(key) or 0.0)


def test_claude_code_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly one subscription call with a tiny schema."""
    monkeypatch.setenv("LLM_BACKEND", "claude_code")
    settings = get_settings()
    key = f"budget:golden:{report_date().isoformat()}"
    before = _budget_value(key)
    res = llm.call(
        task="smoke",
        model=settings.model_extract,
        system="Answer with a JSON object only.",
        user="What is the capital city of France? Give the city and the country.",
        schema=Capital,
        max_tokens=256,
        budget_key=key,
    )
    after = _budget_value(key)
    print(
        f"\nclaude_code smoke: model={settings.model_extract} parsed={res.parsed.model_dump()} "
        f"input={res.input_tokens} output={res.output_tokens} cache_read={res.cache_read} "
        f"cost_usd(API-equivalent)={res.cost_usd:.6f} redis {key}: {before:.6f} -> {after:.6f}"
    )
    assert res.parsed.city.lower() == "paris"
    if res.input_tokens + res.output_tokens > 0:
        assert res.cost_usd > 0
        assert after > before


def test_api_smoke_with_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two calls with the real extraction system prompt: the second must read the prompt cache."""
    monkeypatch.setenv("LLM_BACKEND", "api")
    settings = get_settings()
    key = settings.anthropic_api_key
    if key is None or not key.get_secret_value().startswith("sk-ant-"):
        pytest.skip("no real ANTHROPIC_API_KEY")
    from domino_extract.prompts import EXTRACT_SYSTEM
    from domino_extract.schema import ExtractionResult

    budget_key = f"budget:golden:{report_date().isoformat()}"
    before = _budget_value(budget_key)
    user = "Source: test (grade A, en)\n\nHigher interest rates reduced housing starts in March."
    results = [
        llm.call(task="smoke", model=settings.model_extract, system=EXTRACT_SYSTEM, user=user,
                 schema=ExtractionResult, budget_key=budget_key)
        for _ in range(2)
    ]
    after = _budget_value(budget_key)
    print(f"\napi smoke: cache_read={[r.cache_read for r in results]} cost={[r.cost_usd for r in results]}")
    assert all(r.cost_usd > 0 for r in results)
    assert results[1].cache_read > 0
    assert after > before
