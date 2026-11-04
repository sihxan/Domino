"""Error hierarchy: the only exceptions Domino code raises."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class DominoError(Exception): ...


class TransientError(DominoError): ...  # 재시도 대상 (네트워크·429·5xx)


class PermanentError(DominoError): ...  # 재시도 금지 (스키마·파싱·규칙 위반)


class UsageLimitReached(TransientError):
    """Claude subscription usage limit hit (claude_code LLM backend): stop LLM work until `resets_at`."""

    def __init__(self, message: str, *, resets_at: datetime | None = None, limit_type: str | None = None) -> None:
        self.resets_at = resets_at  # UTC aware, None when the CLI did not report it
        self.limit_type = limit_type  # five_hour | seven_day | seven_day_opus | seven_day_sonnet | overage
        super().__init__(message)


class BudgetExceeded(PermanentError): ...


@dataclass
class GateFailure:
    code: str
    detail: str


class GateFailed(PermanentError):
    def __init__(self, failures: list[GateFailure]) -> None:
        self.failures = failures
        super().__init__("; ".join(f"{f.code}: {f.detail}" for f in failures))
