"""Error hierarchy: the only exceptions Domino code raises."""

from __future__ import annotations

from dataclasses import dataclass


class DominoError(Exception): ...


class TransientError(DominoError): ...  # 재시도 대상 (네트워크·429·5xx)


class PermanentError(DominoError): ...  # 재시도 금지 (스키마·파싱·규칙 위반)


class BudgetExceeded(PermanentError): ...


@dataclass
class GateFailure:
    code: str
    detail: str


class GateFailed(PermanentError):
    def __init__(self, failures: list[GateFailure]) -> None:
        self.failures = failures
        super().__init__("; ".join(f"{f.code}: {f.detail}" for f in failures))
