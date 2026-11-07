"""Best-effort logging of calls to external APIs.

Two providers are logged: Picnic (``picnic/client.py`` and ``picnic/auth.py``)
and OpenRouter (``llm.create_message``).  The log is built to make a request
replayable: each row stores the HTTP method, full URL, request/response headers
and the complete request/response bodies.  Bodies are not truncated.

What is stored
--------------
- ``http_method`` and the full ``url``, including the query string.
- ``request_headers`` and ``response_headers`` as JSON.
- The full request and response bodies as JSON where they are JSON, or verbatim
  when they are not.

Credential values are masked, not removed:

- Header values for ``Authorization``, ``Proxy-Authorization``,
  ``x-picnic-auth``, cookies, and any header whose name contains ``token``,
  ``secret``, ``api-key``/``apikey`` or ends in ``-auth`` become
  ``[REDACTED]``.  A ``Bearer``/``Basic`` scheme is preserved, e.g.
  ``Authorization: Bearer [REDACTED]``.
- Body fields named ``password``, ``secret``, ``otp``, ``token``, ``api_key``,
  etc. become ``[REDACTED]`` anywhere in the JSON structure.

Everything else is verbatim, however large.  The table contains prompt,
tool-result and order data, so treat the database file as sensitive and prune
the log with ``db.queries.prune_api_requests()``.  ``API_LOG_BODIES=false``
disables body capture; URLs and headers are still recorded.

Consent
-------
The bot opens a :func:`request_context` per chat turn with the chat's id and
its history-persistence decision.  A chat that opted out of history (or
``PERSIST_CONVERSATIONS=false``) gets rows without bodies or query strings;
OpenRouter requests outside any context store no bodies either.  Rows with a
``chat_id`` are deleted by ``history.py`` with the turns they belong to.

Instrumentation
---------------
- Picnic: :func:`install_picnic_capture` wraps the ``requests.Session`` used by
  python-picnic-api2.  Every HTTP call — normal operations, login and 2FA —
  is captured with the *prepared* method, URL, headers and wire body.
- OpenRouter: ``llm.create_message`` wraps its call in the same
  :func:`capture_http_requests` block and fills in the URL, request headers,
  body, raw completion and token counts itself.

Picnic captures are collected in a contextvar and persisted by
:func:`capture_http_requests` after the synchronous library call returns.  The
wrappers therefore run the blocking client through ``asyncio.to_thread``, which
copies the contextvar into the worker thread.

Policy
------
- **Best effort.**  Persisting the log must never change the outcome of the
  request being logged: every failure here is a warning, never an exception.
- **Await, do not detach.**  The insert is awaited so the row is committed
  before the caller returns.  Losing rows to a fire-and-forget task would be
  worse than one INSERT.  It runs on a separate engine whose lock timeout is
  ``engine.LOG_LOCK_TIMEOUT_SECONDS``, so a database locked by another
  connection costs a lost row, not a 5 s stall per call.  All rows of one
  wrapped call are written in one transaction.
- **Retention is explicit.**  Rows are not truncated; use
  ``db.queries.prune_api_requests()`` from an operator task.
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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .db import queries as db
from .db.engine import get_log_db

logger = logging.getLogger(__name__)

PROVIDER_PICNIC = "picnic"
PROVIDER_OPENROUTER = "openrouter"

REDACTED = "[REDACTED]"

BODIES_ENABLED = os.getenv("API_LOG_BODIES", "true").strip().lower() != "false"

_MAX_OPERATION = 200
_MAX_TARGET = 500
_MAX_ERROR = 500

# Exact body keys that hold credentials.  This is deliberately a short list:
# matching broadly on "code" or "token" would hide ordinary payload fields.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "otp",
        "auth_token",
        "access_token",
        "refresh_token",
        "token",
        "api_key",
        "apikey",
        "authorization",
    }
)

# Header names that always carry a credential.  Names containing "token",
# "secret", "api-key"/"apikey" or ending in "-auth" are also masked, which
# covers x-picnic-auth and x-csrf-token.
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-picnic-auth",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
    }
)


@dataclass(frozen=True)
class _Context:
    source: str
    run_id: str
    chat_id: int | None = None
    store_bodies: bool = True


_context: contextvars.ContextVar[_Context | None] = contextvars.ContextVar(
    "api_log_context", default=None
)

# The list a Picnic session wrapper appends to.  It is set by
# capture_http_requests() and reaches the worker thread via asyncio.to_thread.
_captures: contextvars.ContextVar[list[HttpCapture] | None] = contextvars.ContextVar(
    "api_log_captures", default=None
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
class HttpCapture:
    """One HTTP request made inside a :func:`capture_http_requests` block."""

    method: str
    url: str
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: Any = None
    response_headers: dict[str, str] | None = None
    response_body: Any = None
    status_code: int | None = None
    error: BaseException | None = None
    started_perf: float = field(default_factory=time.perf_counter)
    requested_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


@dataclass
class CaptureSession:
    """One logged call, shared with the caller while it is in flight.

    The fields below describe the call as a whole.  They become the row when
    no HTTP request was captured: the in-process mock client, a Picnic call
    that failed before sending, or an OpenRouter call (the openai SDK uses
    httpx, which is not wrapped, so ``llm.py`` fills them in itself).  When
    HTTP requests were captured, each becomes its own row instead.

    If the call raises and no ``response_body`` was set, the payload carried
    by the exception (``error_response_body``) is stored.
    """

    provider: str
    operation: str
    target: str | None = None
    http_method: str | None = None
    url: str | None = None
    request_headers: Any = None
    request_body: Any = None
    response_body: Any = None
    http_status: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: BaseException | None = None
    timer: Timer = field(default_factory=Timer.start)
    captures: list[HttpCapture] = field(default_factory=list)


def call_body(*args: Any, **kwargs: Any) -> dict:
    """The request payload of a wrapper call, used only as a fallback."""
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
    source: str,
    *,
    run_id: str | None = None,
    chat_id: int | None = None,
    store_bodies: bool = True,
) -> AsyncIterator[str]:
    """Attribute every request logged inside the block to *source*/*run_id*.

    *chat_id* ties the rows to a chat, so deleting its history deletes them.
    *store_bodies* is the caller's consent decision (the bot passes
    ``history.is_persistence_enabled``): when false, rows keep their metadata
    but no bodies and no URL query string.  This module makes no consent
    decision of its own; see :func:`_may_store_bodies` for the default.

    Yields the run id (generated when not given).  Contexts nest: the inner
    block wins, and the outer one resumes when it exits.
    """
    ctx = _Context(
        source=source,
        run_id=run_id or uuid.uuid4().hex,
        chat_id=chat_id,
        store_bodies=store_bodies,
    )
    token = _context.set(ctx)
    try:
        yield ctx.run_id
    finally:
        _context.reset(token)


@asynccontextmanager
async def capture_http_requests(
    provider: str,
    operation: str,
    *,
    target: str | None = None,
    http_method: str | None = None,
    url: str | None = None,
    request_headers: Any = None,
    request_body: Any = None,
) -> AsyncIterator[CaptureSession]:
    """Log the call made inside the block; the one wrapper for both providers.

    A blocking Picnic call must run through ``asyncio.to_thread`` so the
    capture list reaches the worker thread.  Every captured HTTP request
    becomes its own row.  If none was captured, one row is written from the
    yielded :class:`CaptureSession` (see there).  The block's outcome is never
    changed: an exception is logged and re-raised unchanged.
    """
    session = CaptureSession(
        provider=provider,
        operation=operation,
        target=target,
        http_method=http_method,
        url=url,
        request_headers=request_headers,
        request_body=request_body,
    )
    token = _captures.set(session.captures)
    try:
        yield session
    except BaseException as exc:  # logged below, then re-raised unchanged
        session.error = exc
        raise
    finally:
        _captures.reset(token)
        await _persist(provider, operation, _session_rows(session))


def _session_rows(session: CaptureSession) -> list[dict]:
    """One row per captured HTTP request, or one fallback row if there were none."""
    if not session.captures:
        return [
            _row(
                session.provider,
                session.operation,
                session.timer,
                target=session.target,
                error=session.error,
                http_status=session.http_status,
                http_method=session.http_method,
                url=session.url,
                request_headers=session.request_headers,
                input_tokens=session.input_tokens,
                output_tokens=session.output_tokens,
                request_body=session.request_body,
                response_body=(
                    session.response_body
                    if session.response_body is not None
                    else error_response_body(session.error)
                ),
            )
        ]
    return [
        _row(
            session.provider,
            session.operation,
            Timer(capture.started_perf, capture.requested_at),
            target=session.target,
            error=capture.error,
            http_status=capture.status_code,
            http_method=capture.method,
            url=capture.url,
            request_headers=capture.request_headers,
            response_headers=capture.response_headers,
            request_body=capture.request_body,
            response_body=capture.response_body,
        )
        for capture in session.captures
    ]


def install_picnic_capture(api: Any) -> bool:
    """Wrap a PicnicAPI session so every HTTP call is captured.

    Returns ``True`` when the client has a ``requests``-style session.  The
    wrapper records the prepared request (method, full URL, headers, wire
    body) and the response, including failures raised by ``requests`` itself.
    """
    session = getattr(api, "session", None)
    if session is None or not hasattr(session, "request"):
        return False
    if getattr(session, "_api_log_capture_installed", False):
        return True

    original_request = session.request

    def request(method: str, url: str, **kwargs: Any):
        capture = HttpCapture(
            method=str(method).upper(),
            url=str(url),
            request_headers=_headers_dict(
                getattr(session, "headers", None), kwargs.get("headers")
            ),
            request_body=_request_body(kwargs),
        )
        captures = _captures.get()
        if captures is not None:
            captures.append(capture)

        try:
            response = original_request(method, url, **kwargs)
        except BaseException as exc:  # transport failure, re-raised unchanged
            capture.error = exc
            raise

        capture.status_code = getattr(response, "status_code", None)
        capture.response_headers = _headers_dict(getattr(response, "headers", None))
        capture.response_body = _response_text(response)

        # Prefer the prepared request: it has the final merged headers and the
        # actual wire body (e.g. JSON already encoded).
        prepared = getattr(response, "request", None)
        if prepared is not None:
            capture.method = str(getattr(prepared, "method", capture.method)).upper()
            capture.url = str(getattr(prepared, "url", capture.url))
            prepared_headers = _headers_dict(getattr(prepared, "headers", None))
            if prepared_headers:
                capture.request_headers = prepared_headers
            body = getattr(prepared, "body", None)
            if body is not None:
                capture.request_body = body
        return response

    session.request = request
    session._api_log_capture_installed = True
    return True


async def record(
    provider: str,
    operation: str,
    timer: Timer,
    *,
    target: str | None = None,
    error: BaseException | None = None,
    http_status: int | None = None,
    http_method: str | None = None,
    url: str | None = None,
    request_headers: Any = None,
    response_headers: Any = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    request_body: Any = None,
    response_body: Any = None,
) -> None:
    """Persist one log row; never raises.

    Prefer :func:`capture_http_requests`; this is the low-level writer.
    """
    await _persist(
        provider,
        operation,
        [
            _row(
                provider,
                operation,
                timer,
                target=target,
                error=error,
                http_status=http_status,
                http_method=http_method,
                url=url,
                request_headers=request_headers,
                response_headers=response_headers,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                request_body=request_body,
                response_body=response_body,
            )
        ],
    )


async def _persist(provider: str, operation: str, rows: list[dict]) -> None:
    """Insert *rows* in one transaction on the log engine; never raises.

    The log engine gives up on a held lock after
    ``engine.LOG_LOCK_TIMEOUT_SECONDS``, so a busy database costs the request
    a lost row, not a multi-second stall.
    """
    try:
        async with get_log_db() as session:
            for row in rows:
                await db.insert_api_request(session, **row)
            await session.commit()
    except Exception:  # observability must not break the request
        logger.warning(
            "Could not record API request %s/%s", provider, operation, exc_info=True
        )


def _row(
    provider: str,
    operation: str,
    timer: Timer,
    *,
    target: str | None = None,
    error: BaseException | None = None,
    http_status: int | None = None,
    http_method: str | None = None,
    url: str | None = None,
    request_headers: Any = None,
    response_headers: Any = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    request_body: Any = None,
    response_body: Any = None,
) -> dict:
    """The column values for one row: truncated, redacted and encoded."""
    ctx = _context.get()
    if not _may_store_bodies(provider, ctx):
        request_body = response_body = None
        url = _without_query(url)
    return dict(
        provider=provider,
        operation=operation[:_MAX_OPERATION],
        target=target[:_MAX_TARGET] if target else None,
        http_method=http_method[:20] if http_method else None,
        url=url,
        request_headers=_headers_text(request_headers),
        response_headers=_headers_text(response_headers),
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
        chat_id=ctx.chat_id if ctx else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        requested_at=timer.requested_at,
    )


def _may_store_bodies(provider: str, ctx: _Context | None) -> bool:
    """Whether a row may carry bodies (and a URL query string).

    Inside a :func:`request_context`, the caller decided.  Outside one, an
    OpenRouter request fails closed: its body is a chat transcript, and
    nothing has confirmed that chat's consent.  Picnic requests outside a
    context (import script, MCP server, login) carry no chat content.
    """
    if not BODIES_ENABLED:
        return False
    if ctx is not None:
        return ctx.store_bodies
    return provider != PROVIDER_OPENROUTER


def _without_query(url: str | None) -> str | None:
    """*url* without its query string, which can carry a search term."""
    if not url:
        return url
    return url.split("?", 1)[0]


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


def _headers_dict(*sources: Any) -> dict[str, str]:
    """Merge header mappings (session headers first, per-request last)."""
    merged: dict[str, str] = {}
    for source in sources:
        if source is None:
            continue
        try:
            items = source.items()
        except AttributeError:
            continue
        for name, value in items:
            if value is None:
                continue
            merged[str(name)] = str(value)
    return merged


def _request_body(kwargs: dict) -> Any:
    if kwargs.get("json") is not None:
        return kwargs["json"]
    return kwargs.get("data")


def _response_text(response: Any) -> Any:
    try:
        return response.text
    except Exception:  # noqa: BLE001 — a body we cannot decode is not fatal
        return None


def _is_sensitive_header(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in _SENSITIVE_HEADERS
        or "authorization" in lowered
        or "token" in lowered
        or "secret" in lowered
        or "api-key" in lowered
        or "apikey" in lowered
        or lowered.endswith("-auth")
    )


def _redact_header(name: str, value: Any) -> str:
    text = "" if value is None else str(value)
    if not _is_sensitive_header(name):
        return text
    scheme, separator, _ = text.partition(" ")
    if separator and scheme.lower() in {"bearer", "basic", "token"}:
        return f"{scheme} {REDACTED}"
    return REDACTED


def _headers_text(headers: Any) -> str | None:
    if not headers:
        return None
    try:
        items = headers.items()
    except AttributeError:
        return None
    redacted = {str(name): _redact_header(str(name), value) for name, value in items}
    return json.dumps(redacted, ensure_ascii=False)


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


def _decode_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped or stripped[0] not in '{["':
        return None
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return None


def _body_text(value: Any) -> str | None:
    """JSON-encode a body, redact credentials, and keep the whole thing."""
    if value is None or not BODIES_ENABLED:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if isinstance(value, str):
        parsed = _decode_json(value)
        if parsed is None:
            # Not JSON (e.g. an HTML error page): keep it verbatim.
            return value
        return json.dumps(_redact(_jsonable(parsed)), ensure_ascii=False, default=str)
    try:
        return json.dumps(_redact(_jsonable(value)), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)
