"""LLM access via OpenRouter, plus per-chat model selection.

Every model request goes through OpenRouter's OpenAI-compatible
Chat Completions endpoint, so any model OpenRouter lists (Anthropic, OpenAI,
Google, Mistral, ...) can be used by changing a model id — no code change.

Canonical message format
------------------------
Conversation history keeps the **Anthropic Messages shape** it has always had
(``text`` / ``tool_use`` / ``tool_result`` content blocks). That is what
``history.py`` reasons about — turn boundaries, orphaned ``tool_result``
detection, dangling ``tool_use`` detection — and what is already stored in the
database. This module translates to the OpenAI wire format on the way out and
back again on the way in, so:

- no stored conversation needs migrating,
- the turn/deletion invariants in ``history.py`` are untouched, and
- a chat can switch model mid-conversation: the history is re-translated on
  every request, whichever provider produced it.

Per-chat model
--------------
Each chat may pin its own model in ``chat_settings.model``. ``NULL`` means
"follow the deployment default" (``LLM_MODEL``), so changing the default moves
every chat that has not explicitly chosen something else.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from .db.queries import get_chat_settings, get_db
from .db.queries import set_chat_model as _set_chat_model_row

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
DEFAULT_MODEL = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4.6")
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))

# OpenAI finish_reason -> the Anthropic stop_reason bot.py already handles.
_STOP_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


class LLMError(RuntimeError):
    """The provider answered, but not with anything usable."""


@dataclass
class LLMResponse:
    """A completion translated back into Anthropic-shaped content blocks."""

    content: list[dict]
    stop_reason: str | None
    model: str | None = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def allowed_models() -> set[str]:
    """Models a chat may be switched to. Empty means "any model id"."""
    raw = os.getenv("LLM_ALLOWED_MODELS", "")
    return {m.strip() for m in raw.split(",") if m.strip()}


_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        headers = {"X-Title": os.getenv("OPENROUTER_APP_NAME", "Picnic Like a Pro")}
        # Optional attribution on openrouter.ai; harmless when unset.
        referer = os.getenv("OPENROUTER_APP_URL")
        if referer:
            headers["HTTP-Referer"] = referer
        _client = AsyncOpenAI(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url=OPENROUTER_BASE_URL,
            default_headers=headers,
        )
    return _client


# ---------------------------------------------------------------------------
# Per-chat model selection
#
# Deliberately uncached: it is one primary-key lookup per turn, and reading it
# fresh means a switch made from another process (scripts/chat_model.py)
# applies to the very next message without restarting the bot.
#
# Reads are fail-soft (a DB fault falls back to the default model rather than
# breaking the chat); writes propagate so a caller never reports a switch that
# did not happen.
# ---------------------------------------------------------------------------

async def get_chat_model(chat_id: int) -> str:
    """The model this chat's next request will use."""
    try:
        async with get_db() as session:
            settings = await get_chat_settings(session, chat_id)
    except Exception:  # noqa: BLE001 — never let a DB fault break a chat
        logger.exception("Could not read model setting for chat %d", chat_id)
        return DEFAULT_MODEL
    return (settings or {}).get("model") or DEFAULT_MODEL


async def set_chat_model(chat_id: int, model: str | None) -> None:
    """Pin *model* for this chat, or pass ``None`` to follow the default again.

    Raises ``ValueError`` when ``LLM_ALLOWED_MODELS`` is set and *model* is not
    in it, so user-facing callers can reuse the same guard.
    """
    model = model.strip() if model else None
    allowed = allowed_models()
    if model and allowed and model not in allowed:
        raise ValueError(
            f"Model {model!r} is not allowed. Choose one of: {', '.join(sorted(allowed))}"
        )
    async with get_db() as session:
        await _set_chat_model_row(session, chat_id, model)
        await session.commit()


# ---------------------------------------------------------------------------
# Anthropic shape -> OpenAI wire format
# ---------------------------------------------------------------------------

def _text_of(content: Any) -> str:
    """Flatten a string or a list of blocks into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


def to_openai_tools(tools: list[dict]) -> list[dict]:
    """Anthropic tool schemas (``input_schema``) -> OpenAI function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def to_openai_messages(system: str | None, messages: list[dict]) -> list[dict]:
    """Translate Anthropic-shaped history into Chat Completions messages.

    - an assistant message's ``tool_use`` blocks become ``tool_calls``;
    - a user message's ``tool_result`` blocks become one ``tool`` message each,
      emitted *before* any text in the same message, because OpenAI requires
      tool messages to immediately follow the assistant message that called
      them.
    """
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in messages:
        role, content = msg.get("role"), msg.get("content")

        if role == "assistant":
            text = _text_of(content)
            tool_calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": json.dumps(b.get("input") or {}),
                    },
                }
                for b in (content if isinstance(content, list) else [])
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            entry: dict = {"role": "assistant", "content": text or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
            continue

        # user
        if isinstance(content, str):
            out.append({"role": "user", "content": content})
            continue
        for b in content or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": b["tool_use_id"],
                        "content": _text_of(b.get("content")),
                    }
                )
        text = _text_of(content)
        if text:
            out.append({"role": "user", "content": text})

    return out


# ---------------------------------------------------------------------------
# OpenAI wire format -> Anthropic shape
# ---------------------------------------------------------------------------

def _parse_arguments(raw: str | None) -> dict:
    """Decode a tool call's JSON arguments.

    Malformed JSON is passed through under a key no tool accepts, so the call
    fails inside ``mcp_server.call_tool`` and the error is reported back to
    the model as a tool_result — which lets it retry — instead of crashing the
    turn here.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_invalid_arguments": raw}
    return parsed if isinstance(parsed, dict) else {"_invalid_arguments": raw}


def from_openai_completion(completion: Any) -> LLMResponse:
    """Translate a Chat Completions response into Anthropic-shaped blocks."""
    choices = getattr(completion, "choices", None)
    if not choices:
        # OpenRouter reports some upstream failures as a 200 with an error body.
        error = getattr(completion, "error", None)
        raise LLMError(f"LLM returned no choices: {error or 'empty response'}")

    choice = choices[0]
    message = choice.message
    blocks: list[dict] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for call in message.tool_calls or []:
        blocks.append(
            {
                "type": "tool_use",
                # Some providers omit ids; the tool_result must reference one.
                "id": call.id or f"call_{uuid.uuid4().hex[:24]}",
                "name": call.function.name,
                "input": _parse_arguments(call.function.arguments),
            }
        )

    finish = choice.finish_reason
    stop_reason = _STOP_REASONS.get(finish, finish)
    # Some models report finish_reason="stop" alongside complete tool calls.
    # Treat those as tool use — but never a truncated ("length") response,
    # whose last tool call may be cut off mid-arguments.
    if message.tool_calls and finish != "length":
        stop_reason = "tool_use"

    return LLMResponse(
        content=blocks,
        stop_reason=stop_reason,
        model=getattr(completion, "model", None),
    )


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

async def create_message(
    *,
    model: str,
    system: str | None,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int = MAX_TOKENS,
) -> LLMResponse:
    """Send Anthropic-shaped *messages* to *model* via OpenRouter."""
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": to_openai_messages(system, messages),
    }
    if tools:
        kwargs["tools"] = to_openai_tools(tools)
    completion = await get_client().chat.completions.create(**kwargs)
    return from_openai_completion(completion)
