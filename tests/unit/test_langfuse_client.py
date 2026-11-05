"""domino_core.clients.langfuse: one Langfuse v2 generation per llm.call, never breaking the call (W2-7, all mocked)."""

from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from langfuse import Langfuse
from pydantic import BaseModel, SecretStr

from domino_core.clients import langfuse as lf
from domino_core.clients import llm
from domino_core.clients.llm_backends import BackendReply, BackendRequest, Usage
from domino_core.errors import PermanentError, TransientError
from domino_core.settings import Settings, get_settings

SECRET = "sk-lf-secret-value-xyz"
PUBLIC = "pk-lf-public-test"
HOST = "http://langfuse.test:3000"
MODEL = "claude-haiku-5-5"  # test data; production code reads model names from settings only

BASE_ENV = {
    "PG_DSN": "postgresql+psycopg://u:p@localhost:5432/domino",
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "x",
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_ENDPOINT": "http://localhost:9000",
    "S3_KEY": "k",
    "S3_SECRET": "s",
    "LANGFUSE_HOST": HOST,
    "LANGFUSE_PUBLIC_KEY": PUBLIC,
    "LANGFUSE_SECRET_KEY": SECRET,
    "RESEND_API_KEY": "r",
    "FROM_EMAIL": "a@example.com",
}


class Out(BaseModel):
    answer: str
    n: int


# --- fakes --------------------------------------------------------------------------------------


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, float] = {}

    def get(self, key: str) -> str | None:
        return str(self.values[key]) if key in self.values else None

    def incrbyfloat(self, key: str, amount: float) -> float:
        self.values[key] = self.values.get(key, 0.0) + amount
        return self.values[key]

    def expire(self, key: str, seconds: int) -> bool:
        return True


@dataclass
class FakeTrace:
    id: str
    kwargs: dict[str, Any]
    generations: list[dict[str, Any]] = field(default_factory=list)

    def generation(self, **kw: Any) -> object:
        self.generations.append(kw)
        return object()


@dataclass
class FakeLangfuse:
    """Stands in for `langfuse.Langfuse`: records traces and their generations."""

    raise_on_trace: BaseException | None = None
    traces: list[FakeTrace] = field(default_factory=list)
    flushed: int = 0

    def trace(self, **kw: Any) -> FakeTrace:
        if self.raise_on_trace is not None:
            raise self.raise_on_trace
        t = FakeTrace(id=f"tr-{len(self.traces) + 1}", kwargs=kw)
        self.traces.append(t)
        return t

    def flush(self) -> None:
        self.flushed += 1


@dataclass
class FakeBackend:
    reply: BackendReply | None = None
    exc: BaseException | None = None
    calls: list[BackendRequest] = field(default_factory=list)

    def __call__(self, req: BackendRequest, settings: Settings) -> BackendReply:
        self.calls.append(req)
        if self.exc is not None:
            raise self.exc
        assert self.reply is not None
        return self.reply


# --- fixtures -----------------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in ["LLM_BACKEND", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)  # Settings reads `.env` from cwd; the repo's real one must not leak in
    monkeypatch.setattr(lf, "_clients", {})


@pytest.fixture
def fake_lf(monkeypatch: pytest.MonkeyPatch, env: None) -> FakeLangfuse:
    fake = FakeLangfuse()
    made: list[tuple[str, str]] = []

    def new_client(host: str, public_key: str, secret_key: str) -> FakeLangfuse:
        made.append((host, public_key))
        return fake

    monkeypatch.setattr(lf, "_new_client", new_client)
    return fake


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, env: None) -> FakeBackend:
    fake = FakeBackend(reply=BackendReply('{"answer": "ok", "n": 3}', Usage(1000, 200, 300, 400), "end_turn"))
    monkeypatch.setitem(llm.BACKENDS, "claude_code", fake)
    redis = FakeRedis()
    monkeypatch.setattr(llm, "get_redis", lambda: redis)
    return fake


def run(**kw: Any) -> llm.LLMResult[Any]:
    args: dict[str, Any] = {
        "task": "extract",
        "model": MODEL,
        "system": "SYS",
        "user": "USER",
        "schema": Out,
        "budget_key": "budget:extract:2026-11-05",
        "trace": {"prompt_version": "ext-1.0", "chunk_id": "ch_1"},
    }
    args.update(kw)
    return llm.call(**args)


# --- llm.call → one generation ------------------------------------------------------------------


def test_successful_call_sends_one_generation_with_tags(fake_lf: FakeLangfuse, backend: FakeBackend) -> None:
    res = run()
    assert res.parsed == Out(answer="ok", n=3)

    assert len(fake_lf.traces) == 1
    t = fake_lf.traces[0]
    assert t.kwargs["name"] == "extract"
    assert set(t.kwargs["tags"]) == {"task=extract", "prompt_version=ext-1.0"}
    assert len(t.generations) == 1
    g = t.generations[0]
    assert g["name"] == "extract"
    assert g["model"] == MODEL
    assert g["version"] == "ext-1.0"
    assert g["level"] == "DEFAULT"
    assert g["metadata"]["prompt_version"] == "ext-1.0"
    assert g["metadata"]["task"] == "extract"
    assert g["metadata"]["chunk_id"] == "ch_1"
    assert g["metadata"]["backend"] == "claude_code"
    assert g["metadata"]["cache_write"] == 300 and g["metadata"]["cache_read"] == 400
    # tokens: input is the whole prompt (uncached + cache write + cache read), cost is the API-equivalent cost
    assert g["usage"] == {
        "unit": "TOKENS",
        "input": 1700,
        "output": 200,
        "total": 1900,
        "total_cost": pytest.approx(res.cost_usd),
    }
    assert res.cost_usd > 0
    assert g["input"] == [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]
    assert g["output"] == '{"answer": "ok", "n": 3}'
    assert isinstance(g["start_time"], datetime) and isinstance(g["end_time"], datetime)
    assert g["end_time"] >= g["start_time"]


def test_trace_none_is_allowed(fake_lf: FakeLangfuse, backend: FakeBackend) -> None:
    run(trace=None, schema=None)
    t = fake_lf.traces[0]
    assert t.kwargs["tags"] == ["task=extract"]
    g = t.generations[0]
    assert g["version"] is None
    assert g["metadata"]["prompt_version"] is None
    assert g["metadata"]["task"] == "extract"


def test_schema_failure_is_traced_as_error_with_cost(fake_lf: FakeLangfuse, backend: FakeBackend) -> None:
    backend.reply = BackendReply("not json", Usage(1000, 200), "end_turn")
    with pytest.raises(PermanentError):
        run()
    g = fake_lf.traces[0].generations[0]
    assert g["level"] == "ERROR"
    assert g["status_message"].startswith("PermanentError: schema:")
    assert g["usage"]["total_cost"] > 0
    assert g["output"] == "not json"


def test_backend_failure_is_traced_as_error_without_usage(fake_lf: FakeLangfuse, backend: FakeBackend) -> None:
    backend.exc = TransientError("api 529")
    with pytest.raises(TransientError):
        run()
    g = fake_lf.traces[0].generations[0]
    assert g["level"] == "ERROR"
    assert g["status_message"] == "TransientError: api 529"
    assert g["usage"] == {"unit": "TOKENS", "input": 0, "output": 0, "total": 0, "total_cost": 0.0}
    assert g["output"] is None


# --- Langfuse failures never reach the caller ----------------------------------------------------


def test_langfuse_exception_logs_warning_and_call_returns(
    fake_lf: FakeLangfuse, backend: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_lf.raise_on_trace = RuntimeError(f"connection refused (auth {SECRET})")
    res = run()
    assert res.parsed == Out(answer="ok", n=3)
    err = capsys.readouterr().err
    lines = [json.loads(line) for line in err.splitlines() if line.strip()]
    warn = [x for x in lines if x["event"] == "langfuse_trace_failed"]
    assert len(warn) == 1 and warn[0]["level"] == "warning"
    assert warn[0]["error"] == "RuntimeError"
    assert SECRET not in err


def test_client_construction_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, backend: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(host: str, public_key: str, secret_key: str) -> FakeLangfuse:
        raise ValueError(f"bad credentials {secret_key}")

    monkeypatch.setattr(lf, "_new_client", broken)
    res = run()
    assert res.parsed == Out(answer="ok", n=3)
    err = capsys.readouterr().err
    assert "langfuse_trace_failed" in err
    assert SECRET not in err


def test_secret_key_never_logged(
    fake_lf: FakeLangfuse, backend: FakeBackend, capsys: pytest.CaptureFixture[str]
) -> None:
    run()
    backend.exc = TransientError("boom")
    with pytest.raises(TransientError):
        run()
    fake_lf.raise_on_trace = RuntimeError(SECRET)
    backend.exc = None
    run()
    err = capsys.readouterr().err
    assert "llm_call" in err and "langfuse_trace_failed" in err
    assert SECRET not in err


# --- disabled when unconfigured (tests, CI) -------------------------------------------------------


@pytest.mark.parametrize("field_name", ["langfuse_host", "langfuse_public_key", "langfuse_secret_key"])
def test_missing_host_or_keys_skips_silently(
    monkeypatch: pytest.MonkeyPatch, backend: FakeBackend, capsys: pytest.CaptureFixture[str], field_name: str
) -> None:
    monkeypatch.setattr(lf, "_new_client", lambda *a: pytest.fail("no client without host and keys"))
    blank: Any = SecretStr("") if field_name == "langfuse_secret_key" else "  "
    settings = get_settings().model_copy(update={field_name: blank})
    monkeypatch.setattr(lf, "default_settings", lambda: settings)
    res = run()
    assert res.parsed == Out(answer="ok", n=3)
    assert lf.trace_generation("extract", MODEL, "in", "out", Usage(1, 1), 0.0, 5, {}) is None
    err = capsys.readouterr().err
    assert "langfuse" not in err  # silently: no warning


def test_client_is_built_once_per_configuration(monkeypatch: pytest.MonkeyPatch, env: None) -> None:
    made: list[str] = []

    def new_client(host: str, public_key: str, secret_key: str) -> FakeLangfuse:
        made.append(host)
        return FakeLangfuse()

    monkeypatch.setattr(lf, "_new_client", new_client)
    for _ in range(3):
        assert lf.trace_generation("extract", MODEL, "in", "out", Usage(1, 1), 0.0, 5, {}) is not None
    assert made == [HOST]


def test_flush_flushes_built_clients(fake_lf: FakeLangfuse) -> None:
    lf.trace_generation("extract", MODEL, "in", "out", Usage(1, 1), 0.0, 5, {})
    lf.flush()
    assert fake_lf.flushed == 1


# --- the real SDK 2.x against a mock transport (no network) ---------------------------------------


def test_real_sdk_posts_trace_and_generation(monkeypatch: pytest.MonkeyPatch, env: None) -> None:
    batches: list[dict[str, Any]] = []
    auth: list[str] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            auth.append(request.headers.get("authorization", ""))
            if request.url.path == "/api/public/ingestion":
                batches.append(json.loads(request.content))
        return httpx.Response(207, json={"successes": [], "errors": []})

    def new_client(host: str, public_key: str, secret_key: str) -> Any:
        return Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            httpx_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=1,
        )

    monkeypatch.setattr(lf, "_new_client", new_client)
    trace_id = lf.trace_generation(
        "extract",
        MODEL,
        [{"role": "user", "content": "USER"}],
        '{"answer": "ok"}',
        Usage(1000, 200, 0, 0),
        0.0123,
        321,
        {"prompt_version": "ext-1.0"},
    )
    lf.flush()
    for c in lf._clients.values():
        c.shutdown()

    assert trace_id
    events = [e for b in batches for e in b["batch"]]
    traces = [e["body"] for e in events if e["type"] == "trace-create"]
    gens = [e["body"] for e in events if e["type"] == "generation-create"]
    assert len(traces) == 1 and len(gens) == 1
    assert traces[0]["id"] == trace_id
    assert set(traces[0]["tags"]) == {"task=extract", "prompt_version=ext-1.0"}
    g = gens[0]
    assert g["traceId"] == trace_id and g["name"] == "extract" and g["model"] == MODEL
    assert g["version"] == "ext-1.0"
    assert g["usage"]["input"] == 1000 and g["usage"]["output"] == 200 and g["usage"]["total"] == 1200
    assert g["usage"]["totalCost"] == pytest.approx(0.0123)
    assert g["metadata"]["prompt_version"] == "ext-1.0" and g["metadata"]["task"] == "extract"
    expected = "Basic " + base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()
    assert auth and all(a == expected for a in auth)
