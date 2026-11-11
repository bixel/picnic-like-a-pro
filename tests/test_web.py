"""Reporting UI: aggregation, login links, session cookies and the HTTP API."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio

from picnic_meal_planner import bot
from picnic_meal_planner.db import engine as engine_mod
from picnic_meal_planner.db import queries as db
from picnic_meal_planner.web import app as web_app
from picnic_meal_planner.web import login, reports

USER = 42


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", str(USER))
    monkeypatch.delenv("ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-secret")
    monkeypatch.delenv("WEB_BASE_URL", raising=False)


def _product(pid, l3=None, l2=None, l1=None, **names):
    return {
        "id": pid, "name": pid.title(),
        "l3_id": l3, "l3_name": names.get("l3_name"),
        "l2_id": l2, "l2_name": names.get("l2_name"),
        "l1_id": l1, "l1_name": names.get("l1_name"),
    }


PRODUCTS = [
    _product("milk", 30, 20, 10, l3_name="Milch", l2_name="Molkerei", l1_name="Frisch"),
    _product("oat", 30, 20, 10, l3_name="Milch", l2_name="Molkerei", l1_name="Frisch"),
    _product("pils", 31, 21, None, l3_name="Pils"),
    _product("bag"),
]
ORDERS = [
    {"id": 1, "date": "2026-01-05T10:00:00+01:00", "total_price": 1000},
    {"id": 2, "date": "2026-03-20T10:00:00+01:00", "total_price": 500},
    {"id": 3, "date": "", "total_price": 99},
]
ORDER_LINES = [
    {"order_id": 1, "product_id": "milk", "quantity": 2, "unit_price": 100},
    {"order_id": 1, "product_id": "pils", "quantity": 1, "unit_price": 600},
    {"order_id": 2, "product_id": "milk", "quantity": 1, "unit_price": 110},
    {"order_id": 2, "product_id": "oat", "quantity": 1, "unit_price": 200},
    {"order_id": 2, "product_id": "bag", "quantity": 3, "unit_price": None},
    {"order_id": 3, "product_id": "milk", "quantity": 9, "unit_price": 100},
]


def _lines(date_from=None, date_to=None):
    return reports.build_lines(reports.order_days(ORDERS, date_from, date_to), ORDER_LINES)


# ---------------------------------------------------------------------------
# reports.py
# ---------------------------------------------------------------------------

class TestReports:
    def test_undated_orders_are_left_out(self):
        assert set(reports.order_days(ORDERS)) == {1, 2}

    def test_date_range_is_inclusive(self):
        days = reports.order_days(ORDERS, date(2026, 1, 5), date(2026, 1, 5))
        assert days == {1: date(2026, 1, 5)}
        assert reports.order_days(ORDERS, date(2026, 1, 6), date(2026, 3, 19)) == {}

    def test_hierarchy_totals_add_up(self):
        tree = reports.hierarchy(_lines(), PRODUCTS)
        assert (tree["level"], tree["id"], tree["name"]) == ("global", "all", None)
        assert (tree["spend"], tree["units"], tree["orders"]) == (1110, 8, 2)
        assert sum(child["spend"] for child in tree["children"]) == tree["spend"]
        # Highest spend first.
        assert [(c["level"], c["id"], c["name"]) for c in tree["children"]] == [
            ("l1", "unknown", None),
            ("l1", "10", "Frisch"),
            ("uncategorised", "none", None),
        ]

    def test_hierarchy_descends_to_products(self):
        frisch = reports.hierarchy(_lines(), PRODUCTS)["children"][1]
        l2 = frisch["children"][0]
        l3 = l2["children"][0]
        assert (l2["level"], l2["name"], l3["level"], l3["name"]) == ("l2", "Molkerei", "l3", "Milch")
        assert [(p["level"], p["id"], p["spend"], p["orders"]) for p in l3["children"]] == [
            ("product", "milk", 310, 2),
            ("product", "oat", 200, 1),
        ]
        # A parent counts an order once, however many of its products are in it.
        assert l3["orders"] == 2

    def test_uncategorised_products_hang_below_their_own_node(self):
        node = reports.hierarchy(_lines(), PRODUCTS)["children"][2]
        assert [c["id"] for c in node["children"]] == ["bag"]
        assert node["spend"] == 0 and node["units"] == 3

    def test_hierarchy_only_lists_what_was_ordered(self):
        tree = reports.hierarchy(_lines(date_to=date(2026, 1, 31)), PRODUCTS)
        assert [c["id"] for c in tree["children"]] == ["unknown", "10"]

    def test_lines_of_unknown_products_are_ignored(self):
        assert reports.hierarchy(_lines(), [])["spend"] == 0

    def test_select_node(self):
        path, ids = reports.select_node(PRODUCTS, "l2", "20")
        assert ids == {"milk", "oat"}
        assert path == [
            {"level": "l1", "id": "10", "name": "Frisch"},
            {"level": "l2", "id": "20", "name": "Molkerei"},
        ]
        assert reports.select_node(PRODUCTS, "global", "all") == ([], {"milk", "oat", "pils", "bag"})
        assert reports.select_node(PRODUCTS, "uncategorised", "none")[1] == {"bag"}
        assert reports.select_node(PRODUCTS, "product", "pils")[0][-1]["name"] == "Pils"
        assert reports.select_node(PRODUCTS, "l3", "999") is None

    def test_monthly_series_fills_gaps(self):
        series = reports.timeseries(_lines(), "month")
        assert [(b["start"], b["spend"], b["orders"]) for b in series] == [
            ("2026-01-01", 800, 1), ("2026-02-01", 0, 0), ("2026-03-01", 310, 1),
        ]

    def test_weekly_series_starts_on_monday(self):
        series = reports.timeseries(_lines(), "week")
        assert series[0]["start"] == "2026-01-05" and series[-1]["start"] == "2026-03-16"
        assert len(series) == 11

    def test_month_buckets_cross_the_year_end(self):
        lines = [
            reports.Line(date(2025, 12, 31), 1, "milk", 1, 1),
            reports.Line(date(2026, 1, 1), 2, "milk", 1, 1),
        ]
        assert [b["start"] for b in reports.timeseries(lines, "month")] == ["2025-12-01", "2026-01-01"]

    def test_empty_series_and_summary(self):
        assert reports.timeseries([], "week") == []
        assert reports.summary([]) == {
            "spend": 0, "units": 0, "orders": 0, "first_order": None, "last_order": None,
        }

    def test_summary(self):
        assert reports.summary(_lines()) == {
            "spend": 1110, "units": 8, "orders": 2,
            "first_order": "2026-01-05", "last_order": "2026-03-20",
        }


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
