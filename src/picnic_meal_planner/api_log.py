"""Best-effort logging of calls to external APIs.

Two providers are logged: Picnic (``picnic/client.py`` and ``picnic/auth.py``)
and OpenRouter (``llm.create_message``).  Instrumentation sits at each
provider's single choke point rather than at every call site, so the bot, the
MCP server, the import script and preflight are all covered without touching
them.

What is stored
--------------
A row holds the operation, target, timing, outcome, error, token counts and
the **JSON request and response bodies**, so a surprising call can be
reproduced.  Headers are never stored (they carry the OpenRouter API key and
the Picnic session token), and credential fields inside a body — password,
2FA code, token, API key, secret — are replaced with ``[REDACTED]`` before the
body is written.  Everything else is verbatim.

The table therefore contains prompt, tool-result and order data.  Treat the
database file as sensitive, and prune the log with
``db.queries.prune_api_requests()``.  Bodies are capped by
``API_LOG_MAX_BODY_CHARS`` (default 200 000 chars per body) and can be turned
off entirely with ``API_LOG_BODIES=false``; when off, only metadata is kept.

Policy
------
- **Best effort.**  Persisting the log must never change the outcome of the
  request being logged: every failure here is a warning, never an exception.
  This follows ``history.py``'s read/write degradation, not its deletion
  behaviour — a log is not something a user asked to be durable.
- **Await, do not detach.**  The insert is awaited so the row is committed
  before the caller returns (and before a script claims its run succeeded).
  Losing rows to a fire-and-forget task would be worse than one small INSERT.
- **Bounded.**  Bodies are truncated and ``error_message`` is capped;
  ``prune_api_requests()`` provides retention.

``request_context()`` adds ambient attribution: everything logged inside the
block shares a ``source`` and ``run_id``, which is how one
``scripts/import_history.py`` run is grouped in the table.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .db import queries as db

logger = logging.getLogger(__name__)

PROVIDER_PICNIC = "picnic"
PROVIDER_OPENROUTER = "openrouter"

REDACTED = "[REDACTED]"

# Per-body cap: a request log must not let one large call balloon the DB.
# Applied after JSON encoding, before the insert.
MAX_BODY_CHARS = int(os.getenv("API_LOG_MAX_BODY_CHARS", "200000"))
BODIES_ENABLED = os.getenv("API_LOG_BODIES", "true").strip().lower() != "false"

_MAX_OPERATION = 200
_MAX_TARGET = 500
_MAX_ERROR = 500

# Exact key names that hold credentials.  Matched case-insensitively.  This is
# deliberately a short, exact list: a broad match on "code" or "token" would
# hide ordinary payload fields that are useful for debugging.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "auth_token",
        "access_token",
        "refresh_token",
        "token",
        "api_key",
        "apikey",
        "secret",
        "authorization",
    }
)


@dataclass(frozen=True)
class _Context:
    source: str
    run_id: str


_context: contextvars.ContextVar[_Context | None] = contextvars.ContextVar(
    "api_log_context", default=None
)


@dataclass(frozen=True)
class Timer:
    """When a request started, and how long it has been running."""

    started_perf: float
    requested_at: str

    @classmethod
    def start(cls) -> Timer:
        return cls(
            started_perf=time.perf_counter(),
            requested_at=datetime.now(UTC).isoformat(),
        )

    @property
    def duration_ms(self) -> int:
        return max(0, round((time.perf_counter() - self.started_perf) * 1000))


@dataclass
class Pending:
    """The bodies of a request in progress, filled in by the caller.

    ``api_request()`` yields one of these.  Set ``response_body`` after a
    successful call; the context manager records whatever is present, whether
    the block returns or raises.
    """

    request_body: Any = None
    response_body: Any = None


def call_body(*args: Any, **kwargs: Any) -> dict:
    """The request payload of a wrapper call, ready for the log."""
    return {"args": list(args), "kwargs": kwargs}


def error_response_body(error: BaseException | None) -> Any:
    """Best-effort response payload carried by an SDK/transport exception."""
    if error is None:
        return None
    body = getattr(error, "body", None)
    if body is not None:
        return body
    response = getattr(error, "response", None)
    if response is None:
        return None
    try:
        return response.json()
    except Exception:  # noqa: BLE001 — not JSON, or already consumed
        text = getattr(response, "text", None)
        return text if isinstance(text, str) else None


@asynccontextmanager
async def request_context(
    source: str, *, run_id: str | None = None
) -> AsyncIterator[str]:
    """Attribute every request logged inside the block to *source*/*run_id*.

    Yields the run id (generated when not given).  Contexts nest: the inner
    block wins, and the outer one resumes when it exits.
    """
    ctx = _Context(source=source, run_id=run_id or uuid.uuid4().hex)
    token = _context.set(ctx)
    try:
        yield ctx.run_id
    finally:
        _context.reset(token)


@asynccontextmanager
async def api_request(
    provider: str,
    operation: str,
    *,
    target: str | None = None,
    request_body: Any = None,
    capture_response: bool = True,
) -> AsyncIterator[Pending]:
    """Log one request, whether it succeeds or raises.

    The caller may set ``response_body`` on the yielded :class:`Pending`
    before the block exits.  Pass ``capture_response=False`` for a call whose
    response carries a credential (the Picnic login), so no response body is
    recorded even on failure.  Exceptions are re-raised unchanged; the log
    entry is written before they propagate.
    """
    timer = Timer.start()
    entry = Pending(request_body=request_body)
    try:
        yield entry
    except Exception as exc:  # logged, then re-raised unchanged
        response = None
        if capture_response:
            response = (
                entry.response_body
                if entry.response_body is not None
                else error_response_body(exc)
            )
        await record(
            provider,
            operation,
            timer,
            target=target,
            request_body=entry.request_body,
            response_body=response,
            error=exc,
        )
        raise
    else:
        await record(
            provider,
            operation,
            timer,
            target=target,
            request_body=entry.request_body,
            response_body=entry.response_body if capture_response else None,
        )


async def record(
    provider: str,
    operation: str,
    timer: Timer,
    *,
    target: str | None = None,
    error: BaseException | None = None,
    http_status: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    request_body: Any = None,
    response_body: Any = None,
) -> None:
    """Persist one log row; never raises.

    Callers that need a request's *usage* (OpenRouter token counts) or its raw
    completion call this directly; everything else should use
    :func:`api_request`.
    """
    ctx = _context.get()
    try:
        async with db.get_db() as session:
            await db.insert_api_request(
                session,
                provider=provider,
                operation=operation[:_MAX_OPERATION],
                target=target[:_MAX_TARGET] if target else None,
                status="error" if error is not None else "success",
                http_status=(
                    http_status if http_status is not None else _http_status_of(error)
                ),
                duration_ms=timer.duration_ms,
                error_type=type(error).__name__ if error is not None else None,
                error_message=_error_message(error),
                request_body=_body_text(request_body),
                response_body=_body_text(response_body),
                source=ctx.source if ctx else None,
                run_id=ctx.run_id if ctx else None,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                requested_at=timer.requested_at,
            )
            await session.commit()
    except Exception:  # observability must not break the request
        logger.warning(
            "Could not record API request %s/%s", provider, operation, exc_info=True
        )


def _http_status_of(error: BaseException | None) -> int | None:
    """The HTTP status an SDK exception carries, if it exposes one."""
    if error is None:
        return None
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _error_message(error: BaseException | None) -> str | None:
    if error is None:
        return None
    message = str(error).strip()
    return message[:_MAX_ERROR] if message else None


def _jsonable(value: Any) -> Any:
    """Turn API payloads (dicts, pydantic models, primitives) into JSON data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except TypeError:
            return value.model_dump()
    raw = getattr(value, "raw", None)
    if isinstance(raw, (dict, list)):
        return _jsonable(raw)
    return repr(value)


def _redact(value: Any) -> Any:
    """Replace credential fields anywhere in a JSON-shaped payload."""
    if isinstance(value, dict):
        return {
            key: REDACTED if key.lower() in _SENSITIVE_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _body_text(value: Any) -> str | None:
    """JSON-encode a body, redact credentials, and apply the size cap."""
    if value is None or not BODIES_ENABLED:
        return None
    try:
        if isinstance(value, str):
            # Raw response text (e.g. an error page) is kept as-is; there is
            # no structure to redact, and it is never a login response.
            text = value
        else:
            text = json.dumps(
                _redact(_jsonable(value)), ensure_ascii=False, default=str
            )
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > MAX_BODY_CHARS:
        omitted = len(text) - MAX_BODY_CHARS
        text = f"{text[:MAX_BODY_CHARS]}... [truncated {omitted} chars]"
    return text
