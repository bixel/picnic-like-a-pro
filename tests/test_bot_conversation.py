"""Tests for the bot's Claude conversation loop and Telegram handlers.

The Anthropic client and Telegram Update/Context objects are replaced with
lightweight fakes, so these tests exercise the real control flow in bot.py
without any network access.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from picnic_meal_planner import bot, history


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Block:
    """Base for the fake SDK content blocks.

    The real Anthropic SDK returns pydantic v2 models, and history.py
    serialises them with ``model_dump(mode="json", exclude_none=True)``. These
    fakes implement that method so the tests exercise the same code path a real
    response would; a fake without it would silently skip the serialisation
    logic entirely.
    """

    _fields: tuple[str, ...] = ()

    def model_dump(self, mode=None, exclude_none=False) -> dict:
        data = {"type": self.type, **{f: getattr(self, f) for f in self._fields}}
        if exclude_none:
            data = {k: v for k, v in data.items() if v is not None}
        return data


class _TextBlock(_Block):
    type = "text"
    _fields = ("text", "citations")

    def __init__(self, text: str) -> None:
        self.text = text
        self.citations = None      # nullable on real TextBlocks


class _ToolUseBlock(_Block):
    type = "tool_use"
    _fields = ("id", "name", "input")

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

    async def test_history_is_persisted_after_success(self, fake_claude, db_path):
        fake_claude([_Response([_TextBlock("ok")])])
        await bot._run_claude(42, "remember this")
        context = await history.get_context(42)
        assert len(context) == 2
        assert context[0] == {"role": "user", "content": "remember this"}

    async def test_history_accumulates_across_turns(self, fake_claude, db_path):
        fake_claude([_Response([_TextBlock("a")]), _Response([_TextBlock("b")])])
        await bot._run_claude(7, "first")
        await bot._run_claude(7, "second")
        assert len(await history.get_context(7)) == 4

    async def test_history_survives_a_restart(self, fake_claude, db_path):
        """The point of persisting it: context outlives the process."""
        fake_claude([_Response([_TextBlock("ok")])])
        await bot._run_claude(42, "remember this")

        history._reset_caches()  # simulate a restart

        context = await history.get_context(42)
        assert len(context) == 2
        assert context[0] == {"role": "user", "content": "remember this"}

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
        assert await history.get_context(9) == []

    async def test_history_is_trimmed_to_max_turns(
        self, fake_claude, monkeypatch, db_path
    ):
        monkeypatch.setattr(history, "MAX_HISTORY_MESSAGES", 4)
        client = fake_claude([_Response([_TextBlock("ok")])])
        history._contexts[5] = [
            {"role": "user", "content": f"m{i}"} for i in range(10)
        ]
        history._loaded_chats.add(5)
        await bot._run_claude(5, "newest")
        assert len(client.calls[0]["messages"]) <= 4

    async def test_conversations_are_isolated_per_chat(self, fake_claude, db_path):
        fake_claude([_Response([_TextBlock("a")]), _Response([_TextBlock("b")])])
        await bot._run_claude(1, "chat one")
        await bot._run_claude(2, "chat two")
        one, two = await history.get_context(1), await history.get_context(2)
        assert one[0]["content"] == "chat one"
        assert two[0]["content"] == "chat two"
        assert len(two) == 2

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


class TestForgetCommand:
    async def test_deletes_stored_history(self, fake_claude, monkeypatch, db_path):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_claude([_Response([_TextBlock("ok")])])
        await bot._run_claude(100, "remember this")

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

    async def test_off_disables_and_deletes(self, fake_claude, monkeypatch, db_path):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_claude([_Response([_TextBlock("ok")])])
        await bot._run_claude(100, "secret")

        update = _make_update()
        await bot.cmd_privacy(update, self._ctx("off"))

        assert not await history.is_persistence_enabled(100)
        assert await history.list_turns(100) == []
        assert "deleted" in update.message.reply_text.await_args[0][0].lower()

    async def test_off_keeps_the_live_conversation(
        self, fake_claude, monkeypatch, db_path
    ):
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        fake_claude([_Response([_TextBlock("ok")])])
        await bot._run_claude(100, "secret")
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

        assert app.add_handler.call_count == 9   # 8 commands + 1 message handler
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
