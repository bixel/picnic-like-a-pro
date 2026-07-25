"""Tests for the bot's Claude conversation loop and Telegram handlers.

The Anthropic client and Telegram Update/Context objects are replaced with
lightweight fakes, so these tests exercise the real control flow in bot.py
without any network access.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picnic_meal_planner import bot


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, name: str, input_data: dict, id: str = "tu_1") -> None:
        self.name = name
        self.input = input_data
        self.id = id


class _Response:
    def __init__(self, content: list, stop_reason: str = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason


class _FakeAnthropic:
    """Returns queued responses in order; records the messages it was sent."""

    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.messages = MagicMock()
        self.messages.create = self._create

    async def _create(self, **kwargs):
        # bot._run_claude mutates the same list across tool rounds, so snapshot
        # it here to capture what was actually sent on this call.
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        return self._responses.pop(0)


@pytest.fixture
def fake_claude(monkeypatch):
    """Install a fake Anthropic client; the test supplies the response queue."""

    def _install(responses):
        client = _FakeAnthropic(responses)
        monkeypatch.setattr(bot, "_get_anthropic_client", lambda: client)
        monkeypatch.setattr(bot, "_mcp_tools", [])
        return client

    return _install


@pytest.fixture(autouse=True)
def clear_histories():
    bot._histories.clear()
    yield
    bot._histories.clear()


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
# _run_claude
# ---------------------------------------------------------------------------

class TestRunClaude:
    async def test_returns_plain_text_reply(self, fake_claude):
        fake_claude([_Response([_TextBlock("Hi there!")])])
        assert await bot._run_claude(1, "hello") == "Hi there!"

    async def test_concatenates_multiple_text_blocks(self, fake_claude):
        fake_claude([_Response([_TextBlock("Hello "), _TextBlock("world")])])
        assert await bot._run_claude(1, "hi") == "Hello world"

    async def test_empty_response_returns_placeholder(self, fake_claude):
        fake_claude([_Response([])])
        assert await bot._run_claude(1, "hi") == "(no reply)"

    async def test_history_is_persisted_after_success(self, fake_claude):
        fake_claude([_Response([_TextBlock("ok")])])
        await bot._run_claude(42, "remember this")
        assert len(bot._histories[42]) == 2
        assert bot._histories[42][0] == {"role": "user", "content": "remember this"}

    async def test_history_accumulates_across_turns(self, fake_claude):
        fake_claude([_Response([_TextBlock("a")]), _Response([_TextBlock("b")])])
        await bot._run_claude(7, "first")
        await bot._run_claude(7, "second")
        assert len(bot._histories[7]) == 4

    async def test_prior_history_is_sent_to_claude(self, fake_claude):
        client = fake_claude([_Response([_TextBlock("a")]), _Response([_TextBlock("b")])])
        await bot._run_claude(7, "first")
        await bot._run_claude(7, "second")
        assert len(client.calls[1]["messages"]) == 3

    async def test_history_not_updated_when_api_raises(self, fake_claude, monkeypatch):
        client = fake_claude([])

        async def boom(**kwargs):
            raise RuntimeError("api down")

        monkeypatch.setattr(client.messages, "create", boom)
        with pytest.raises(RuntimeError):
            await bot._run_claude(9, "hello")
        assert bot._histories[9] == []

    async def test_history_is_trimmed_to_max_turns(self, fake_claude, monkeypatch):
        monkeypatch.setattr(bot, "MAX_HISTORY_TURNS", 2)
        client = fake_claude([_Response([_TextBlock("ok")])])
        bot._histories[5] = [{"role": "user", "content": f"m{i}"} for i in range(10)]
        await bot._run_claude(5, "newest")
        assert len(client.calls[0]["messages"]) <= 4

    async def test_conversations_are_isolated_per_chat(self, fake_claude):
        fake_claude([_Response([_TextBlock("a")]), _Response([_TextBlock("b")])])
        await bot._run_claude(1, "chat one")
        await bot._run_claude(2, "chat two")
        assert bot._histories[1][0]["content"] == "chat one"
        assert bot._histories[2][0]["content"] == "chat two"
        assert len(bot._histories[2]) == 2

    async def test_system_prompt_is_sent(self, fake_claude):
        client = fake_claude([_Response([_TextBlock("ok")])])
        await bot._run_claude(1, "hi")
        assert client.calls[0]["system"] == bot.SYSTEM_PROMPT


class TestRunClaudeToolUse:
    async def test_executes_tool_then_returns_final_text(self, fake_claude, db_path):
        fake_claude([
            _Response([_ToolUseBlock("get_categories", {})], stop_reason="tool_use"),
            _Response([_TextBlock("Here are the categories.")]),
        ])
        assert await bot._run_claude(1, "categories?") == "Here are the categories."

    async def test_tool_result_is_appended_to_messages(self, fake_claude, db_path):
        client = fake_claude([
            _Response([_ToolUseBlock("get_categories", {})], stop_reason="tool_use"),
            _Response([_TextBlock("done")]),
        ])
        await bot._run_claude(1, "categories?")
        second_call_messages = client.calls[1]["messages"]
        tool_result_msg = second_call_messages[-1]
        assert tool_result_msg["role"] == "user"
        assert tool_result_msg["content"][0]["type"] == "tool_result"
        assert tool_result_msg["content"][0]["tool_use_id"] == "tu_1"

    async def test_multiple_tools_in_one_turn(self, fake_claude, db_path):
        client = fake_claude([
            _Response(
                [
                    _ToolUseBlock("get_categories", {}, id="tu_1"),
                    _ToolUseBlock("get_delivery_slots", {}, id="tu_2"),
                ],
                stop_reason="tool_use",
            ),
            _Response([_TextBlock("both done")]),
        ])
        assert await bot._run_claude(1, "go") == "both done"
        assert len(client.calls[1]["messages"][-1]["content"]) == 2

    async def test_consecutive_tool_rounds(self, fake_claude, db_path):
        fake_claude([
            _Response([_ToolUseBlock("get_categories", {})], stop_reason="tool_use"),
            _Response([_ToolUseBlock("get_delivery_slots", {})], stop_reason="tool_use"),
            _Response([_TextBlock("finally")]),
        ])
        assert await bot._run_claude(1, "go") == "finally"

    async def test_failing_tool_does_not_break_loop(self, fake_claude, db_path):
        fake_claude([
            _Response([_ToolUseBlock("nonexistent_tool", {})], stop_reason="tool_use"),
            _Response([_TextBlock("recovered")]),
        ])
        assert await bot._run_claude(1, "go") == "recovered"


# ---------------------------------------------------------------------------
# _chat handler
# ---------------------------------------------------------------------------

class TestChatHandler:
    async def test_authorised_user_gets_reply(self, fake_claude, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_claude([_Response([_TextBlock("Sure!")])])
        update, context = _make_update(), _make_context()
        await bot._chat(update, context)
        update.message.reply_text.assert_awaited_once_with("Sure!")

    async def test_unauthorised_user_is_rejected(self, monkeypatch):
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "999")
        update, context = _make_update(user_id=1), _make_context()
        await bot._chat(update, context)
        assert "not authorised" in update.message.reply_text.await_args[0][0]

    async def test_typing_indicator_is_sent(self, fake_claude, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_claude([_Response([_TextBlock("ok")])])
        update, context = _make_update(), _make_context()
        await bot._chat(update, context)
        context.bot.send_chat_action.assert_awaited_once()

    async def test_explicit_text_overrides_message_text(self, fake_claude, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        client = fake_claude([_Response([_TextBlock("ok")])])
        await bot._chat(_make_update(text="ignored"), _make_context(), "use this")
        assert client.calls[0]["messages"][-1]["content"] == "use this"

    async def test_claude_error_returns_friendly_message(self, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")

        async def boom(chat_id, text):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(bot, "_run_claude", boom)
        update = _make_update()
        await bot._chat(update, _make_context())
        assert "something went wrong" in update.message.reply_text.await_args[0][0]

    async def test_long_reply_is_split_across_messages(self, fake_claude, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_claude([_Response([_TextBlock("z" * 5000)])])
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
    async def test_command_prompts_claude(
        self, fake_claude, monkeypatch, handler_name, expected_keyword
    ):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        client = fake_claude([_Response([_TextBlock("ok")])])
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class TestMain:
    def test_registers_handlers_and_starts_polling(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
        app = MagicMock()
        builder = MagicMock()
        builder.token.return_value = builder
        builder.build.return_value = app
        monkeypatch.setattr(bot.Application, "builder", lambda: builder)

        bot.main()

        assert app.add_handler.call_count == 7   # 6 commands + 1 message handler
        app.run_polling.assert_called_once()

    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(KeyError):
            bot.main()
