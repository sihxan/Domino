"""domino_core.clients: error mapping and thin wrappers over mocked SDKs (no network, no Docker)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import neo4j.exceptions as neo4j_exc
import pytest
import redis as redis_sdk
import sqlalchemy as sa
import sqlalchemy.exc as sa_exc
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import SecretStr

from domino_core.clients import db, http, neo, s3
from domino_core.clients import redis as redis_client
from domino_core.errors import PermanentError, TransientError
from domino_core.settings import Settings


@pytest.fixture
def cfg() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        pg_dsn=SecretStr("postgresql+psycopg://u:p@localhost:5432/domino"),
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("neo4j-secret-value"),
        redis_url=SecretStr("redis://localhost:6379/0"),
        s3_endpoint="http://localhost:9000",
        s3_key=SecretStr("s3-key-value"),
        s3_secret=SecretStr("s3-secret-value"),
        langfuse_host="http://localhost:3000",
        langfuse_public_key="pk-test",
        langfuse_secret_key=SecretStr("langfuse-secret-value"),
        anthropic_api_key=SecretStr("anthropic-secret-value"),
        resend_api_key=SecretStr("resend-secret-value"),
        from_email="report@example.com",
    )


# --- http ---------------------------------------------------------------------------------------


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        result: httpx.Response = handler(request)
        return result

    monkeypatch.setattr(http, "_client", httpx.Client(transport=httpx.MockTransport(record)))
    return seen


def test_http_get_returns_status_text_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_http(monkeypatch, lambda r: httpx.Response(200, text="<html>ok</html>", headers={"X-Test": "1"}))
    status, text, headers = http.get("https://example.com/a", timeout=5)
    assert status == 200
    assert text == "<html>ok</html>"
    assert headers["x-test"] == "1"


def test_http_fixed_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _mock_http(monkeypatch, lambda r: httpx.Response(200, text=""))
    http.get("https://example.com/a", timeout=5)
    http.get("https://example.com/b", timeout=5, headers={"User-Agent": "other", "Accept": "text/html"})
    assert [r.headers["user-agent"] for r in seen] == [http.USER_AGENT, http.USER_AGENT]
    assert seen[1].headers["accept"] == "text/html"


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_http_5xx_is_transient(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    _mock_http(monkeypatch, lambda r: httpx.Response(status))
    with pytest.raises(TransientError):
        http.get("https://example.com/a", timeout=5)


def test_http_429_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_http(monkeypatch, lambda r: httpx.Response(429))
    with pytest.raises(TransientError):
        http.get("https://example.com/a", timeout=5)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_http_4xx_is_permanent(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    _mock_http(monkeypatch, lambda r: httpx.Response(status))
    with pytest.raises(PermanentError):
        http.get("https://example.com/a", timeout=5)


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("t"), httpx.ConnectTimeout("t"), httpx.ConnectError("refused")])
def test_http_timeout_and_network_are_transient(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise exc

    _mock_http(monkeypatch, boom)
    with pytest.raises(TransientError):
        http.get("https://example.com/a", timeout=5)


def test_http_unsupported_protocol_is_permanent() -> None:
    with pytest.raises(PermanentError):
        http.get("ftp://example.com/a", timeout=5)


# --- s3 -----------------------------------------------------------------------------------------


def _client_error(code: str, status: int, op: str = "HeadObject") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, op)


@pytest.fixture
def boto() -> Iterator[MagicMock]:
    with patch("domino_core.clients.s3.boto3.client") as factory:
        factory.return_value = MagicMock()
        yield factory


def test_s3_client_uses_settings(cfg: Settings, boto: MagicMock) -> None:
    s3.get_s3(cfg)
    kwargs = boto.call_args.kwargs
    assert boto.call_args.args == ("s3",)
    assert kwargs["endpoint_url"] == "http://localhost:9000"
    assert kwargs["aws_access_key_id"] == "s3-key-value"
    assert kwargs["aws_secret_access_key"] == "s3-secret-value"


def test_put_immutable_writes_new_key(cfg: Settings, boto: MagicMock) -> None:
    client = boto.return_value
    client.head_object.side_effect = _client_error("404", 404)
    assert s3.put_immutable("raw/2026/11/doc_1.html", b"<html/>", settings=cfg) is True
    client.put_object.assert_called_once_with(
        Bucket="domino", Key="raw/2026/11/doc_1.html", Body=b"<html/>", IfNoneMatch="*"
    )


def test_put_immutable_does_not_overwrite_existing_key(cfg: Settings, boto: MagicMock) -> None:
    client = boto.return_value
    client.head_object.return_value = {"ContentLength": 7}
    assert s3.put_immutable("raw/2026/11/doc_1.html", b"<other/>", settings=cfg) is False
    client.put_object.assert_not_called()


def test_put_immutable_lost_race_does_not_overwrite(cfg: Settings, boto: MagicMock) -> None:
    client = boto.return_value
    client.head_object.side_effect = _client_error("404", 404)
    client.put_object.side_effect = _client_error("PreconditionFailed", 412, "PutObject")
    assert s3.put_immutable("raw/2026/11/doc_1.html", b"<html/>", settings=cfg) is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_client_error("SlowDown", 503), TransientError),
        (_client_error("InternalError", 500), TransientError),
        (_client_error("AccessDenied", 403), PermanentError),
        (EndpointConnectionError(endpoint_url="http://localhost:9000"), TransientError),
    ],
)
def test_s3_error_mapping(cfg: Settings, boto: MagicMock, error: Exception, expected: type[Exception]) -> None:
    boto.return_value.head_object.side_effect = error
    with pytest.raises(expected):
        s3.put_immutable("k", b"v", settings=cfg)


# --- neo ----------------------------------------------------------------------------------------


def _driver() -> tuple[MagicMock, MagicMock]:
    driver = MagicMock()
    session = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    return driver, session


def test_neo_driver_uses_settings(cfg: Settings) -> None:
    with patch("domino_core.clients.neo.GraphDatabase.driver") as factory:
        neo.get_driver(cfg)
    assert factory.call_args.args == ("bolt://localhost:7687",)
    assert factory.call_args.kwargs["auth"] == ("neo4j", "neo4j-secret-value")


def test_run_cypher_file_splits_on_semicolon(tmp_path: Path) -> None:
    path = tmp_path / "0001_schema.cypher"
    path.write_text(
        "// constraints\n"
        "CREATE CONSTRAINT a IF NOT EXISTS FOR (n:A) REQUIRE n.id IS UNIQUE;\n"
        "\n"
        "CREATE INDEX b IF NOT EXISTS FOR (n:B) ON (n.t);\n"
        "CREATE VECTOR INDEX c IF NOT EXISTS FOR (n:C) ON (n.embedding)\n"
        "  OPTIONS { indexConfig: { `vector.dimensions`: 1024 } };\n"
        "\n",
        encoding="utf-8",
    )
    driver, session = _driver()
    assert neo.run_cypher_file(path, driver=driver) == 3
    statements = [c.args[0] for c in session.run.call_args_list]
    assert statements == [
        "CREATE CONSTRAINT a IF NOT EXISTS FOR (n:A) REQUIRE n.id IS UNIQUE",
        "CREATE INDEX b IF NOT EXISTS FOR (n:B) ON (n.t)",
        "CREATE VECTOR INDEX c IF NOT EXISTS FOR (n:C) ON (n.embedding)\n"
        "  OPTIONS { indexConfig: { `vector.dimensions`: 1024 } }",
    ]
    assert session.run.return_value.consume.call_count == 3


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (neo4j_exc.ServiceUnavailable("down"), TransientError),  # type: ignore[no-untyped-call]
        (neo4j_exc.SessionExpired("gone"), TransientError),  # type: ignore[no-untyped-call]
        (neo4j_exc.CypherSyntaxError("bad"), PermanentError),
    ],
)
def test_run_cypher_file_error_mapping(tmp_path: Path, error: Exception, expected: type[Exception]) -> None:
    path = tmp_path / "x.cypher"
    path.write_text("RETURN 1;", encoding="utf-8")
    driver, session = _driver()
    session.run.side_effect = error
    with pytest.raises(expected):
        neo.run_cypher_file(path, driver=driver)


def test_run_cypher_file_missing_is_permanent(tmp_path: Path) -> None:
    driver, _ = _driver()
    with pytest.raises(PermanentError):
        neo.run_cypher_file(tmp_path / "missing.cypher", driver=driver)


# --- db -----------------------------------------------------------------------------------------


def test_db_engine_uses_pg_dsn(cfg: Settings) -> None:
    with patch.object(db, "create_engine") as factory:
        db.get_engine(cfg)
    assert factory.call_args.args == ("postgresql+psycopg://u:p@localhost:5432/domino",)
    assert factory.call_args.kwargs["hide_parameters"] is True


def test_engine_hides_parameters(cfg: Settings) -> None:
    """L4: SQLAlchemy errors must not carry bind values (document text) in their message."""
    real_create_engine = sa.create_engine
    with patch.object(db, "create_engine", lambda url, **kw: real_create_engine("sqlite://", **kw)):
        engine = db.get_engine(cfg)
    secret = "BODY-BODY-secret-document-text"
    with pytest.raises(sa_exc.StatementError) as info, engine.connect() as conn:
        conn.execute(sa.text("SELECT :v"), {"v": [secret]})  # sqlite cannot bind a list
    assert secret not in str(info.value)
    assert "[parameters:" not in str(info.value)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (sa_exc.OperationalError("SELECT 1", {}, Exception("conn refused")), TransientError),
        (sa_exc.IntegrityError("INSERT", {}, Exception("dup")), PermanentError),
    ],
)
def test_db_connection_error_mapping(error: Exception, expected: type[Exception]) -> None:
    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value.execute.side_effect = error
    with pytest.raises(expected), db.connection(engine=engine) as conn:
        conn.execute(MagicMock())


# --- redis --------------------------------------------------------------------------------------


def test_redis_client_uses_redis_url(cfg: Settings) -> None:
    with patch("domino_core.clients.redis.redis.Redis.from_url") as factory:
        redis_client.get_redis(cfg)
    assert factory.call_args.args == ("redis://localhost:6379/0",)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (redis_sdk.ConnectionError("down"), TransientError),
        (redis_sdk.TimeoutError("slow"), TransientError),
        (redis_sdk.ResponseError("WRONGTYPE"), PermanentError),
    ],
)
def test_redis_error_mapping(error: Exception, expected: type[Exception]) -> None:
    with pytest.raises(expected), redis_client.redis_errors():
        raise error
