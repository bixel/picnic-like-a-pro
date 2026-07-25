"""Unit tests for bot.py helpers (message splitting, access control)."""

from __future__ import annotations

import random
import string

import pytest

from picnic_meal_planner import bot


class TestSplitMessage:
    def test_short_message_not_split(self):
        assert bot._split_message("Hello!") == ["Hello!"]

    def test_exact_length_not_split(self):
        text = "x" * 4096
        assert bot._split_message(text) == [text]

    def test_long_message_split_into_chunks(self):
        chunks = bot._split_message("x" * 4097)
        assert len(chunks) == 2
        assert chunks[0] == "x" * 4096
        assert chunks[1] == "x"

    def test_three_chunks(self):
        chunks = bot._split_message("y" * (4096 * 3))
        assert len(chunks) == 3
        assert all(len(c) == 4096 for c in chunks)

    def test_reassembled_text_equals_original(self):
        text = "".join(random.choices(string.ascii_letters, k=10000))
        assert "".join(bot._split_message(text)) == text

    def test_custom_max_len(self):
        chunks = bot._split_message("abc" * 10, max_len=10)
        assert len(chunks) == 3
        assert all(len(c) == 10 for c in chunks)

    def test_empty_string(self):
        assert bot._split_message("") == [""]


class TestAccessControl:
    def test_allowed_by_allowlist(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "123,456")
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        assert bot._is_allowed(123)
        assert bot._is_allowed(456)

    def test_blocked_user_not_in_allowlist(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "123,456")
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        assert not bot._is_allowed(999)

    def test_allow_all_users_overrides_allowlist(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "123")
        monkeypatch.setenv("ALLOW_ALL_USERS", "true")
        assert bot._is_allowed(999)

    def test_allow_all_users_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("ALLOW_ALL_USERS", "TRUE")
        assert bot._is_allowed(42)

    def test_allow_all_users_false_does_not_open_access(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "123")
        monkeypatch.setenv("ALLOW_ALL_USERS", "false")
        assert not bot._is_allowed(999)

    def test_empty_allowlist_blocks_everyone(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "")
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        assert not bot._is_allowed(123)

    def test_missing_allowlist_blocks_everyone(self, monkeypatch):
        monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        assert not bot._is_allowed(123)

    def test_whitespace_in_allowlist_ignored(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", " 123 , 456 ")
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        assert bot._is_allowed(123)
        assert bot._is_allowed(456)

    def test_allowed_user_ids_returns_set_of_ints(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "1,2,3")
        assert bot._allowed_user_ids() == {1, 2, 3}

    def test_allowed_user_ids_empty_when_unset(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "")
        assert bot._allowed_user_ids() == set()

    def test_trailing_comma_tolerated(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "123,456,")
        assert bot._allowed_user_ids() == {123, 456}


class TestAnthropicClient:
    def test_client_is_built_from_env_key(self, monkeypatch):
        monkeypatch.setattr(bot, "_anthropic_client", None)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert bot._get_anthropic_client() is not None

    def test_client_is_cached(self, monkeypatch):
        monkeypatch.setattr(bot, "_anthropic_client", None)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        assert bot._get_anthropic_client() is bot._get_anthropic_client()

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.setattr(bot, "_anthropic_client", None)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(KeyError):
            bot._get_anthropic_client()


class TestMcpToolWiring:
    async def test_get_mcp_tools_returns_schemas(self, monkeypatch):
        monkeypatch.setattr(bot, "_mcp_tools", None)
        tools = await bot._get_mcp_tools()
        assert len(tools) >= 13
        assert all("name" in t for t in tools)

    async def test_get_mcp_tools_is_cached(self, monkeypatch):
        monkeypatch.setattr(bot, "_mcp_tools", None)
        assert await bot._get_mcp_tools() is await bot._get_mcp_tools()

    async def test_call_mcp_tool_dispatches(self):
        assert isinstance(await bot._call_mcp_tool("get_categories", {}), list)

    async def test_call_mcp_tool_unknown_returns_error(self):
        assert "error" in await bot._call_mcp_tool("does_not_exist", {})
