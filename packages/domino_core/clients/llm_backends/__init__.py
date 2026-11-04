"""Request/reply types shared by the LLM backends (`claude_code` subscription CLI, `api` Anthropic SDK)."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class Usage:
    """Token counts with API semantics: `input_tokens` excludes cache writes and reads."""

    input_tokens: int
    output_tokens: int
    cache_write: int = 0
    cache_read: int = 0


@dataclass(frozen=True)
class BackendRequest:
    model: str
    system: str
    user: str
    schema: type[BaseModel] | None
    max_tokens: int
    cache_system: bool


@dataclass(frozen=True)
class BackendReply:
    """What the model returned. `error` is a failure found after tokens were spent (cost still counts)."""

    text: str
    usage: Usage | None
    stop_reason: str | None = None
    error: str | None = None
