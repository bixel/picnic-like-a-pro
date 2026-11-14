"""Reporting UI: aggregation, login links, session cookies and the HTTP API."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from picnic_meal_planner import bot
from picnic_meal_planner.db import engine as engine_mod
from picnic_meal_planner.db import queries as db
from picnic_meal_planner.web import app as web_app
from picnic_meal_planner.web import login

USER = 42


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", str(USER))
    monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-secret")
    monkeypatch.delenv("WEB_BASE_URL", raising=False)


# ---------------------------------------------------------------------------
# Login links and session cookies
# ---------------------------------------------------------------------------

def _token(link: str) -> str:
    return link.split("#token=")[1]


class TestLoginLinks:
    async def test_link_points_at_the_login_page(self, monkeypatch):
        monkeypatch.setenv("WEB_BASE_URL", "https://reports.example/")
        link = await login.create_login_link(USER)
        assert link.startswith("https://reports.example/login#token=")

    async def test_token_works_exactly_once(self):
        token = _token(await login.create_login_link(USER))
        assert await login.redeem_login_token(token) == USER
        assert await login.redeem_login_token(token) is None

    async def test_unknown_token_is_refused(self):
        assert await login.redeem_login_token("nope") is None

    async def test_only_the_hash_is_stored(self, db_session):
        token = _token(await login.create_login_link(USER))
        from sqlalchemy import select

        from picnic_meal_planner.db import WebLoginToken

        row = (await db_session.execute(select(WebLoginToken))).scalar_one()
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in (row.token_hash, row.expires_at)

    async def test_expired_token_is_refused_and_pruned(self, db_session):
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await db.create_login_token(db_session, "old", USER, past)
        await db_session.commit()
        assert await db.consume_login_token(db_session, "old") is None
        await db_session.commit()

        await login.create_login_link(USER)  # creating one drops the expired
        from sqlalchemy import func, select

        from picnic_meal_planner.db import WebLoginToken

        count = await db_session.scalar(select(func.count()).select_from(WebLoginToken))
        assert count == 1


class TestSessions:
    def test_round_trip(self):
        assert login.verify_session(login.sign_session(USER)) == USER

    def test_expiry(self):
        cookie = login.sign_session(USER, now=1000)
        ttl = login.SESSION_TTL.total_seconds()
        assert login.verify_session(cookie, now=1000 + ttl - 1) == USER
        assert login.verify_session(cookie, now=1000 + ttl) is None

    @pytest.mark.parametrize("value", [None, "", "garbage", "1.2.3", "a.b.c.d", "1.2.é"])
    def test_malformed_values(self, value):
        assert login.verify_session(value) is None

    def test_tampering_is_detected(self):
        _, expires, signature = login.sign_session(USER).split(".")
        assert login.verify_session(f"{USER + 1}.{expires}.{signature}") is None

    def test_signed_but_malformed_payload(self):
        assert login.verify_session(f"x.y.{login._signature('x.y')}") is None

    def test_another_secret_invalidates_sessions(self, monkeypatch):
        cookie = login.sign_session(USER)
        monkeypatch.setenv("WEB_SESSION_SECRET", "rotated")
        assert login.verify_session(cookie) is None

    def test_without_a_secret_one_is_made_up_per_process(self, monkeypatch):
        monkeypatch.delenv("WEB_SESSION_SECRET")
        monkeypatch.setattr(login, "_process_secret", None)
        cookie = login.sign_session(USER)
        assert login.verify_session(cookie) == USER
        monkeypatch.setattr(login, "_process_secret", None)  # "restart"
        assert login.verify_session(cookie) is None


# ---------------------------------------------------------------------------
# /report
# ---------------------------------------------------------------------------

def _update(user_id=USER, chat_type="private"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.type = chat_type
    update.message.reply_text = AsyncMock()
    return update


class TestReportCommand:
    async def test_sends_a_working_link_without_preview(self):
        update = _update()
        await bot.cmd_report(update, MagicMock())
        text = update.message.reply_text.call_args.args[0]
        assert update.message.reply_text.call_args.kwargs == {"disable_web_page_preview": True}
        token = _token(text.split("\n")[1])
        assert await login.redeem_login_token(token) == USER

    async def test_refuses_users_not_on_the_allowlist(self):
        update = _update(user_id=7)
        await bot.cmd_report(update, MagicMock())
        assert "not authorised" in update.message.reply_text.call_args.args[0]

    async def test_refuses_group_chats(self):
        update = _update(chat_type="group")
        await bot.cmd_report(update, MagicMock())
        text = update.message.reply_text.call_args.args[0]
        assert "private chat" in text and "token=" not in text

    async def test_reports_a_failure(self, monkeypatch):
        monkeypatch.setattr(login, "create_login_link", AsyncMock(side_effect=RuntimeError))
        update = _update()
        await bot.cmd_report(update, MagicMock())
        assert "couldn't create" in update.message.reply_text.call_args.args[0]


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(seeded_db_path):
    transport = httpx.ASGITransport(app=web_app.create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def user(client):
    """The client, logged in."""
    token = _token(await login.create_login_link(USER))
    assert (await client.post("/api/auth/login", json={"token": token})).status_code == 200
    return client


class TestAuthApi:
    @pytest.mark.parametrize("path", ["/api/me", "/api/hierarchy", "/api/timeseries"])
    async def test_everything_needs_a_session(self, client, path):
        assert (await client.get(path)).status_code == 401

    async def test_login_sets_an_http_only_cookie(self, client):
        token = _token(await login.create_login_link(USER))
        response = await client.post("/api/auth/login", json={"token": token})
        assert response.json() == {"user_id": USER}
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie
        assert (await client.get("/api/me")).json() == {"user_id": USER}

    async def test_cookie_is_secure_behind_https(self, client, monkeypatch):
        monkeypatch.setenv("WEB_BASE_URL", "https://reports.example")
        token = _token(await login.create_login_link(USER))
        response = await client.post("/api/auth/login", json={"token": token})
        assert "secure" in response.headers["set-cookie"].lower()

    async def test_a_link_cannot_be_used_twice(self, client):
        token = _token(await login.create_login_link(USER))
        await client.post("/api/auth/login", json={"token": token})
        client.cookies.clear()
        response = await client.post("/api/auth/login", json={"token": token})
        assert response.status_code == 401 and "set-cookie" not in response.headers

    async def test_a_link_of_a_removed_user_is_refused(self, client, monkeypatch):
        token = _token(await login.create_login_link(USER))
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "")
        assert (await client.post("/api/auth/login", json={"token": token})).status_code == 401

    async def test_removing_a_user_ends_their_session(self, user, monkeypatch):
        monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "")
        assert (await user.get("/api/me")).status_code == 401

    async def test_logout(self, user):
        assert (await user.post("/api/auth/logout")).status_code == 204
        assert (await user.get("/api/me")).status_code == 401

    async def test_api_responses_are_not_cached(self, user):
        response = await user.get("/api/me")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


class TestReportApi:
    async def test_hierarchy_of_the_seeded_history(self, user, db_session):
        await db.upsert_category_parent(db_session, 1, 1000, "Frisch")
        await db_session.commit()

        tree = (await user.get("/api/hierarchy")).json()
        assert tree["level"] == "global" and tree["orders"] == 25
        assert sum(c["spend"] for c in tree["children"]) == tree["spend"] > 0
        l1 = {c["id"]: c for c in tree["children"]}
        assert l1["1000"]["name"] == "Frisch"
        assert l1["1001"]["name"] is None  # not looked up yet
        leaf = l1["1000"]["children"][0]["children"][0]["children"][0]
        assert leaf["level"] == "product" and leaf["name"]

    async def test_hierarchy_respects_the_date_range(self, user):
        tree = (await user.get("/api/hierarchy", params={"from": "2999-01-01"})).json()
        assert tree["orders"] == 0 and tree["children"] == []

    async def test_global_series(self, user):
        data = (await user.get("/api/timeseries")).json()
        assert data["path"] == [] and data["bucket"] == "month"
        assert data["summary"]["orders"] == 25
        assert sum(b["spend"] for b in data["buckets"]) == data["summary"]["spend"]
        # The seeded order totals are exactly the line sums.
        assert data["summary"]["order_total"] == data["summary"]["spend"]

    async def test_category_series_matches_the_tree(self, user):
        tree = (await user.get("/api/hierarchy")).json()
        node = tree["children"][0]
        data = (await user.get(
            "/api/timeseries", params={"level": "l1", "id": node["id"], "bucket": "week"}
        )).json()
        assert data["summary"]["spend"] == node["spend"]
        assert data["summary"]["order_total"] is None
        assert [c["level"] for c in data["path"]] == ["l1"]
        assert date.fromisoformat(data["buckets"][0]["start"]).weekday() == 0

    async def test_product_series_has_the_full_path(self, user):
        data = (await user.get(
            "/api/timeseries", params={"level": "product", "id": "p_milk_whole"}
        )).json()
        assert [c["level"] for c in data["path"]] == ["l1", "l2", "l3", "product"]
        assert data["summary"]["orders"] == 25

    async def test_unknown_node_is_404(self, user):
        response = await user.get("/api/timeseries", params={"level": "l3", "id": "1"})
        assert response.status_code == 404

    async def test_invalid_parameters_are_rejected(self, user):
        assert (await user.get("/api/timeseries", params={"bucket": "day"})).status_code == 422
        assert (await user.get("/api/hierarchy", params={"from": "soon"})).status_code == 422


class TestFrontend:
    async def test_without_a_build_there_is_no_page(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("WEB_STATIC_DIR", str(tmp_path / "missing"))
        assert (await client.get("/")).status_code == 404

    async def test_serves_files_and_falls_back_to_the_index(self, client, monkeypatch, tmp_path):
        (tmp_path / "assets").mkdir()
        (tmp_path / "index.html").write_text("<html>app</html>")
        (tmp_path / "assets" / "app.js").write_text("js")
        (tmp_path.parent / "secret.txt").write_text("secret")
        monkeypatch.setenv("WEB_STATIC_DIR", str(tmp_path))

        assert (await client.get("/")).text == "<html>app</html>"
        assert (await client.get("/assets/app.js")).text == "js"
        assert (await client.get("/login")).text == "<html>app</html>"
        assert (await client.get("/..%2Fsecret.txt")).text == "<html>app</html>"
        assert (await client.get("/api/nope")).status_code == 404


class TestApiSchema:
    def test_committed_openapi_matches_the_app(self):
        """The frontend's types are generated from this file; CI checks that
        step, this checks the file itself.  Fix with `make api-types`."""
        committed = Path(__file__).resolve().parent.parent / "frontend" / "openapi.json"
        assert json.loads(committed.read_text()) == web_app.create_app().openapi()


class TestStartup:
    async def test_current_schema_passes(self):
        async with web_app._lifespan(None):
            pass

    async def test_pending_migration_stops_the_app(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "pending_migration", AsyncMock(return_value="0010"))
        with pytest.raises(engine_mod.SchemaOutOfDateError, match="starting the web app"):
            async with web_app._lifespan(None):
                pass

    async def test_missing_columns_stop_the_app(self, monkeypatch):
        monkeypatch.setattr(engine_mod, "missing_columns", AsyncMock(return_value=["orders.x"]))
        with pytest.raises(engine_mod.SchemaOutOfDateError, match="orders.x"):
            async with web_app._lifespan(None):
                pass

    def test_main_serves_on_localhost_by_default(self, monkeypatch):
        import uvicorn

        monkeypatch.delenv("WEB_HOST", raising=False)
        monkeypatch.delenv("WEB_PORT", raising=False)
        run = MagicMock()
        monkeypatch.setattr(uvicorn, "run", run)
        monkeypatch.setattr(web_app, "load_dotenv", lambda: None)
        web_app.main()
        assert run.call_args.kwargs == {"host": "127.0.0.1", "port": 8080}
