"""HTTP GET over httpx with a fixed User-Agent; 4xx→PermanentError, 429·5xx·timeout→TransientError."""

from __future__ import annotations

import httpx

from domino_core.errors import PermanentError, TransientError

USER_AGENT = "DominoBot/0.1"

_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(follow_redirects=True)
    return _client


def get(url: str, *, timeout: float, headers: dict[str, str] | None = None) -> tuple[int, str, dict[str, str]]:
    """GET `url` and return (status, text, headers). robots·domain semaphore·interval belong to the caller."""
    request_headers = {k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"}
    request_headers["User-Agent"] = USER_AGENT
    try:
        response = _get_client().get(url, headers=request_headers, timeout=timeout)
    except httpx.UnsupportedProtocol as e:
        raise PermanentError(f"http unsupported protocol: {url}") from e
    except httpx.TransportError as e:
        raise TransientError(f"http {type(e).__name__}: {url}") from e
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise PermanentError(f"http {type(e).__name__}: {url}") from e
    status = response.status_code
    if status == 429 or status >= 500:
        raise TransientError(f"http {status}: {url}")
    if status >= 400:
        raise PermanentError(f"http {status}: {url}")
    return status, response.text, dict(response.headers)
