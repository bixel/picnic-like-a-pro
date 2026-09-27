"""Tests for llm.py: the OpenRouter translation layer and per-chat models.

Responses are built with the real ``openai`` pydantic types, so the parsing
here runs against exactly the shapes the SDK hands back.
"""

from __future__ import annotations

import json

import pytest
from openai.types.chat import ChatCompletion

from picnic_meal_planner import llm
from picnic_meal_planner.db.queries import (
    get_chat_settings,
    get_db,
    list_chat_models,
    set_chat_persist_history,
)


def _completion(
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    finish_reason: str = "stop",
) -> ChatCompletion:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.get("id", "call_1"),
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": tc.get("arguments", "{}"),
                },
            }
            for tc in tool_calls
        ]
    return ChatCompletion.model_validate(
        {
            "id": "gen-1",
            "object": "chat.completion",
            "created": 0,
            "model": "openai/gpt-5",
            "choices": [
                {"index": 0, "message": message, "finish_reason": finish_reason}
            ],
        }
    )


# ---------------------------------------------------------------------------
# Outbound translation
# ---------------------------------------------------------------------------

class TestToOpenAIMessages:
    def test_system_prompt_comes_first(self):
        out = llm.to_openai_messages("be nice", [{"role": "user", "content": "hi"}])
        assert out == [
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "hi"},
        ]

    def test_no_system_message_when_empty(self):
        out = llm.to_openai_messages(None, [{"role": "user", "content": "hi"}])
        assert out == [{"role": "user", "content": "hi"}]

    def test_full_tool_turn(self):
        history = [
            {"role": "user", "content": "what's in the cart?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me look."},
                    {"type": "tool_use", "id": "tu_1", "name": "get_cart", "input": {"a": 1}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "milk"}
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Milk."}]},
        ]
        out = llm.to_openai_messages(None, history)
        assert out[1] == {
            "role": "assistant",
            "content": "Let me look.",
            "tool_calls": [
                {
                    "id": "tu_1",
                    "type": "function",
                    "function": {"name": "get_cart", "arguments": json.dumps({"a": 1})},
                }
            ],
        }
        assert out[2] == {"role": "tool", "tool_call_id": "tu_1", "content": "milk"}
        assert out[3] == {"role": "assistant", "content": "Milk."}

    def test_tool_only_assistant_message_has_null_content(self):
        out = llm.to_openai_messages(
            None,
            [{"role": "assistant", "content": [
                {"type": "tool_use", "id": "t", "name": "x", "input": {}}
            ]}],
        )
        assert out[0]["content"] is None

    def test_tool_results_precede_text_in_the_same_user_message(self):
        """OpenAI needs tool messages directly after the calling assistant."""
        out = llm.to_openai_messages(None, [{
            "role": "user",
            "content": [
                {"type": "text", "text": "also this"},
                {"type": "tool_result", "tool_use_id": "t1", "content": "r1"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "r2"},
            ],
        }])
        assert [m["role"] for m in out] == ["tool", "tool", "user"]
        assert out[2]["content"] == "also this"

    def test_block_list_tool_result_content_is_flattened(self):
        out = llm.to_openai_messages(None, [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "t1",
                "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
            }],
        }])
        assert out[0]["content"] == "ab"

    def test_tools_are_wrapped_as_functions(self):
        tools = [{"name": "get_cart", "description": "d", "input_schema": {"type": "object"}}]
        assert llm.to_openai_tools(tools) == [{
            "type": "function",
            "function": {"name": "get_cart", "description": "d", "parameters": {"type": "object"}},
        }]


# ---------------------------------------------------------------------------
# Inbound translation
# ---------------------------------------------------------------------------

class TestFromOpenAICompletion:
    def test_text_reply(self):
        r = llm.from_openai_completion(_completion("hello"))
        assert r.content == [{"type": "text", "text": "hello"}]
        assert r.stop_reason == "end_turn"
        assert r.model == "openai/gpt-5"

    def test_tool_call(self):
        r = llm.from_openai_completion(_completion(
            tool_calls=[{"id": "c1", "name": "get_cart", "arguments": '{"q": "milk"}'}],
            finish_reason="tool_calls",
        ))
        assert r.content == [
            {"type": "tool_use", "id": "c1", "name": "get_cart", "input": {"q": "milk"}}
        ]
        assert r.stop_reason == "tool_use"

    def test_tool_calls_with_finish_stop_still_count_as_tool_use(self):
        r = llm.from_openai_completion(_completion(
            tool_calls=[{"name": "get_cart"}], finish_reason="stop",
        ))
        assert r.stop_reason == "tool_use"

    def test_truncated_tool_call_is_not_tool_use(self):
        """bot.py relies on this to discard, not run, a cut-off tool call."""
        r = llm.from_openai_completion(_completion(
            tool_calls=[{"name": "get_cart", "arguments": '{"q": "mi'}],
            finish_reason="length",
        ))
        assert r.stop_reason == "max_tokens"
        assert r.content[0]["type"] == "tool_use"

    def test_malformed_arguments_are_passed_through(self):
        r = llm.from_openai_completion(_completion(
            tool_calls=[{"name": "x", "arguments": "not json"}], finish_reason="tool_calls",
        ))
        assert r.content[0]["input"] == {"_invalid_arguments": "not json"}

    def test_non_object_arguments_are_passed_through(self):
        r = llm.from_openai_completion(_completion(
            tool_calls=[{"name": "x", "arguments": "[1]"}], finish_reason="tool_calls",
        ))
        assert r.content[0]["input"] == {"_invalid_arguments": "[1]"}

    def test_missing_tool_call_id_is_generated(self):
        r = llm.from_openai_completion(_completion(
            tool_calls=[{"id": "", "name": "x"}], finish_reason="tool_calls",
        ))
        assert r.content[0]["id"].startswith("call_")

    def test_empty_content_yields_no_blocks(self):
        assert llm.from_openai_completion(_completion(None)).content == []

    def test_no_choices_raises(self):
        completion = _completion("x")
        completion.choices = []
        with pytest.raises(llm.LLMError):
            llm.from_openai_completion(completion)

    def test_round_trip_through_history_shape(self):
        """A reply translated in must translate back out unchanged."""
        r = llm.from_openai_completion(_completion(
            "checking",
            tool_calls=[{"id": "c1", "name": "get_cart", "arguments": '{"q": 1}'}],
            finish_reason="tool_calls",
        ))
        out = llm.to_openai_messages(None, [{"role": "assistant", "content": r.content}])
        assert out[0]["content"] == "checking"
        assert out[0]["tool_calls"][0]["id"] == "c1"
        assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"q": 1}


# ---------------------------------------------------------------------------
# create_message
# ---------------------------------------------------------------------------

class _FakeCompletions:
    def __init__(self, completion):
        self.completion = completion
        self.kwargs: dict | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.completion


class _FakeClient:
    def __init__(self, completion):
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(completion)


class TestCreateMessage:
    async def test_sends_translated_request(self, monkeypatch):
        client = _FakeClient(_completion("hi"))
        monkeypatch.setattr(llm, "get_client", lambda: client)
        tools = [{"name": "t", "description": "", "input_schema": {"type": "object"}}]
        r = await llm.create_message(
            model="anthropic/claude-sonnet-4.6",
            system="sys",
            messages=[{"role": "user", "content": "hello"}],
            tools=tools,
            max_tokens=10,
        )
        sent = client.chat.completions.kwargs
        assert sent["model"] == "anthropic/claude-sonnet-4.6"
        assert sent["max_tokens"] == 10
        assert sent["messages"][0] == {"role": "system", "content": "sys"}
        assert sent["tools"][0]["function"]["name"] == "t"
        assert r.content == [{"type": "text", "text": "hi"}]

    async def test_omits_tools_when_there_are_none(self, monkeypatch):
        client = _FakeClient(_completion("hi"))
        monkeypatch.setattr(llm, "get_client", lambda: client)
        await llm.create_message(model="m", system=None, messages=[], tools=[])
        assert "tools" not in client.chat.completions.kwargs


class TestClientHeaders:
    def test_referer_header_is_optional(self, monkeypatch):
        monkeypatch.setattr(llm, "_client", None)
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("OPENROUTER_APP_URL", "https://example.test")
        client = llm.get_client()
        assert client.default_headers["HTTP-Referer"] == "https://example.test"
        assert client.default_headers["X-Title"]


# ---------------------------------------------------------------------------
# Per-chat model
# ---------------------------------------------------------------------------

class TestChatModel:
    async def test_default_when_nothing_is_set(self, db_path):
        assert await llm.get_chat_model(1) == llm.DEFAULT_MODEL

    async def test_set_and_reset(self, db_path):
        await llm.set_chat_model(1, "openai/gpt-5")
        assert await llm.get_chat_model(1) == "openai/gpt-5"
        await llm.set_chat_model(1, None)
        assert await llm.get_chat_model(1) == llm.DEFAULT_MODEL

    async def test_whitespace_is_stripped(self, db_path):
        await llm.set_chat_model(1, "  openai/gpt-5 ")
        assert await llm.get_chat_model(1) == "openai/gpt-5"

    async def test_allowlist_is_enforced(self, db_path, monkeypatch):
        monkeypatch.setenv("LLM_ALLOWED_MODELS", "openai/gpt-5, anthropic/claude-sonnet-4.6")
        with pytest.raises(ValueError, match="not allowed"):
            await llm.set_chat_model(1, "some/other-model")
        await llm.set_chat_model(1, "openai/gpt-5")
        assert await llm.get_chat_model(1) == "openai/gpt-5"

    async def test_reset_is_allowed_even_with_an_allowlist(self, db_path, monkeypatch):
        monkeypatch.setenv("LLM_ALLOWED_MODELS", "openai/gpt-5")
        await llm.set_chat_model(1, None)
        assert await llm.get_chat_model(1) == llm.DEFAULT_MODEL

    async def test_read_failure_falls_back_to_default(self, monkeypatch):
        def broken():
            raise RuntimeError("db down")

        monkeypatch.setattr(llm, "get_db", broken)
        assert await llm.get_chat_model(1) == llm.DEFAULT_MODEL

    async def test_model_does_not_touch_the_persistence_flag(self, db_path):
        async with get_db() as session:
            await set_chat_persist_history(session, 1, False)
            await session.commit()
        await llm.set_chat_model(1, "openai/gpt-5")
        async with get_db() as session:
            settings = await get_chat_settings(session, 1)
        assert settings["persist_history"] == 0
        assert settings["model"] == "openai/gpt-5"

    async def test_persistence_flag_does_not_touch_the_model(self, db_path):
        await llm.set_chat_model(1, "openai/gpt-5")
        async with get_db() as session:
            await set_chat_persist_history(session, 1, False)
            await session.commit()
        assert await llm.get_chat_model(1) == "openai/gpt-5"

    async def test_new_row_defaults_to_persisting(self, db_path):
        await llm.set_chat_model(1, "openai/gpt-5")
        async with get_db() as session:
            settings = await get_chat_settings(session, 1)
        assert settings["persist_history"] == 1

    async def test_list_chat_models(self, db_path):
        from picnic_meal_planner.db.queries import append_turn

        await llm.set_chat_model(2, "openai/gpt-5")
        async with get_db() as session:
            await append_turn(session, 1, [{"role": "user", "content": "hi"}])
            await session.commit()
            chats = await list_chat_models(session)
        assert chats == [
            {"chat_id": 1, "model": None},
            {"chat_id": 2, "model": "openai/gpt-5"},
        ]
