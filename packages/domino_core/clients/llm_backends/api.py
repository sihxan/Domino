"""Anthropic API backend: Messages API with native structured output and prompt caching (decisions W2-4·W2-6·T7)."""

from __future__ import annotations

from typing import Any

import anthropic

from domino_core.clients.llm_backends import BackendReply, BackendRequest, Usage
from domino_core.errors import PermanentError, TransientError
from domino_core.settings import Settings


def _client(settings: Settings) -> anthropic.Anthropic:
    """SDK client with its default retries (W2-4); the caller has checked that the key exists."""
    assert settings.anthropic_api_key is not None
    return anthropic.Anthropic(api_key=settings.anthropic_api_key.get_secret_value())


def build_request(req: BackendRequest) -> dict[str, Any]:
    """messages.create kwargs. No sampling parameters: API version 2026-10-21 rejects them (decisions T7)."""
    kwargs: dict[str, Any] = {
        "model": req.model,
        "max_tokens": req.max_tokens,
        "system": (
            [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}]
            if req.cache_system
            else req.system
        ),
        "messages": [{"role": "user", "content": req.user}],
    }
    if req.schema is not None:
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(req.schema)}
        }
    return kwargs


def run(req: BackendRequest, settings: Settings) -> BackendReply:
    """One Messages API call; SDK errors become TransientError (429·5xx·529·network) or PermanentError."""
    if settings.anthropic_api_key is None:
        raise PermanentError("api backend needs settings.anthropic_api_key (ANTHROPIC_API_KEY)")
    client = _client(settings)
    try:
        msg = client.messages.create(**build_request(req))
    except anthropic.APIConnectionError as e:  # includes APITimeoutError
        raise TransientError(f"anthropic {type(e).__name__}") from None
    except anthropic.APIStatusError as e:
        if e.status_code == 429 or e.status_code >= 500:
            raise TransientError(f"anthropic {type(e).__name__} {e.status_code}") from None
        raise PermanentError(f"anthropic {type(e).__name__} {e.status_code}") from None
    text = "".join(block.text for block in msg.content if block.type == "text")
    u = msg.usage
    usage = Usage(
        u.input_tokens, u.output_tokens, u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0
    )
    return BackendReply(text, usage, msg.stop_reason)
