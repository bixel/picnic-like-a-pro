"""Tests for the Picnic login: stored token, 2FA flow, expiry, rotation.

``FakePicnicAPI`` mirrors the parts of python-picnic-api2's ``PicnicAPI`` the
login touches: the constructor never logs in by itself (no credentials are
passed), ``login`` sets a token or raises ``Picnic2FARequired``, and the 2FA
code is verified against the same instance that requested it.
"""

from __future__ import annotations

import pytest
from python_picnic_api2 import Picnic2FAError, Picnic2FARequired, PicnicAuthError

from picnic_meal_planner.db import queries as db
from picnic_meal_planner.picnic import auth
from picnic_meal_planner.picnic import client as picnic


class _Session:
    def __init__(self, token):
        self.auth_token = token


class FakePicnicAPI:
    instances: list[FakePicnicAPI] = []
    needs_2fa = False
    valid_tokens: set[str] = set()
    rotate_to: str | None = None

    def __init__(self, country_code="NL", auth_token=None):
        self.country_code = country_code
        self.session = _Session(auth_token)
        self.logins = 0
        self.code_requests: list[str] = []
        FakePicnicAPI.instances.append(self)

    def login(self, username, password):
        self.logins += 1
        if password != "right":
            raise PicnicAuthError("Picnic authentication error: Invalid credentials")
        if FakePicnicAPI.needs_2fa:
            self.session.auth_token = "half-token"
            raise Picnic2FARequired()
        self._grant("password-token")

    def generate_2fa_code(self, channel):
        self.code_requests.append(channel)

    def verify_2fa_code(self, code):
        if self.session.auth_token != "half-token":
            raise AssertionError("code verified on a session that never asked for one")
        if code != "123456":
            raise Picnic2FAError("Invalid OTP", code="OTP_INVALID")
        self._grant("2fa-token")

    def _grant(self, token):
        self.session.auth_token = token
        FakePicnicAPI.valid_tokens.add(token)

    def get_user(self):
        if self.session.auth_token not in FakePicnicAPI.valid_tokens:
            raise PicnicAuthError("Picnic authentication error")
        if FakePicnicAPI.rotate_to:
            self._grant(FakePicnicAPI.rotate_to)
        return {"user_id": "u1", "firstname": "Kim"}


@pytest.fixture(autouse=True)
def real_mode(monkeypatch):
    """Real (non-mock) mode against FakePicnicAPI, from a clean client."""
    import python_picnic_api2

    FakePicnicAPI.instances = []
    FakePicnicAPI.needs_2fa = False
    FakePicnicAPI.valid_tokens = set()
    FakePicnicAPI.rotate_to = None
    monkeypatch.setattr(python_picnic_api2, "PicnicAPI", FakePicnicAPI)
    monkeypatch.setenv("PICNIC_MOCK", "false")
    monkeypatch.setenv("PICNIC_USERNAME", "kim@example.com")
    monkeypatch.setenv("PICNIC_PASSWORD", "right")
    monkeypatch.setenv("PICNIC_COUNTRY_CODE", "DE")
    picnic.reset_client()
    yield
    picnic.reset_client()


async def _stored_token():
    async with db.get_db() as session:
        return await db.get_picnic_token(session)


async def _store(token):
    async with db.get_db() as session:
        await db.set_picnic_token(session, token)
        await session.commit()


class TestTokenQueries:
    async def test_round_trip_and_overwrite(self, db_session):
        assert await db.get_picnic_token(db_session) is None
        await db.set_picnic_token(db_session, "a")
        await db.set_picnic_token(db_session, "b")
        await db_session.commit()
        assert await db.get_picnic_token(db_session) == "b"

    async def test_delete_only_if_spares_a_newer_token(self, db_session):
        await db.set_picnic_token(db_session, "new")
        assert await db.delete_picnic_token(db_session, only_if="old") == 0
        assert await db.get_picnic_token(db_session) == "new"
        assert await db.delete_picnic_token(db_session, only_if="new") == 1
        assert await db.get_picnic_token(db_session) is None


class TestClientConstruction:
    async def test_stored_token_is_used_without_password_login(self):
        await _store("stored")
        FakePicnicAPI.valid_tokens.add("stored")
        client = await picnic.get_client()
        assert client.session.auth_token == "stored"
        assert client.logins == 0
        assert client.country_code == "DE"

    async def test_password_login_without_2fa_stores_the_token(self):
        client = await picnic.get_client()
        assert client.logins == 1
        assert await _stored_token() == "password-token"

    async def test_2fa_demand_raises_and_sends_no_code(self):
        FakePicnicAPI.needs_2fa = True
        with pytest.raises(picnic.PicnicLoginRequired, match="/picnic_login"):
            await picnic.get_client()
        assert FakePicnicAPI.instances[0].code_requests == []
        assert await _stored_token() is None

    async def test_2fa_demand_is_not_retried_on_every_call(self):
        FakePicnicAPI.needs_2fa = True
        for _ in range(3):
            with pytest.raises(picnic.PicnicLoginRequired):
                await picnic.get_client()
        assert len(FakePicnicAPI.instances) == 1

    async def test_token_stored_elsewhere_clears_the_2fa_demand(self):
        """picnic_login.py in another process stores a token; no restart needed."""
        FakePicnicAPI.needs_2fa = True
        with pytest.raises(picnic.PicnicLoginRequired):
            await picnic.get_client()
        await _store("from-script")
        FakePicnicAPI.valid_tokens.add("from-script")
        assert (await picnic.get_client()).session.auth_token == "from-script"

    async def test_wrong_password_propagates_auth_error(self, monkeypatch):
        monkeypatch.setenv("PICNIC_PASSWORD", "wrong")
        with pytest.raises(PicnicAuthError):
            await picnic.get_client()

    async def test_missing_credentials_ask_for_login(self, monkeypatch):
        monkeypatch.delenv("PICNIC_PASSWORD")
        with pytest.raises(picnic.PicnicLoginRequired, match="PICNIC_PASSWORD"):
            await picnic.get_client()


class TestCalls:
    async def test_rejected_token_is_cleared_and_reported(self):
        await _store("expired")
        with pytest.raises(picnic.PicnicLoginRequired, match="expired"):
            await picnic.get_user()
        assert await _stored_token() is None
        assert picnic._client is None

    async def test_rejected_token_spares_a_newer_stored_one(self):
        await _store("expired")
        await picnic.get_client()
        await _store("fresh")  # another process logged in meanwhile
        with pytest.raises(picnic.PicnicLoginRequired):
            await picnic.get_user()
        assert await _stored_token() == "fresh"

    async def test_rotated_token_is_written_back(self):
        await _store("stored")
        FakePicnicAPI.valid_tokens.add("stored")
        FakePicnicAPI.rotate_to = "rotated"
        assert (await picnic.get_user())["firstname"] == "Kim"
        assert await _stored_token() == "rotated"

    async def test_tool_error_message_tells_the_user_what_to_do(self):
        """MCP tools surface exceptions as {"error": str(exc)}."""
        from picnic_meal_planner import mcp_server

        FakePicnicAPI.needs_2fa = True
        result = await mcp_server.call_tool("get_cart", {})
        assert "/picnic_login" in result["error"]


class TestLoginFlow:
    async def test_no_2fa_logs_in_directly(self):
        assert await picnic.start_login() is True
        assert await _stored_token() == "password-token"
        assert picnic.pending_login_channel() is None

    async def test_full_2fa_flow(self):
        FakePicnicAPI.needs_2fa = True
        assert await picnic.start_login("email") is False
        assert picnic.pending_login_channel() == "EMAIL"
        assert FakePicnicAPI.instances[-1].code_requests == ["EMAIL"]

        await picnic.submit_login_code(" 123456 ")
        assert await _stored_token() == "2fa-token"
        assert picnic.pending_login_channel() is None
        # The installed client is the one that completed 2FA, not a new login.
        assert (await picnic.get_user())["user_id"] == "u1"
        assert len(FakePicnicAPI.instances) == 1

    async def test_wrong_code_keeps_the_login_pending(self):
        FakePicnicAPI.needs_2fa = True
        await picnic.start_login()
        with pytest.raises(Picnic2FAError):
            await picnic.submit_login_code("000000")
        assert picnic.pending_login_channel() == "SMS"
        await picnic.submit_login_code("123456")
        assert await _stored_token() == "2fa-token"

    async def test_code_without_pending_login(self):
        with pytest.raises(picnic.PicnicLoginRequired, match="/picnic_login"):
            await picnic.submit_login_code("123456")

    async def test_expired_pending_login(self, monkeypatch):
        FakePicnicAPI.needs_2fa = True
        await picnic.start_login()
        monkeypatch.setattr(auth, "CODE_TTL_SECONDS", -1)
        assert picnic.pending_login_channel() is None
        with pytest.raises(picnic.PicnicLoginRequired):
            await picnic.submit_login_code("123456")

    async def test_2fa_login_clears_the_fail_fast_flag(self):
        FakePicnicAPI.needs_2fa = True
        with pytest.raises(picnic.PicnicLoginRequired):
            await picnic.get_cart()
        await picnic.start_login()
        await picnic.submit_login_code("123456")
        assert picnic._needs_login is False

    async def test_unknown_channel_is_rejected(self):
        with pytest.raises(ValueError, match="SMS"):
            await picnic.start_login("pigeon")

    async def test_logout_forgets_the_token(self):
        await picnic.start_login()
        assert await picnic.logout() == 1
        assert await _stored_token() is None
        assert picnic._client is None


class TestTokenStoreDegrades:
    async def test_unreadable_db_falls_back_to_password_login(self, monkeypatch):
        async def boom(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(db, "get_picnic_token", boom)
        monkeypatch.setattr(db, "set_picnic_token", boom)
        client = await picnic.get_client()
        assert client.session.auth_token == "password-token"


class TestTelegramCommands:
    """/picnic_login and /picnic_code, driven through bot.py's handlers."""

    @pytest.fixture(autouse=True)
    def allowed(self, monkeypatch):
        monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "1,2")
        monkeypatch.delenv("PICNIC_LOGIN_USER_IDS", raising=False)

    @staticmethod
    def _call(handler, *args, user_id=1):
        from unittest.mock import AsyncMock, MagicMock

        update = MagicMock()
        update.effective_user.id = user_id
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.args = list(args)
        return update, handler(update, context)

    async def _reply(self, handler, *args, user_id=1) -> str:
        update, coro = self._call(handler, *args, user_id=user_id)
        await coro
        return update.message.reply_text.call_args.args[0]

    async def test_full_flow_over_telegram(self):
        from picnic_meal_planner import bot

        FakePicnicAPI.needs_2fa = True
        reply = await self._reply(bot.cmd_picnic_login, "email")
        assert "by email" in reply and "/picnic_code" in reply
        assert "Logged in" in await self._reply(bot.cmd_picnic_code, "123456")
        assert await _stored_token() == "2fa-token"

    async def test_no_code_needed(self):
        from picnic_meal_planner import bot

        assert "no code was needed" in await self._reply(bot.cmd_picnic_login)

    async def test_wrong_code_invites_a_retry(self):
        from picnic_meal_planner import bot

        FakePicnicAPI.needs_2fa = True
        await self._reply(bot.cmd_picnic_login)
        assert "didn't accept" in await self._reply(bot.cmd_picnic_code, "999999")
        assert picnic.pending_login_channel() == "SMS"

    async def test_code_without_login(self):
        from picnic_meal_planner import bot

        assert "/picnic_login" in await self._reply(bot.cmd_picnic_code, "123456")

    async def test_code_command_needs_an_argument(self):
        from picnic_meal_planner import bot

        assert "Usage" in await self._reply(bot.cmd_picnic_code)

    async def test_bad_channel_shows_usage(self):
        from picnic_meal_planner import bot

        assert "Usage" in await self._reply(bot.cmd_picnic_login, "fax")

    async def test_wrong_password_is_reported(self, monkeypatch):
        from picnic_meal_planner import bot

        monkeypatch.setenv("PICNIC_PASSWORD", "wrong")
        assert "rejected" in await self._reply(bot.cmd_picnic_login)

    async def test_unlisted_user_is_rejected(self):
        from picnic_meal_planner import bot

        assert "not authorised" in await self._reply(bot.cmd_picnic_login, user_id=99)
        assert FakePicnicAPI.instances == []

    async def test_login_user_ids_narrow_who_may_log_in(self, monkeypatch):
        from picnic_meal_planner import bot

        monkeypatch.setenv("PICNIC_LOGIN_USER_IDS", "1")
        assert "not authorised" in await self._reply(bot.cmd_picnic_login, user_id=2)
        assert "not authorised" in await self._reply(bot.cmd_picnic_code, "1", user_id=2)
        assert "Logged in" in await self._reply(bot.cmd_picnic_login, user_id=1)

    async def test_mock_mode_has_nothing_to_log_in_to(self, monkeypatch):
        from picnic_meal_planner import bot

        monkeypatch.setenv("PICNIC_MOCK", "true")
        assert "mocked" in await self._reply(bot.cmd_picnic_login)


def _load_login_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "picnic_login.py"
    spec = importlib.util.spec_from_file_location("picnic_login", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestLoginScript:
    """scripts/picnic_login.py, in-process so FakePicnicAPI stands in for Picnic."""

    @pytest.fixture
    def script(self):
        return _load_login_script()

    async def test_2fa_login_with_a_retry(self, script, capsys):
        FakePicnicAPI.needs_2fa = True
        codes = iter(["000000", "123456"])
        assert await script._login("EMAIL", read_code=lambda _: next(codes)) == 0
        out = capsys.readouterr().out
        assert "by email" in out and "2 attempt(s) left" in out and "Logged in as Kim" in out
        assert await _stored_token() == "2fa-token"

    async def test_gives_up_after_three_wrong_codes(self, script):
        FakePicnicAPI.needs_2fa = True
        assert await script._login("SMS", read_code=lambda _: "000000") == 1
        assert await _stored_token() is None

    async def test_wrong_password(self, script, monkeypatch, capsys):
        monkeypatch.setenv("PICNIC_PASSWORD", "wrong")
        assert await script._login("SMS") == 1
        assert "rejected" in capsys.readouterr().err

    async def test_status_and_logout(self, script, capsys):
        assert await script.main(["--status"]) == 1
        await picnic.start_login()
        picnic.reset_client()  # as a separate process would see it
        assert await script.main(["--status"]) == 0
        assert await script.main(["--logout"]) == 0
        assert await _stored_token() is None
        assert "removed" in capsys.readouterr().out

    async def test_refuses_an_unmigrated_database(self, script, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("DB_PATH", str(tmp_path / "missing.db"))
        assert await script.main(["--status"]) == 1
        assert "alembic upgrade head" in capsys.readouterr().err

    async def test_unreachable_picnic_is_reported_without_a_traceback(
        self, script, monkeypatch, capsys
    ):
        import requests

        def offline(self, *a):
            raise requests.ConnectionError("tunnel failed")

        monkeypatch.setattr(FakePicnicAPI, "login", offline)
        assert await script.main([]) == 1
        assert "Cannot reach Picnic" in capsys.readouterr().err


class TestPreflight:
    @pytest.fixture
    def preflight(self, monkeypatch):
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "scripts" / "preflight.py"
        spec = importlib.util.spec_from_file_location("preflight_auth", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "_results", [])
        return module

    @staticmethod
    def _api_result(preflight):
        [(status, _, detail)] = [r for r in preflight._results if r[1] == "picnic api"]
        return status, detail

    async def test_2fa_account_points_at_the_login_commands(self, preflight):
        FakePicnicAPI.needs_2fa = True
        await preflight.check_picnic()
        status, detail = self._api_result(preflight)
        assert status == preflight.FAIL and "/picnic_login" in detail
        assert FakePicnicAPI.instances[0].code_requests == []

    async def test_stored_session_passes_without_a_password_login(self, preflight):
        await _store("stored")
        FakePicnicAPI.valid_tokens.add("stored")
        await preflight.check_picnic()
        assert self._api_result(preflight) == (
            preflight.OK,
            "logged in as Kim — 0 completed deliveries  <-- confirm this is the right account",
        )
        assert FakePicnicAPI.instances[0].logins == 0

    async def test_expired_session_fails(self, preflight):
        await _store("expired")
        await preflight.check_picnic()
        status, detail = self._api_result(preflight)
        assert status == preflight.FAIL and "expired" in detail

