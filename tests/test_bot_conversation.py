"""Tests for the bot's LLM conversation loop and Telegram handlers.

The LLM call and Telegram Update/Context objects are replaced with
lightweight fakes, so these tests exercise the real control flow in bot.py
without any network access.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from picnic_meal_planner import bot, history, llm


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _TextBlock(text: str) -> dict:
    """A text block as llm.create_message returns it (already plain dicts)."""
    return {"type": "text", "text": text}


def _ToolUseBlock(name: str, input_data: dict, id: str = "tu_1") -> dict:
    return {"type": "tool_use", "id": id, "name": name, "input": input_data}


def _reply(content: list[dict], stop_reason: str = "end_turn") -> llm.LLMResponse:
    return llm.LLMResponse(content=content, stop_reason=stop_reason)


class _FakeLLM:
    """Stands in for llm.create_message: returns queued responses in order and
    records the arguments of every call."""

    def __init__(self, responses: list[llm.LLMResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def __call__(self, **kwargs):
        # bot._run_llm mutates the same list across tool rounds, so snapshot
        # it here to capture what was actually sent on this call.
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        return self._responses.pop(0)


@pytest.fixture
def fake_llm(monkeypatch):
    """Install a fake LLM; the test supplies the response queue."""

    def _install(responses):
        client = _FakeLLM(responses)
        monkeypatch.setattr(llm, "create_message", client)
        monkeypatch.setattr(bot, "_mcp_tools", [])
        return client

    return _install


@pytest.fixture(autouse=True)
def clear_histories():
    """Reset history.py's in-process caches between tests.

    The database itself is already isolated per test by the autouse
    ``isolated_db`` fixture in conftest.py.
    """
    history._reset_caches()
    yield
    history._reset_caches()


def _make_update(text: str = "hello", user_id: int = 1, chat_id: int = 100):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_context():
    context = MagicMock()
    context.bot.send_chat_action = AsyncMock()
    return context


# ---------------------------------------------------------------------------
# _run_llm
# ---------------------------------------------------------------------------

class TestRunLlm:
    async def test_returns_plain_text_reply(self, fake_llm):
        fake_llm([_reply([_TextBlock("Hi there!")])])
        assert await bot._run_llm(1, "hello") == "Hi there!"

    async def test_concatenates_multiple_text_blocks(self, fake_llm):
        fake_llm([_reply([_TextBlock("Hello "), _TextBlock("world")])])
        assert await bot._run_llm(1, "hi") == "Hello world"

    async def test_empty_response_returns_placeholder(self, fake_llm):
        fake_llm([_reply([])])
        assert await bot._run_llm(1, "hi") == "(no reply)"

    async def test_history_is_persisted_after_success(self, fake_llm, db_path):
        fake_llm([_reply([_TextBlock("ok")])])
        await bot._run_llm(42, "remember this")
        context = await history.get_context(42)
        assert len(context) == 2
        assert context[0] == {"role": "user", "content": "remember this"}

    async def test_history_accumulates_across_turns(self, fake_llm, db_path):
        fake_llm([_reply([_TextBlock("a")]), _reply([_TextBlock("b")])])
        await bot._run_llm(7, "first")
        await bot._run_llm(7, "second")
        assert len(await history.get_context(7)) == 4

    async def test_history_survives_a_restart(self, fake_llm, db_path):
        """The point of persisting it: context outlives the process."""
        fake_llm([_reply([_TextBlock("ok")])])
        await bot._run_llm(42, "remember this")

        history._reset_caches()  # simulate a restart

        context = await history.get_context(42)
        assert len(context) == 2
        assert context[0] == {"role": "user", "content": "remember this"}

    async def test_prior_history_is_sent_to_the_model(self, fake_llm):
        client = fake_llm([_reply([_TextBlock("a")]), _reply([_TextBlock("b")])])
        await bot._run_llm(7, "first")
        await bot._run_llm(7, "second")
        assert len(client.calls[1]["messages"]) == 3

    async def test_history_not_updated_when_api_raises(self, fake_llm, monkeypatch):
        fake_llm([])

        async def boom(**kwargs):
            raise RuntimeError("api down")

        monkeypatch.setattr(llm, "create_message", boom)
        with pytest.raises(RuntimeError):
            await bot._run_llm(9, "hello")
        assert await history.get_context(9) == []

    async def test_history_is_trimmed_to_max_turns(
        self, fake_llm, monkeypatch, db_path
    ):
        monkeypatch.setattr(history, "MAX_HISTORY_MESSAGES", 4)
        client = fake_llm([_reply([_TextBlock("ok")])])
        history._contexts[5] = [
            {"role": "user", "content": f"m{i}"} for i in range(10)
        ]
        history._loaded_chats.add(5)
        await bot._run_llm(5, "newest")
        assert len(client.calls[0]["messages"]) <= 4

    async def test_conversations_are_isolated_per_chat(self, fake_llm, db_path):
        fake_llm([_reply([_TextBlock("a")]), _reply([_TextBlock("b")])])
        await bot._run_llm(1, "chat one")
        await bot._run_llm(2, "chat two")
        one, two = await history.get_context(1), await history.get_context(2)
        assert one[0]["content"] == "chat one"
        assert two[0]["content"] == "chat two"
        assert len(two) == 2

    async def test_system_prompt_is_sent(self, fake_llm):
        client = fake_llm([_reply([_TextBlock("ok")])])
        await bot._run_llm(1, "hi")
        assert client.calls[0]["system"] == bot.SYSTEM_PROMPT


class TestRunLlmModelSelection:
    async def test_uses_default_model_for_a_new_chat(self, fake_llm, db_path):
        client = fake_llm([_reply([_TextBlock("ok")])])
        await bot._run_llm(1, "hi")
        assert client.calls[0]["model"] == llm.DEFAULT_MODEL

    async def test_uses_the_chats_pinned_model(self, fake_llm, db_path):
        await llm.set_chat_model(1, "openai/gpt-5")
        client = fake_llm([_reply([_TextBlock("ok")])])
        await bot._run_llm(1, "hi")
        assert client.calls[0]["model"] == "openai/gpt-5"

    async def test_model_is_per_chat(self, fake_llm, db_path):
        await llm.set_chat_model(1, "openai/gpt-5")
        client = fake_llm([_reply([_TextBlock("a")]), _reply([_TextBlock("b")])])
        await bot._run_llm(1, "hi")
        await bot._run_llm(2, "hi")
        assert [c["model"] for c in client.calls] == ["openai/gpt-5", llm.DEFAULT_MODEL]

    async def test_switch_applies_to_the_next_message(self, fake_llm, db_path):
        client = fake_llm([_reply([_TextBlock("a")]), _reply([_TextBlock("b")])])
        await bot._run_llm(1, "first")
        await llm.set_chat_model(1, "google/gemini-2.5-pro")
        await bot._run_llm(1, "second")
        assert client.calls[1]["model"] == "google/gemini-2.5-pro"
        # History from the previous model is carried over.
        assert len(client.calls[1]["messages"]) == 3

    async def test_switch_during_a_tool_loop_waits_for_the_next_turn(
        self, fake_llm, monkeypatch, db_path
    ):
        """The switch lands between two requests of one turn; both must use
        the model the turn started with. Fails if the lookup moves into the
        loop."""
        client = fake_llm([
            _reply([_ToolUseBlock("get_categories", {})], stop_reason="tool_use"),
            _reply([_TextBlock("done")]),
            _reply([_TextBlock("next")]),
        ])
        await llm.set_chat_model(1, "openai/gpt-5")

        async def tool_that_switches_model(name, input_data):
            await llm.set_chat_model(1, "google/gemini-2.5-pro")
            return "ok"

        monkeypatch.setattr(bot, "_call_mcp_tool", tool_that_switches_model)
        await bot._run_llm(1, "go")
        assert [c["model"] for c in client.calls] == ["openai/gpt-5"] * 2

        await bot._run_llm(1, "again")
        assert client.calls[2]["model"] == "google/gemini-2.5-pro"


class TestRunLlmThroughTheRealAdapter:
    """bot._run_llm against the real llm.create_message, with only the OpenAI
    client faked — exercises translation and the tool loop together."""

    async def test_malformed_tool_arguments_reach_the_model_as_an_error(
        self, monkeypatch, db_path
    ):
        from openai.types.chat import ChatCompletion

        def completion(message: dict, finish: str) -> ChatCompletion:
            return ChatCompletion.model_validate({
                "id": "g", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            })

        queue = [
            completion({"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "get_cart", "arguments": "{not json"},
            }]}, "tool_calls"),
            completion({"role": "assistant", "content": "Sorry, retrying."}, "stop"),
        ]
        sent: list[list[dict]] = []

        class _Completions:
            async def create(self, **kwargs):
                sent.append(kwargs["messages"])
                return queue.pop(0)

        client = MagicMock()
        client.chat.completions = _Completions()
        monkeypatch.setattr(llm, "get_client", lambda: client)
        monkeypatch.setattr(bot, "_mcp_tools", [])

        assert await bot._run_llm(1, "cart?") == "Sorry, retrying."

        tool_msg = sent[1][-1]
        assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
        assert "error" in tool_msg["content"]
        # The turn is well-formed, so it was archived.
        assert len(await history.list_turns(1)) == 1


class TestRunLlmToolUse:
    async def test_executes_tool_then_returns_final_text(self, fake_llm, db_path):
        fake_llm([
            _reply([_ToolUseBlock("get_categories", {})], stop_reason="tool_use"),
            _reply([_TextBlock("Here are the categories.")]),
        ])
        assert await bot._run_llm(1, "categories?") == "Here are the categories."

    async def test_tool_result_is_appended_to_messages(self, fake_llm, db_path):
        client = fake_llm([
            _reply([_ToolUseBlock("get_categories", {})], stop_reason="tool_use"),
            _reply([_TextBlock("done")]),
        ])
        await bot._run_llm(1, "categories?")
        second_call_messages = client.calls[1]["messages"]
        tool_result_msg = second_call_messages[-1]
        assert tool_result_msg["role"] == "user"
        assert tool_result_msg["content"][0]["type"] == "tool_result"
        assert tool_result_msg["content"][0]["tool_use_id"] == "tu_1"

    async def test_multiple_tools_in_one_turn(self, fake_llm, db_path):
        client = fake_llm([
            _reply(
                [
                    _ToolUseBlock("get_categories", {}, id="tu_1"),
                    _ToolUseBlock("get_delivery_slots", {}, id="tu_2"),
                ],
                stop_reason="tool_use",
            ),
            _reply([_TextBlock("both done")]),
        ])
        assert await bot._run_llm(1, "go") == "both done"
        assert len(client.calls[1]["messages"][-1]["content"]) == 2

    async def test_consecutive_tool_rounds(self, fake_llm, db_path):
        fake_llm([
            _reply([_ToolUseBlock("get_categories", {})], stop_reason="tool_use"),
            _reply([_ToolUseBlock("get_delivery_slots", {})], stop_reason="tool_use"),
            _reply([_TextBlock("finally")]),
        ])
        assert await bot._run_llm(1, "go") == "finally"

    async def test_failing_tool_does_not_break_loop(self, fake_llm, db_path):
        fake_llm([
            _reply([_ToolUseBlock("nonexistent_tool", {})], stop_reason="tool_use"),
            _reply([_TextBlock("recovered")]),
        ])
        assert await bot._run_llm(1, "go") == "recovered"


class TestRunLlmUnfinishedTurns:
    """Regression (C3): a turn that ends mid-tool_use must never be archived.

    When generation is cut off inside a tool_use block, stop_reason is
    "max_tokens" rather than "tool_use", so the tool-execution branch is
    skipped. Persisting that message leaves a tool_use with no tool_result,
    which the Messages API rejects — and nothing can repair it afterwards,
    because trimming only fixes the head and commit_turn only runs on success.
    The chat would 400 forever.
    """

    async def test_truncated_tool_use_is_not_persisted(self, fake_llm, db_path):
        fake_llm([_reply(
            [_TextBlock("let me look"), _ToolUseBlock("get_cart", {})],
            stop_reason="max_tokens",
        )])
        await bot._run_llm(100, "what's in the cart?")

        history._reset_caches()
        assert await history.get_context(100) == []
        assert await history.list_turns(100) == []

    async def test_truncated_tool_use_tells_the_user(self, fake_llm, db_path):
        fake_llm([_reply(
            [_ToolUseBlock("get_cart", {})], stop_reason="max_tokens",
        )])
        reply = await bot._run_llm(100, "hi")
        assert "ran out of room" in reply.lower()

    async def test_chat_still_works_after_a_truncated_turn(
        self, fake_llm, db_path
    ):
        """The proof that the poisoning is gone: the next turn succeeds."""
        client = fake_llm([
            _reply([_ToolUseBlock("get_cart", {})], stop_reason="max_tokens"),
            _reply([_TextBlock("all good")]),
        ])
        await bot._run_llm(100, "first")
        assert await bot._run_llm(100, "second") == "all good"

        # The retry must not carry the abandoned tool_use forward.
        sent = client.calls[1]["messages"]
        assert not any(
            isinstance(m["content"], list)
            and any(b.get("type") == "tool_use" for b in m["content"])
            for m in sent
        )
        assert len(await history.list_turns(100)) == 1   # only the good turn

    async def test_empty_response_is_not_persisted(self, fake_llm, db_path):
        fake_llm([_reply([])])
        assert await bot._run_llm(100, "hi") == "(no reply)"
        history._reset_caches()
        assert await history.get_context(100) == []


# ---------------------------------------------------------------------------
# _chat handler
# ---------------------------------------------------------------------------

class TestChatHandler:
    async def test_authorised_user_gets_reply(self, fake_llm, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_llm([_reply([_TextBlock("Sure!")])])
        update, context = _make_update(), _make_context()
        await bot._chat(update, context)
        update.message.reply_text.assert_awaited_once_with("Sure!")

    async def test_unauthorised_user_is_rejected(self, monkeypatch):
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "999")
        update, context = _make_update(user_id=1), _make_context()
        await bot._chat(update, context)
        assert "not authorised" in update.message.reply_text.await_args[0][0]

    async def test_typing_indicator_is_sent(self, fake_llm, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_llm([_reply([_TextBlock("ok")])])
        update, context = _make_update(), _make_context()
        await bot._chat(update, context)
        context.bot.send_chat_action.assert_awaited_once()

    async def test_explicit_text_overrides_message_text(self, fake_llm, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        client = fake_llm([_reply([_TextBlock("ok")])])
        await bot._chat(_make_update(text="ignored"), _make_context(), "use this")
        assert client.calls[0]["messages"][-1]["content"] == "use this"

    async def test_llm_error_returns_friendly_message(self, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")

        async def boom(chat_id, text):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(bot, "_run_llm", boom)
        update = _make_update()
        await bot._chat(update, _make_context())
        assert "something went wrong" in update.message.reply_text.await_args[0][0]

    async def test_long_reply_is_split_across_messages(self, fake_llm, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_llm([_reply([_TextBlock("z" * 5000)])])
        update = _make_update()
        await bot._chat(update, _make_context())
        assert update.message.reply_text.await_count == 2


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

class TestCommandHandlers:
    async def test_start_lists_commands(self, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        update = _make_update()
        await bot.cmd_start(update, _make_context())
        assert "/forecast" in update.message.reply_text.await_args[0][0]

    async def test_start_rejects_unauthorised(self, monkeypatch):
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "999")
        update = _make_update(user_id=1)
        await bot.cmd_start(update, _make_context())
        assert "not authorised" in update.message.reply_text.await_args[0][0]

    @pytest.mark.parametrize(
        "handler_name,expected_keyword",
        [
            ("cmd_plan", "meals"),
            ("cmd_order", "shopping"),
            ("cmd_forecast", "running low"),
            ("cmd_history", "recent"),
            ("cmd_cart", "cart"),
        ],
    )
    async def test_command_prompts_the_model(
        self, fake_llm, monkeypatch, handler_name, expected_keyword
    ):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        client = fake_llm([_reply([_TextBlock("ok")])])
        await getattr(bot, handler_name)(_make_update(), _make_context())
        sent = client.calls[0]["messages"][-1]["content"].lower()
        assert expected_keyword in sent

    @pytest.mark.parametrize(
        "handler_name",
        ["cmd_plan", "cmd_order", "cmd_forecast", "cmd_history", "cmd_cart"],
    )
    async def test_commands_reject_unauthorised(self, monkeypatch, handler_name):
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "999")
        update = _make_update(user_id=1)
        await getattr(bot, handler_name)(update, _make_context())
        assert "not authorised" in update.message.reply_text.await_args[0][0]


class TestModelCommand:
    async def test_shows_the_default_model(self, monkeypatch, db_path):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        update = _make_update()
        await bot.cmd_model(update, _make_context())
        reply = update.message.reply_text.await_args[0][0]
        assert llm.DEFAULT_MODEL in reply and "default" in reply

    async def test_shows_the_pinned_model(self, monkeypatch, db_path):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        await llm.set_chat_model(100, "openai/gpt-5")
        update = _make_update(chat_id=100)
        await bot.cmd_model(update, _make_context())
        reply = update.message.reply_text.await_args[0][0]
        assert "openai/gpt-5" in reply and "default" not in reply

    async def test_rejects_unauthorised(self, monkeypatch):
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "999")
        update = _make_update(user_id=1)
        await bot.cmd_model(update, _make_context())
        assert "not authorised" in update.message.reply_text.await_args[0][0]


class TestForgetCommand:
    async def test_deletes_stored_history(self, fake_llm, monkeypatch, db_path):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_llm([_reply([_TextBlock("ok")])])
        await bot._run_llm(100, "remember this")

        update = _make_update()
        await bot.cmd_forget(update, _make_context())

        assert await history.get_context(100) == []
        assert "deleted 2" in update.message.reply_text.await_args[0][0]

    async def test_reports_when_nothing_was_stored(self, monkeypatch, db_path):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        update = _make_update()
        await bot.cmd_forget(update, _make_context())
        assert "nothing stored" in update.message.reply_text.await_args[0][0].lower()

    async def test_rejects_unauthorised(self, monkeypatch, db_path):
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "999")
        update = _make_update(user_id=1)
        await bot.cmd_forget(update, _make_context())
        assert "not authorised" in update.message.reply_text.await_args[0][0]

    async def test_reports_failure_rather_than_claiming_success(
        self, monkeypatch, db_path
    ):
        """clear_history propagates by design, so the handler must catch it."""
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")

        async def boom(chat_id):
            raise RuntimeError("db down")

        monkeypatch.setattr(history, "clear_history", boom)
        update = _make_update()
        await bot.cmd_forget(update, _make_context())

        reply = update.message.reply_text.await_args[0][0].lower()
        assert "couldn't clear" in reply
        assert "forgotten" not in reply


class TestPrivacyCommand:
    def _ctx(self, *args):
        context = _make_context()
        context.args = list(args)
        return context

    async def test_reports_status_by_default(self, monkeypatch, db_path):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        update = _make_update()
        await bot.cmd_privacy(update, self._ctx())
        assert "ON" in update.message.reply_text.await_args[0][0]

    async def test_unrecognised_argument_falls_back_to_status(
        self, monkeypatch, db_path
    ):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        update = _make_update()
        await bot.cmd_privacy(update, self._ctx("maybe"))
        assert "currently" in update.message.reply_text.await_args[0][0]

    async def test_off_disables_and_deletes(self, fake_llm, monkeypatch, db_path):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_llm([_reply([_TextBlock("ok")])])
        await bot._run_llm(100, "secret")

        update = _make_update()
        await bot.cmd_privacy(update, self._ctx("off"))

        assert not await history.is_persistence_enabled(100)
        assert await history.list_turns(100) == []
        assert "deleted" in update.message.reply_text.await_args[0][0].lower()

    async def test_off_keeps_the_live_conversation(
        self, fake_llm, monkeypatch, db_path
    ):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_llm([_reply([_TextBlock("ok")])])
        await bot._run_llm(100, "secret")
        await bot.cmd_privacy(_make_update(), self._ctx("off"))
        assert len(await history.get_context(100)) == 2

    async def test_on_is_case_insensitive(self, monkeypatch, db_path):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        await bot.cmd_privacy(_make_update(), self._ctx("off"))
        await bot.cmd_privacy(_make_update(), self._ctx("ON"))
        assert await history.is_persistence_enabled(100)

    async def test_rejects_unauthorised(self, monkeypatch, db_path):
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "999")
        update = _make_update(user_id=1)
        await bot.cmd_privacy(update, self._ctx("off"))
        assert "not authorised" in update.message.reply_text.await_args[0][0]
        assert await history.is_persistence_enabled(100)

    async def test_reports_failure_rather_than_claiming_success(
        self, monkeypatch, db_path
    ):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")

        async def boom(chat_id, enabled):
            raise RuntimeError("db down")

        monkeypatch.setattr(history, "set_persistence", boom)
        update = _make_update()
        await bot.cmd_privacy(update, self._ctx("off"))

        reply = update.message.reply_text.await_args[0][0].lower()
        assert "couldn't change" in reply
        assert "has been deleted" not in reply


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class TestMain:
    def test_registers_handlers_and_starts_polling(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        app = MagicMock()
        builder = MagicMock()
        builder.token.return_value = builder
        builder.post_init.return_value = builder
        builder.build.return_value = app
        monkeypatch.setattr(bot.Application, "builder", lambda: builder)

        bot.main()

        assert app.add_handler.call_count == 12  # 11 commands + 1 message handler
        app.run_polling.assert_called_once()
        # The DB must be initialised on startup, before any update is handled.
        builder.post_init.assert_called_once_with(bot._post_init)

    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(KeyError):
            bot.main()


class TestPostInit:
    """The post_init hook is what creates the schema on a fresh deployment."""

    async def test_creates_schema_on_a_fresh_database(self, tmp_path, monkeypatch):
        from sqlalchemy import inspect

        from picnic_meal_planner.db import engine as engine_mod

        # Point at a database that has never been initialised.
        fresh = tmp_path / "fresh.db"
        monkeypatch.setenv("DB_PATH", str(fresh))
        await engine_mod._engine.dispose()
        engine_mod._engine = None
        engine_mod._session_factory = None

        await bot._post_init(MagicMock())

        async with engine_mod.get_engine().connect() as conn:
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        assert {"products", "orders", "order_items"} <= set(tables)

    async def test_refuses_to_start_when_the_schema_is_behind(self, db_path):
        """A skipped migration must stop the bot, not silently drop history."""
        from picnic_meal_planner.db import engine as engine_mod

        async with engine_mod.get_engine().begin() as conn:
            await conn.exec_driver_sql("ALTER TABLE chat_settings DROP COLUMN model")
        with pytest.raises(bot.SchemaOutOfDateError, match="alembic upgrade head") as exc:
            await bot._post_init(MagicMock())
        assert "chat_settings.model" in str(exc.value)

    async def test_refuses_to_start_on_a_0002_database(self, tmp_path, monkeypatch):
        """The real upgrade path: a DB left at revision 0002 by Alembic."""
        from alembic import command
        from alembic.config import Config

        from picnic_meal_planner.db import engine as engine_mod

        root = Path(__file__).resolve().parent.parent
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "alembic"))
        db_file = tmp_path / "at_0002.db"
        monkeypatch.setenv("DB_PATH", str(db_file))
        await engine_mod._engine.dispose()
        engine_mod._engine = None
        engine_mod._session_factory = None
        # env.py calls asyncio.run(), which cannot nest in the running loop.
        await asyncio.to_thread(command.upgrade, cfg, "0002")

        with pytest.raises(bot.SchemaOutOfDateError):
            await bot._post_init(MagicMock())

    async def test_refuses_a_0003_database_before_creating_new_tables(
        self, tmp_path, monkeypatch
    ):
        """init_db() must not run first: it would create picnic_auth outside
        Alembic, and `alembic upgrade head` would then fail on it."""
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import inspect

        from picnic_meal_planner.db import engine as engine_mod

        root = Path(__file__).resolve().parent.parent
        cfg = Config(str(root / "alembic.ini"))
        cfg.set_main_option("script_location", str(root / "alembic"))
        monkeypatch.setenv("DB_PATH", str(tmp_path / "at_0003.db"))
        await engine_mod._engine.dispose()
        engine_mod._engine = None
        engine_mod._session_factory = None
        await asyncio.to_thread(command.upgrade, cfg, "0003")

        with pytest.raises(bot.SchemaOutOfDateError, match="0003"):
            await bot._post_init(MagicMock())
        async with engine_mod.get_engine().connect() as conn:
            tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        assert "picnic_auth" not in tables
        await engine_mod._engine.dispose()
        # And the upgrade the error asks for still works.
        await asyncio.to_thread(command.upgrade, cfg, "head")

    async def test_is_safe_to_run_against_an_existing_database(self, db_path):
        """Restarting the bot must not fail or wipe data."""
        from picnic_meal_planner.db.models import Product
        from picnic_meal_planner.db.queries import get_db

        async with get_db() as session:
            session.add(Product(id="p_survivor", name="Survivor"))
            await session.commit()

        await bot._post_init(MagicMock())

        async with get_db() as session:
            assert await session.get(Product, "p_survivor") is not None
