"""minio (S3) client via boto3 and immutable puts: an existing key is never overwritten."""

from __future__ import annotations

from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from domino_core.clients import default_settings
from domino_core.errors import PermanentError, TransientError
from domino_core.settings import Settings

_TRANSIENT_CODES = {"SlowDown", "Throttling", "ThrottlingException", "RequestTimeout", "ServiceUnavailable"}

_client: Any = None


def get_s3(settings: Settings | None = None) -> Any:
    """boto3 S3 client for `settings.s3_endpoint`; with no argument, a process-wide default."""
    global _client
    if settings is None and _client is not None:
        return _client
    s = settings or default_settings()
    client = boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint,
        aws_access_key_id=s.s3_key.get_secret_value(),
        aws_secret_access_key=s.s3_secret.get_secret_value(),
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    if settings is None:
        _client = client
    return client


def _status(e: ClientError) -> int:
    meta: dict[str, Any] = e.response.get("ResponseMetadata", {})
    return int(meta.get("HTTPStatusCode", 0))


def _code(e: ClientError) -> str:
    return str(e.response.get("Error", {}).get("Code", ""))


def _map(e: Exception, key: str) -> Exception:
    if isinstance(e, ClientError):
        status, code = _status(e), _code(e)
        if status >= 500 or status == 429 or code in _TRANSIENT_CODES:
            return TransientError(f"s3 {code or status}: {key}")
        return PermanentError(f"s3 {code or status}: {key}")
    return TransientError(f"s3 {type(e).__name__}: {key}")


def put_immutable(key: str, body: bytes, *, settings: Settings | None = None) -> bool:
    """Write `body` at `key` only if absent. Returns True if written, False if the key already existed."""
    s = settings or default_settings()
    client = get_s3(settings)
    try:
        client.head_object(Bucket=s.s3_bucket, Key=key)
        return False
    except ClientError as e:
        if _status(e) != 404 and _code(e) not in {"404", "NoSuchKey", "NotFound"}:
            raise _map(e, key) from e
    except BotoCoreError as e:
        raise _map(e, key) from e
    try:
        client.put_object(Bucket=s.s3_bucket, Key=key, Body=body, IfNoneMatch="*")
    except ClientError as e:
        if _status(e) == 412 or _code(e) == "PreconditionFailed":
            return False
        raise _map(e, key) from e
    except BotoCoreError as e:
        raise _map(e, key) from e
    return True
