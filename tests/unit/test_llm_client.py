"""domino_core.clients.llm: backend selection, error mapping, API-equivalent cost and Redis budgets (all mocked)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel, Field

from domino_core.clients import llm
from domino_core.clients.llm_backends import api as api_backend
from domino_core.clients.llm_backends import claude_code as cc_backend
from domino_core.errors import BudgetExceeded, PermanentError, TransientError, UsageLimitReached
from domino_core.timeutil import report_date

ROOT = Path(__file__).resolve().parents[2]
OAUTH_SECRET = "oauth-secret-value-xyz"
API_SECRET = "api-secret-value-xyz"
MODEL = "claude-haiku-5-5"  # test data; production code reads model names from settings only
TODAY = report_date().isoformat()
MONTH_KEY = f"budget:month:{report_date().strftime('%Y-%m')}"

BASE_ENV = {
    "PG_DSN": "postgresql+psycopg://u:p@localhost:5432/domino",
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "x",
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_ENDPOINT": "http://localhost:9000",
    "S3_KEY": "k",
    "S3_SECRET": "s",
    "LANGFUSE_HOST": "http://localhost:3000",
    "LANGFUSE_PUBLIC_KEY": "pk",
    "LANGFUSE_SECRET_KEY": "sk",
    "RESEND_API_KEY": "r",
    "FROM_EMAIL": "a@example.com",
}


class Out(BaseModel):
    answer: str = Field(max_length=5)
    n: int = Field(ge=0, le=10)


# --- fakes --------------------------------------------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, float] = {}
        self.ttl: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return str(self.values[key]) if key in self.values else None

    def incrbyfloat(self, key: str, amount: float) -> float:
        self.values[key] = self.values.get(key, 0.0) + amount
        return self.values[key]

    def expire(self, key: str, seconds: int) -> bool:
        self.ttl[key] = seconds
        return True


@dataclass
class FakeCli:
    """Stands in for the Claude Code CLI process: records each invocation, returns canned stream-json."""

    lines: list[dict[str, Any]] = field(default_factory=list)
    returncode: int = 0
    exc: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self, cmd: list[str], *, input: str, env: dict[str, str], cwd: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append({"cmd": cmd, "input": input, "env": env, "cwd": cwd, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        stdout = "\n".join(json.dumps(line) for line in self.lines) + "\n"
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=stdout, stderr="")


def usage(inp: int = 1000, out: int = 200, cw: int = 0, cr: int = 0) -> dict[str, int]:
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
    }


def result_line(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 10,
        "duration_api_ms": 9,
        "num_turns": 1,
        "session_id": "s1",
        "result": "",
        "usage": usage(),
        "total_cost_usd": 0.0001,
    }
    base.update(kw)
    return base


@dataclass
class FakeMessages:
    response: Any = None
    exc: BaseException | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc
        return self.response


@dataclass
class FakeClient:
    messages: FakeMessages


@dataclass
class _Block:
    text: str
    type: str = "text"


@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


@dataclass
class _Message:
    content: list[_Block]
    usage: _Usage
    stop_reason: str = "end_turn"


def api_message(text: str, stop_reason: str = "end_turn", cw: int = 0, cr: int = 0) -> _Message:
    return _Message([_Block(text)], _Usage(1000, 200, cw, cr), stop_reason)


# --- fixtures -----------------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in ["LLM_BACKEND", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)  # Settings reads `.env` from cwd; the repo's real one must not leak in


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(llm, "get_redis", lambda: fake)
    return fake


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, env: None) -> Iterator[FakeCli]:
    fake = FakeCli(lines=[result_line(structured_output={"answer": "ok", "n": 3})])
    monkeypatch.setattr(cc_backend, "_run_cli", fake)
    monkeypatch.setattr(cc_backend, "cli_path", lambda: "/fake/bin/claude")
    yield fake


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, env: None) -> FakeMessages:
    monkeypatch.setenv("LLM_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_SECRET)
    msgs = FakeMessages(response=api_message('{"answer": "ok", "n": 3}'))
    monkeypatch.setattr(api_backend, "_client", lambda settings: FakeClient(msgs))
    return msgs


def run(schema: type[BaseModel] | None = Out, **kw: Any) -> llm.LLMResult[Any]:
    args: dict[str, Any] = {"task": "extract", "model": MODEL, "system": "SYS", "user": "USER", "schema": schema}
    args.update(kw)
    return llm.call(**args)


def price() -> llm.ModelPrice:
    return llm.load_pricing().models[MODEL]


# --- pricing ------------------------------------------------------------------------------------


def test_pricing_yaml_has_models_budgets_and_monthly_cap() -> None:
    p = llm.load_pricing()
    for model in ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]:
        m = p.models[model]
        assert m.input > 0 and m.output > 0 and m.cache_write > 0 and m.cache_read > 0
    assert p.budgets == {"classify": 0.3, "linker": 0.5, "baseline": 0.5, "publish": 0.5, "golden": 2.0}
    assert p.monthly_cap_usd == 250


def test_cost_separates_cache_read_and_write() -> None:
    m = llm.ModelPrice(input=1.0, output=5.0, cache_write=1.25, cache_read=0.1)
    u = llm.Usage(input_tokens=1_000_000, output_tokens=2_000_000, cache_write=4_000_000, cache_read=8_000_000)
    assert llm.cost_usd(m, u) == pytest.approx(1.0 + 10.0 + 5.0 + 0.8)


# --- backend selection --------------------------------------------------------------------------


def test_default_backend_is_claude_code(cli: FakeCli, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_backend, "_client", lambda settings: pytest.fail("api backend must not be used"))
    res = run()
    assert len(cli.calls) == 1
    assert res.parsed == Out(answer="ok", n=3)


def test_api_backend_selected(api: FakeMessages, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc_backend, "_run_cli", lambda *a, **k: pytest.fail("claude_code must not be used"))
    res = run()
    assert len(api.calls) == 1
    assert res.parsed == Out(answer="ok", n=3)


# --- claude_code backend ------------------------------------------------------------------------


def test_claude_code_success_command_env_and_cost(
    cli: FakeCli, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_SECRET)  # e.g. compose env_file: must not reach the CLI
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.invalid")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_SECRET)
    cli.lines = [
        {"type": "system", "subtype": "init"},
        result_line(structured_output={"answer": "ok", "n": 3}, usage=usage(1000, 200, 300, 400)),
    ]
    res = run(budget_key=f"budget:extract:{TODAY}", max_tokens=777)

    call = cli.calls[0]
    cmd = call["cmd"]
    assert cmd[0] == "/fake/bin/claude"
    assert "-p" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--system-prompt") + 1] == "SYS"
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--model") + 1] == MODEL
    assert "--setting-sources=" in cmd
    assert "--strict-mcp-config" in cmd
    assert "--no-session-persistence" in cmd
    assert json.loads(cmd[cmd.index("--json-schema") + 1])["properties"].keys() == {"answer", "n"}
    assert "USER" not in cmd and call["input"] == "USER"
    assert OAUTH_SECRET not in " ".join(cmd)
    env = call["env"]
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_BASE_URL" not in env and "CLAUDECODE" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH_SECRET
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "777"
    assert Path(call["cwd"]).name and call["cwd"] != str(ROOT)

    p = price()
    expected = (1000 * p.input + 200 * p.output + 300 * p.cache_write + 400 * p.cache_read) / 1e6
    assert res.cost_usd == pytest.approx(expected)
    assert (res.input_tokens, res.output_tokens, res.cache_read) == (1300, 200, 400)
    assert json.loads(res.raw) == {"answer": "ok", "n": 3}
    assert redis.values[f"budget:extract:{TODAY}"] == pytest.approx(expected)
    assert redis.values[MONTH_KEY] == pytest.approx(expected)
    assert redis.ttl[f"budget:extract:{TODAY}"] == 8 * 86400
    assert redis.ttl[MONTH_KEY] == 40 * 86400


def test_claude_code_without_schema_returns_text(cli: FakeCli, redis: FakeRedis) -> None:
    cli.lines = [result_line(result="Y")]
    res = run(schema=None, task="classify")
    assert res.parsed == "Y" and res.raw == "Y"
    assert "--json-schema" not in cli.calls[0]["cmd"]


def test_claude_code_fenced_result_text_is_parsed(cli: FakeCli, redis: FakeRedis) -> None:
    cli.lines = [result_line(result='```json\n{"answer": "ok", "n": 1}\n```')]
    assert run().parsed == Out(answer="ok", n=1)


@pytest.mark.parametrize(
    "line",
    [
        result_line(result="not json at all"),  # JSON parse failure
        result_line(structured_output={"answer": "toolong", "n": 3}),  # max_length violated
        result_line(structured_output={"answer": "ok", "n": 11}),  # le violated
        result_line(subtype="error_max_structured_output_retries", is_error=True, result=""),
    ],
)
def test_claude_code_schema_failures_are_permanent_and_still_cost(
    cli: FakeCli, redis: FakeRedis, line: dict[str, Any]
) -> None:
    cli.lines = [line]
    with pytest.raises(PermanentError, match="schema") as ei:
        run(budget_key=f"budget:extract:{TODAY}")
    assert not isinstance(ei.value, BudgetExceeded)
    assert redis.values[f"budget:extract:{TODAY}"] > 0
    assert redis.values[MONTH_KEY] > 0


def test_claude_code_usage_limit_event(cli: FakeCli, redis: FakeRedis) -> None:
    cli.lines = [
        {
            "type": "rate_limit_event",
            "uuid": "u",
            "session_id": "s",
            "rate_limit_info": {"status": "rejected", "resetsAt": 1_800_000_000, "rateLimitType": "five_hour"},
        },
        {"type": "assistant", "message": {"content": []}, "error": "rate_limit"},
        result_line(is_error=True, result="You've hit your session limit · resets 3:45pm", usage=None),
    ]
    cli.returncode = 1
    with pytest.raises(UsageLimitReached) as ei:
        run()
    assert isinstance(ei.value, TransientError)
    assert ei.value.resets_at == datetime.fromtimestamp(1_800_000_000, tz=UTC)
    assert ei.value.limit_type == "five_hour"


@pytest.mark.parametrize(
    "text", ["You've hit your weekly limit · resets Mon 12:00am", "Claude AI usage limit reached", "session limit"]
)
def test_claude_code_usage_limit_text_only(cli: FakeCli, redis: FakeRedis, text: str) -> None:
    cli.lines = [result_line(is_error=True, subtype="success", result=text, usage=None)]
    cli.returncode = 1
    with pytest.raises(UsageLimitReached) as ei:
        run()
    assert ei.value.resets_at is None


@pytest.mark.parametrize(
    ("lines", "rc", "exc"),
    [
        ([result_line(is_error=True, result="API Error: 529 overloaded", api_error_status=529)], 1, TransientError),
        ([result_line(is_error=True, result="API Error: 500", api_error_status=500)], 1, TransientError),
        ([result_line(is_error=True, result="API Error: 401 Invalid authentication credentials",
                      api_error_status=401)], 1, PermanentError),
        ([{"type": "assistant", "message": {"content": []}, "error": "authentication_failed"},
          result_line(is_error=True, result="Login expired · Please run /login")], 1, PermanentError),
        ([result_line(is_error=True, result="API Error: 400 bad", api_error_status=400)], 1, PermanentError),
        ([result_line(is_error=True, subtype="error_during_execution", result="boom")], 1, TransientError),
        ([], 1, TransientError),  # crashed before emitting a result
        ([{"type": "system", "subtype": "init"}], 0, TransientError),  # exit 0 but no result object
    ],
)
def test_claude_code_failure_mapping(
    cli: FakeCli, redis: FakeRedis, lines: list[dict[str, Any]], rc: int, exc: type[Exception]
) -> None:
    cli.lines = lines
    cli.returncode = rc
    with pytest.raises(exc) as ei:
        run()
    assert not isinstance(ei.value, UsageLimitReached)
    if exc is TransientError:
        assert not isinstance(ei.value, PermanentError)


def test_claude_code_timeout_is_transient(cli: FakeCli, redis: FakeRedis) -> None:
    cli.exc = subprocess.TimeoutExpired(cmd="claude", timeout=1)
    with pytest.raises(TransientError):
        run()


def test_claude_code_missing_cli_is_permanent(
    cli: FakeCli, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> str:
        raise PermanentError("claude_code CLI not found")

    monkeypatch.setattr(cc_backend, "cli_path", missing)
    with pytest.raises(PermanentError):
        run()
    assert cli.calls == []


def test_claude_code_missing_usage_costs_zero_with_warning(
    cli: FakeCli, redis: FakeRedis, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.lines = [result_line(structured_output={"answer": "ok", "n": 3}, usage=None)]
    res = run()
    assert res.cost_usd == 0.0
    assert (res.input_tokens, res.output_tokens, res.cache_read) == (0, 0, 0)
    assert "llm_usage_missing" in capsys.readouterr().err


def test_cli_path_prefers_bundled_binary() -> None:
    path = Path(cc_backend.cli_path())
    assert path.name == "claude"


# --- api backend --------------------------------------------------------------------------------


def test_api_request_shape_and_cost(api: FakeMessages, redis: FakeRedis) -> None:
    api.response = api_message('{"answer": "ok", "n": 3}', cw=300, cr=400)
    res = run(max_tokens=777, temperature=0.0)
    kw = api.calls[0]
    assert kw["model"] == MODEL and kw["max_tokens"] == 777
    assert kw["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"] == [{"role": "user", "content": "USER"}]
    fmt = kw["output_config"]["format"]
    assert fmt["type"] == "json_schema" and set(fmt["schema"]["properties"]) == {"answer", "n"}
    assert "temperature" not in kw
    p = price()
    expected = (1000 * p.input + 200 * p.output + 300 * p.cache_write + 400 * p.cache_read) / 1e6
    assert res.cost_usd == pytest.approx(expected)
    assert res.cache_read == 400


def test_api_no_cache_and_no_schema(api: FakeMessages, redis: FakeRedis) -> None:
    api.response = api_message("Y")
    res = run(schema=None, cache_system=False, task="classify")
    kw = api.calls[0]
    assert kw["system"] == "SYS"
    assert "output_config" not in kw
    assert res.parsed == "Y"


def _req() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    return cls(f"status {code}", response=httpx2.Response(code, request=_req()), body=None)


@pytest.mark.parametrize(
    ("err", "exc"),
    [
        (_status(anthropic.RateLimitError, 429), TransientError),
        (_status(anthropic.InternalServerError, 500), TransientError),
        (_status(anthropic.ServiceUnavailableError, 503), TransientError),
        (_status(anthropic.OverloadedError, 529), TransientError),
        (anthropic.APIConnectionError(request=_req()), TransientError),
        (anthropic.APITimeoutError(request=_req()), TransientError),
        (_status(anthropic.BadRequestError, 400), PermanentError),
        (_status(anthropic.AuthenticationError, 401), PermanentError),
        (_status(anthropic.PermissionDeniedError, 403), PermanentError),
        (_status(anthropic.NotFoundError, 404), PermanentError),
    ],
)
def test_api_error_mapping(api: FakeMessages, redis: FakeRedis, err: Exception, exc: type[Exception]) -> None:
    api.exc = err
    with pytest.raises(exc) as ei:
        run()
    if exc is TransientError:
        assert not isinstance(ei.value, PermanentError)
    assert API_SECRET not in str(ei.value)


@pytest.mark.parametrize(
    "msg",
    [
        api_message("not json"),
        api_message('{"answer": "toolong", "n": 3}'),
        api_message('{"answer": "ok", "n": 3}', stop_reason="max_tokens"),
        api_message("I can't help with that.", stop_reason="refusal"),
    ],
)
def test_api_schema_failures_are_permanent_and_still_cost(api: FakeMessages, redis: FakeRedis, msg: _Message) -> None:
    api.response = msg
    with pytest.raises(PermanentError):
        run(budget_key=f"budget:extract:{TODAY}")
    assert redis.values[f"budget:extract:{TODAY}"] > 0


def test_api_missing_key_is_permanent(api: FakeMessages, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(PermanentError, match="anthropic_api_key"):
        run()
    assert api.calls == []


# --- budgets ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "key", "limit"),
    [
        ("extract", f"budget:extract:{TODAY}", 2.0),  # settings.budget_extract_daily
        ("judge", f"budget:judge:{TODAY}", 1.0),  # settings.budget_judge_daily
        ("editorial", "budget:report:rpt_1", 5.0),  # settings.budget_report_run
        ("classify", f"budget:classify:{TODAY}", 0.3),  # pricing.yaml budgets
        ("extract", f"budget:golden:{TODAY}", 2.0),
    ],
)
def test_budget_exhausted_blocks_before_backend(
    cli: FakeCli, redis: FakeRedis, task: str, key: str, limit: float
) -> None:
    redis.values[key] = limit
    with pytest.raises(BudgetExceeded):
        run(task=task, budget_key=key)
    assert cli.calls == []
    redis.values[key] = limit - 0.01
    run(task=task, budget_key=key)
    assert len(cli.calls) == 1


@pytest.mark.parametrize("key", [f"budget:nope:{TODAY}", f"extract:{TODAY}", "budget:extract", "budget::x"])
def test_unknown_budget_key_is_permanent(cli: FakeCli, redis: FakeRedis, key: str) -> None:
    with pytest.raises(PermanentError) as ei:
        run(budget_key=key)
    assert not isinstance(ei.value, BudgetExceeded)
    assert cli.calls == []


@pytest.mark.parametrize(("task", "blocked"), [("extract", True), ("classify", True), ("judge", False),
                                               ("editorial", False)])
def test_monthly_cap_blocks_only_extract_and_classify(
    cli: FakeCli, redis: FakeRedis, capsys: pytest.CaptureFixture[str], task: str, blocked: bool
) -> None:
    redis.values[MONTH_KEY] = 250.0
    if blocked:
        with pytest.raises(BudgetExceeded):
            run(task=task)
        assert cli.calls == []
    else:
        run(task=task)
        assert len(cli.calls) == 1
        assert "llm_monthly_cap_exceeded" in capsys.readouterr().err


def test_model_missing_from_pricing_is_permanent(cli: FakeCli, redis: FakeRedis) -> None:
    with pytest.raises(PermanentError, match="pricing"):
        run(model="some-unpriced-model")
    assert cli.calls == []


def test_no_budget_key_still_counts_month(cli: FakeCli, redis: FakeRedis) -> None:
    run()
    assert set(redis.values) == {MONTH_KEY}


# --- hygiene ------------------------------------------------------------------------------------


def test_model_literals_only_in_settings_and_pricing() -> None:
    allowed = {ROOT / "packages/domino_core/settings.py", ROOT / "packages/domino_core/pricing.yaml"}
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "packages").rglob("*")
        if p.is_file() and p.suffix in {".py", ".yaml", ".yml", ".md", ".json", ".toml", ".cypher", ".sql"}
        and p not in allowed and "claude-" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_secrets_never_logged(
    cli: FakeCli, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", API_SECRET)
    run()
    cli.lines = [result_line(is_error=True, result="API Error: 500", api_error_status=500)]
    cli.returncode = 1
    with pytest.raises(TransientError) as ei:
        run()
    err = capsys.readouterr().err
    assert "llm_call" in err
    for secret in (OAUTH_SECRET, API_SECRET):
        assert secret not in err
        assert secret not in str(ei.value)
