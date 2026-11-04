"""Extraction output schema (code-phase0.md §2.1): what the LLM returns for one chunk."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

PROMPT_VERSION = "ext-1.0"

EntityType = Literal[
    "INDICATOR", "ASSET", "INDUSTRY", "TECHNOLOGY", "POLICY", "COMPANY", "COMMODITY", "INFRA", "STRUCTURAL"
]
Direction = Literal["UP", "DOWN", "VOLATILITY", "NEUTRAL"]
RelKind = Literal["CAUSAL", "CORRELATION", "SPECULATIVE", "CONDITIONAL"]
CausalType = Literal["DIRECT", "ENABLING", "BOTTLENECK", "SUBSTITUTION"]
Horizon = Literal["ultra_short", "short", "long"]
ClaimType = Literal["fact", "attribution"]


class EntityRef(BaseModel):
    """An entity as written in the text, with its type and the direction it moves."""

    name: str = Field(min_length=1, max_length=120, description="원문 표현 그대로. 번역·정규화 금지")
    type: EntityType
    direction: Direction
    region: str = Field(default="GL", description="ISO-2 국가 또는 GL(글로벌). 원문이 특정 국가 지표면 그 국가")


class CausalTriple(BaseModel):
    """One [cause] --(mechanism)--> [effect] statement the text itself makes, with its verbatim quote."""

    cause: EntityRef
    effect: EntityRef
    mechanism: str = Field(max_length=200)
    targets: list[EntityRef] = Field(default_factory=list, max_length=3)
    relation_kind: RelKind
    causal_type: CausalType
    polarity: Literal["POSITIVE", "NEGATIVE"]
    horizon: Horizon = Field(description="원문 시제·표현으로: 즉각/수주=ultra_short, 수개월/분기=short, 수년/구조=long")
    lag_days: int | None = None
    impact: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1, description="원문의 단정 강도")
    claim_type: ClaimType = Field(description="fact=결과·사실 서술, attribution=누가 주장·발표했다는 서술")
    chain_position: int | None = None
    valid_at_hint: str | None = Field(default=None, description="원문에 인과 성립 시점이 있으면 ISO date, 없으면 null")
    quote: str = Field(max_length=300)


class EventHint(BaseModel):
    """A specific dated occurrence the chunk reports (announcement, decision, incident, data release)."""

    title: str = Field(max_length=120)
    occurred_at: str | None = None  # ISO date
    category: Literal["MACRO", "TECH", "POLICY", "CORPORATE", "GEO"]
    country: str = "GL"
    actors: list[str] = Field(default_factory=list, max_length=4)  # 원문 표현
    action_type: str | None = None  # CAMEO 대분류 명 (MAKE_STATEMENT, CONSULT, ... , FIGHT)
    scheduled_at: str | None = None  # 예정 사건이면 ISO date


class ExtractionResult(BaseModel):
    """The full LLM output for one chunk: an optional event, at most 10 triples, and the no-causal-content flag."""

    event: EventHint | None
    triples: list[CausalTriple] = Field(max_length=10)
    no_causal_content: bool

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        if self.no_causal_content and self.triples:
            raise ValueError("no_causal_content with triples")
        return self
