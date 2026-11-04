"""Claude Code headless backend: one CLI run on the Claude subscription, no tools, JSON out (decisions T7)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from domino_core.clients.llm_backends import BackendReply, BackendRequest, Usage
from domino_core.errors import PermanentError, TransientError, UsageLimitReached
from domino_core.settings import Settings

CLAUDE_CODE_TIMEOUT_S = 300
CLAUDE_CODE_MAX_TURNS = 3  # --json-schema answers through a synthetic StructuredOutput tool call, plus retries
# Only these are inherited: ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL would take precedence over
# the subscription (or reroute it), so the CLI never sees the ambient environment.
PASSTHROUGH_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "CLAUDE_CONFIG_DIR")
STRUCTURED_RETRIES_EXHAUSTED = "error_max_structured_output_retries"

_LIMIT_TEXT = re.compile(r"hit your [\w ]{0,20}limit|usage limit|session limit|weekly limit", re.IGNORECASE)
_AUTH_TEXT = re.compile(r"/login|login expired|oauth token|invalid authentication|not logged in", re.IGNORECASE)
_PERMANENT_ERRORS = {"authentication_failed", "billing_error", "invalid_request"}
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*)\n\s*```\s*$", re.DOTALL)


def cli_path() -> str:
    """The CLI bundled in the pinned claude_agent_sdk wheel, else `claude` on PATH."""
    spec = importlib.util.find_spec("claude_agent_sdk")
    for location in (spec.submodule_search_locations or []) if spec else []:
        bundled = Path(location) / "_bundled" / "claude"
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    found = shutil.which("claude")
    if found:
        return found
    raise PermanentError("claude_code CLI not found (install the claude_agent_sdk wheel or put `claude` on PATH)")


def _run_cli(
    cmd: list[str], *, input: str, env: dict[str, str], cwd: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # fixed argv, no shell
        cmd, input=input, env=env, cwd=cwd, timeout=timeout, capture_output=True, text=True, check=False
    )


def build_command(req: BackendRequest, cli: str) -> list[str]:
    """argv for one print-mode run; the user message goes on stdin, never argv."""
    cmd = [
        cli,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--system-prompt",
        req.system,
        "--tools",
        "",
        "--model",
        req.model,
        "--setting-sources=",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--max-turns",
        str(CLAUDE_CODE_MAX_TURNS),
    ]
    if req.schema is not None:
        cmd += ["--json-schema", json.dumps(req.schema.model_json_schema())]
    return cmd


def build_env(req: BackendRequest, settings: Settings) -> dict[str, str]:
    """Allow-listed environment for the CLI (see PASSTHROUGH_ENV) plus the subscription token if configured."""
    env = {k: v for k in PASSTHROUGH_ENV if (v := os.environ.get(k))}
    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(req.max_tokens)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    if settings.claude_code_oauth_token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = settings.claude_code_oauth_token.get_secret_value()
    return env


def run(req: BackendRequest, settings: Settings) -> BackendReply:
    """Run the CLI once and turn its stream-json output into a reply or a Domino error."""
    cmd = build_command(req, cli_path())
    env = build_env(req, settings)
    with tempfile.TemporaryDirectory(prefix="domino-llm-") as cwd:  # no CLAUDE.md / project settings to pick up
        try:
            proc = _run_cli(cmd, input=req.user, env=env, cwd=cwd, timeout=CLAUDE_CODE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise TransientError(f"claude_code timed out after {CLAUDE_CODE_TIMEOUT_S}s") from None
        except OSError as e:
            raise PermanentError(f"claude_code cannot start the CLI: {type(e).__name__}") from None
    events = _events(proc.stdout)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None or result.get("is_error"):
        if result is not None and result.get("subtype") == STRUCTURED_RETRIES_EXHAUSTED:
            error = f"schema: {STRUCTURED_RETRIES_EXHAUSTED}"
            return BackendReply("", _usage(result), result.get("stop_reason"), error)
        raise _failure(events, result, proc.returncode)
    return BackendReply(_text(req, result), _usage(result), result.get("stop_reason"))


def _events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # non-JSON noise; the result object decides success
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _text(req: BackendRequest, result: dict[str, Any]) -> str:
    text = result.get("result")
    text = text if isinstance(text, str) else ""
    if req.schema is None:
        return text
    if result.get("structured_output") is not None:
        return json.dumps(result["structured_output"], ensure_ascii=False)
    fenced = _FENCE.match(text)
    return fenced.group(1) if fenced else text


def _usage(result: dict[str, Any]) -> Usage | None:
    raw = result.get("usage")
    if not isinstance(raw, dict):
        return None

    def n(key: str) -> int:
        value = raw.get(key)
        return int(value) if isinstance(value, int | float) else 0

    u = Usage(n("input_tokens"), n("output_tokens"), n("cache_creation_input_tokens"), n("cache_read_input_tokens"))
    return None if u == Usage(0, 0, 0, 0) else u


def _failure(events: list[dict[str, Any]], result: dict[str, Any] | None, returncode: int) -> Exception:
    """Map a failed run to UsageLimitReached / TransientError / PermanentError (decisions T7, 응답 해석)."""
    rejected = next(
        (
            e["rate_limit_info"]
            for e in events
            if e.get("type") == "rate_limit_event"
            and isinstance(e.get("rate_limit_info"), dict)
            and e["rate_limit_info"].get("status") == "rejected"
        ),
        None,
    )
    errors = {str(e["error"]) for e in events if e.get("type") == "assistant" and e.get("error")}
    texts = [str(result.get("result") or "")] if result else []
    texts += [str(x) for x in (result.get("errors") or [])] if result else []
    text = " | ".join(t for t in texts if t)[:300]
    status = result.get("api_error_status") if result else None
    where = f"claude_code run failed (exit {returncode}, subtype {result.get('subtype') if result else None})"

    if rejected is not None or "rate_limit" in errors or _LIMIT_TEXT.search(text):
        info = rejected or {}
        resets = info.get("resetsAt")
        return UsageLimitReached(
            f"{where}: subscription usage limit: {text}",
            resets_at=datetime.fromtimestamp(resets, tz=UTC) if isinstance(resets, int | float) else None,
            limit_type=info.get("rateLimitType"),
        )
    if isinstance(status, int):
        if status == 429 or status >= 500:
            return TransientError(f"{where}: API {status}: {text}")
        return PermanentError(f"{where}: API {status}: {text}")
    if errors & _PERMANENT_ERRORS or _AUTH_TEXT.search(text):
        return PermanentError(f"{where}: {sorted(errors)}: {text}")
    if result is None:
        return TransientError(f"{where}: no result object")
    return TransientError(f"{where}: {sorted(errors)}: {text}")
