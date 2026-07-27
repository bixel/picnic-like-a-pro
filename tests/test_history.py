"""Tests for conversation history: serialization, truncation, storage, opt-out.

The database is isolated per test by the autouse ``isolated_db`` fixture in
conftest.py; ``reset_history_caches`` below clears history.py's in-process
caches, which is what a process restart looks like from the module's point of
view.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from picnic_meal_planner import history


@pytest.fixture(autouse=True)
def reset_history_caches():
    history._reset_caches()
    yield
    history._reset_caches()


CHAT = 4242

# A tool-using turn — the shape that must never be split by a delete or a trim.
TOOL_TURN = [
    {"role": "user", "content": "what are we low on?"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "tu_1", "name": "forecast_order",
             "input": {"horizon_days": 7}},
        ],
    },
    {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "milk, eggs"},
    ]},
    {"role": "assistant", "content": [{"type": "text", "text": "Milk and eggs."}]},
]

PLAIN_TURN = [
    {"role": "user", "content": "thanks"},
    {"role": "assistant", "content": [{"type": "text", "text": "Anytime!"}]},
]


async def _commit(chat_id: int, turn: list[dict]) -> None:
    """Append a turn the way bot._run_claude does."""
    base = await history.get_context(chat_id)
    messages = history.trim_history(base, history.MAX_HISTORY_MESSAGES - 1)
    new_from = len(messages)
    messages.extend(turn)
    await history.commit_turn(chat_id, messages, new_from)


# ---------------------------------------------------------------------------
# normalize_content
# ---------------------------------------------------------------------------

class _FakeBlock:
    """Mimics an Anthropic SDK pydantic content block."""

    def __init__(self, **data):
        self._data = data

    def model_dump(self, mode=None, exclude_none=False):
        data = dict(self._data)
        if exclude_none:
            data = {k: v for k, v in data.items() if v is not None}
        return data


class _LegacyBlock:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class TestNormalizeContent:
    def test_passes_strings_through(self):
        assert history.normalize_content("hello") == "hello"

    def test_maps_none_to_empty_string(self):
        assert history.normalize_content(None) == ""

    def test_passes_plain_dicts_through(self):
        blocks = [{"type": "text", "text": "hi"}]
        assert history.normalize_content(blocks) == blocks

    def test_dumps_sdk_blocks(self):
        out = history.normalize_content([_FakeBlock(type="text", text="hi")])
        assert out == [{"type": "text", "text": "hi"}]

    def test_drops_none_fields(self):
        """Nullable fields the API does not need echoed back are stripped."""
        out = history.normalize_content(
            [_FakeBlock(type="text", text="hi", citations=None)]
        )
        assert "citations" not in out[0]

    def test_output_is_json_serialisable(self):
        """The whole point: these values have to survive a round trip to SQLite."""
        out = history.normalize_content(
            [_FakeBlock(type="tool_use", id="t", name="n", input={"a": 1})]
        )
        assert json.loads(json.dumps(out)) == out

    def test_falls_back_to_to_dict(self):
        out = history.normalize_content([_LegacyBlock({"type": "text"})])
        assert out == [{"type": "text"}]

    def test_rejects_unserialisable_blocks(self):
        with pytest.raises(TypeError):
            history.normalize_content([object()])


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

class TestIsTurnStart:
    def test_plain_user_text_is_a_turn_start(self):
        assert history.is_turn_start({"role": "user", "content": "hi"})

    def test_assistant_is_never_a_turn_start(self):
        assert not history.is_turn_start({"role": "assistant", "content": "hi"})

    def test_tool_result_is_not_a_turn_start(self):
        assert not history.is_turn_start(
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t", "content": "x"}]}
        )

    def test_user_with_other_blocks_is_a_turn_start(self):
        assert history.is_turn_start(
            {"role": "user", "content": [{"type": "text", "text": "hi"}]}
        )


class TestTrimHistory:
    # A tool-heavy turn followed by a short one: a naive slice of the last 4
    # messages starts on the tool_result, which the API rejects with a 400.
    HISTORY = TOOL_TURN + [
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
    ]

    def test_the_naive_slice_would_orphan_a_tool_result(self):
        """Guards the premise of the test below — this is the bug being fixed."""
        assert not history.is_turn_start(self.HISTORY[-4:][0])

    def test_cuts_at_a_safe_boundary(self):
        assert history.is_turn_start(history.trim_history(self.HISTORY, 4)[0])

    def test_never_starts_on_an_assistant_message(self):
        for limit in range(1, len(self.HISTORY) + 1):
            trimmed = history.trim_history(self.HISTORY, limit)
            if trimmed:
                assert trimmed[0]["role"] == "user"

    def test_never_starts_on_a_tool_result(self):
        for limit in range(1, len(self.HISTORY) + 1):
            trimmed = history.trim_history(self.HISTORY, limit)
            if trimmed and isinstance(trimmed[0]["content"], list):
                assert not any(
                    b.get("type") == "tool_result" for b in trimmed[0]["content"]
                )

    def test_drops_forward_not_backward(self):
        """May drop more than strictly necessary — always the safe direction."""
        assert history.trim_history(self.HISTORY, 4) == self.HISTORY[4:]

    def test_returns_input_unchanged_under_the_limit(self):
        assert history.trim_history(self.HISTORY, 99) == self.HISTORY

    def test_returns_a_copy(self):
        assert history.trim_history(self.HISTORY, 99) is not self.HISTORY

    def test_zero_limit_returns_empty(self):
        assert history.trim_history(self.HISTORY, 0) == []

    def test_returns_empty_when_no_safe_boundary_exists(self):
        unsafe = [{"role": "assistant", "content": "x"}] * 5
        assert history.trim_history(unsafe, 2) == []


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class TestPersistence:
    async def test_a_fresh_chat_is_empty(self, db_path):
        assert await history.get_context(CHAT) == []

    async def test_a_turn_round_trips(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        assert await history.get_context(CHAT) == TOOL_TURN

    async def test_history_survives_a_restart(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        history._reset_caches()
        assert await history.get_context(CHAT) == TOOL_TURN

    async def test_tool_use_input_survives_a_restart(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        history._reset_caches()
        context = await history.get_context(CHAT)
        assert context[1]["content"][1]["input"] == {"horizon_days": 7}

    async def test_turns_accumulate(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        await _commit(CHAT, PLAIN_TURN)
        history._reset_caches()
        assert len(await history.get_context(CHAT)) == 6

    async def test_archive_keeps_more_than_the_api_window(self, db_path, monkeypatch):
        """The full transcript is kept; only what is sent to the API is capped."""
        await _commit(CHAT, PLAIN_TURN)
        await _commit(CHAT, PLAIN_TURN)
        await _commit(CHAT, PLAIN_TURN)
        monkeypatch.setattr(history, "MAX_HISTORY_MESSAGES", 2)
        history._reset_caches()

        assert len(await history.get_context(CHAT)) == 2      # window
        assert len(await history.list_turns(CHAT)) == 3       # archive

    async def test_chats_are_isolated(self, db_path):
        await _commit(1, TOOL_TURN)
        await _commit(2, PLAIN_TURN)
        history._reset_caches()
        assert len(await history.get_context(1)) == 4
        assert len(await history.get_context(2)) == 2

    async def test_list_turns_summarises(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        await _commit(CHAT, PLAIN_TURN)
        turns = await history.list_turns(CHAT)
        assert [t["message_count"] for t in turns] == [4, 2]
        assert turns[0]["preview"] == "what are we low on?"


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

class TestDeletion:
    async def test_clear_history_removes_everything(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        assert await history.clear_history(CHAT) == 4
        assert await history.get_context(CHAT) == []

    async def test_clear_history_is_durable(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        await history.clear_history(CHAT)
        history._reset_caches()
        assert await history.get_context(CHAT) == []

    async def test_delete_turns_removes_the_whole_turn(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        await _commit(CHAT, PLAIN_TURN)
        turns = await history.list_turns(CHAT)

        assert await history.delete_turns(CHAT, [turns[0]["turn_id"]]) == 4
        assert len(await history.get_context(CHAT)) == 2

    async def test_deleting_a_turn_leaves_no_orphaned_tool_result(self, db_path):
        """The reason a turn, not a message, is the unit of deletion."""
        await _commit(CHAT, TOOL_TURN)
        await _commit(CHAT, PLAIN_TURN)
        turns = await history.list_turns(CHAT)
        await history.delete_turns(CHAT, [turns[0]["turn_id"]])

        context = await history.get_context(CHAT)
        assert history.is_turn_start(context[0])
        assert not any(
            isinstance(m["content"], list)
            and any(b.get("type") == "tool_result" for b in m["content"])
            for m in context
        )

    async def test_deletion_invalidates_the_live_cache(self, db_path):
        """Without invalidation a deleted turn is re-sent to the API until restart."""
        await _commit(CHAT, TOOL_TURN)
        await _commit(CHAT, PLAIN_TURN)
        await history.get_context(CHAT)                 # warm the cache
        turns = await history.list_turns(CHAT)

        await history.delete_turns(CHAT, [turns[0]["turn_id"]])

        assert len(await history.get_context(CHAT)) == 2   # no restart in between

    async def test_delete_day_removes_todays_turns(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        assert await history.delete_day(CHAT, date.today()) == 4
        assert await history.get_context(CHAT) == []

    async def test_delete_day_ignores_other_days(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        assert await history.delete_day(CHAT, date.today() - timedelta(days=1)) == 0
        assert len(await history.get_context(CHAT)) == 4

    async def test_delete_turns_with_no_ids_is_a_noop(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        assert await history.delete_turns(CHAT, []) == 0
        assert len(await history.get_context(CHAT)) == 4


# ---------------------------------------------------------------------------
# Opt-out
# ---------------------------------------------------------------------------

class TestOptOut:
    async def test_enabled_by_default(self, db_path):
        assert await history.is_persistence_enabled(CHAT)

    async def test_opting_out_deletes_what_was_stored(self, db_path):
        await _commit(CHAT, TOOL_TURN)
        await history.set_persistence(CHAT, False)
        assert await history.list_turns(CHAT) == []

    async def test_opting_out_keeps_the_live_conversation(self, db_path):
        """Storage stops; the in-progress conversation must not vanish mid-chat."""
        await _commit(CHAT, TOOL_TURN)
        await history.set_persistence(CHAT, False)
        assert len(await history.get_context(CHAT)) == 4

    async def test_nothing_is_written_while_opted_out(self, db_path):
        await history.set_persistence(CHAT, False)
        await _commit(CHAT, TOOL_TURN)
        assert await history.list_turns(CHAT) == []

    async def test_opt_out_survives_a_restart(self, db_path):
        await history.set_persistence(CHAT, False)
        history._reset_caches()
        assert not await history.is_persistence_enabled(CHAT)
        assert await history.get_context(CHAT) == []

    async def test_opting_back_in_resumes_writing(self, db_path):
        await history.set_persistence(CHAT, False)
        await history.set_persistence(CHAT, True)
        await _commit(CHAT, TOOL_TURN)
        assert len(await history.list_turns(CHAT)) == 1

    async def test_global_kill_switch_disables_storage(self, db_path, monkeypatch):
        monkeypatch.setenv("PERSIST_CONVERSATIONS", "false")
        history._reset_caches()

        assert not await history.is_persistence_enabled(CHAT)
        await _commit(CHAT, TOOL_TURN)
        assert await history.list_turns(CHAT) == []

    async def test_kill_switch_keeps_in_session_memory(self, db_path, monkeypatch):
        monkeypatch.setenv("PERSIST_CONVERSATIONS", "false")
        history._reset_caches()
        await _commit(CHAT, TOOL_TURN)
        assert len(await history.get_context(CHAT)) == 4

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "NO"])
    async def test_kill_switch_accepts_falsey_spellings(
        self, db_path, monkeypatch, value
    ):
        monkeypatch.setenv("PERSIST_CONVERSATIONS", value)
        history._reset_caches()
        assert not await history.is_persistence_enabled(CHAT)


# ---------------------------------------------------------------------------
# Failure behaviour
# ---------------------------------------------------------------------------

class TestFailureBehaviour:
    """Reads and writes degrade to memory-only; deletions must not fail silently."""

    @pytest.fixture
    def broken_db(self, monkeypatch, tmp_path):
        from picnic_meal_planner.db import engine as engine_mod

        monkeypatch.setenv("DB_PATH", str(tmp_path / "no" / "such" / "dir" / "x.db"))
        engine_mod._engine = None
        engine_mod._session_factory = None
        history._reset_caches()

    async def test_get_context_degrades_instead_of_raising(self, broken_db):
        assert await history.get_context(CHAT) == []

    async def test_commit_turn_degrades_instead_of_raising(self, broken_db):
        await history.commit_turn(CHAT, list(PLAIN_TURN), 0)   # must not raise
        assert len(await history.get_context(CHAT)) == 2       # memory still works

    async def test_persistence_check_fails_closed(self, broken_db):
        """Never store when consent cannot be confirmed."""
        assert not await history.is_persistence_enabled(CHAT)

    async def test_clear_history_propagates(self, broken_db):
        with pytest.raises(Exception):
            await history.clear_history(CHAT)

    async def test_delete_turns_propagates(self, broken_db):
        with pytest.raises(Exception):
            await history.delete_turns(CHAT, [1])
