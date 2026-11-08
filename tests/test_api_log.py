"""Tests for api_log.py: what gets recorded, and what happens when it cannot.

The per-test SQLite database from conftest is already initialised with the
``api_requests`` table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime

import pytest
import requests
from requests.adapters import BaseAdapter

from picnic_meal_planner import api_log
from picnic_meal_planner.db import queries as db


def _timer() -> api_log.Timer:
    return api_log.Timer(started_perf=time.perf_counter(), requested_at="2026-01-01T00:00:00+00:00")


async def _rows(**filters) -> list[dict]:
    async with db.get_db() as session:
        return await db.list_api_requests(session, **filters)


class _ApiError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class TestRecord:
    async def test_success_row_records_http_details_and_bodies(self):
        await api_log.record(
            api_log.PROVIDER_PICNIC,
            "get_delivery",
            _timer(),
            target="d1",
            http_method="GET",
            url="https://storefront.example/api/15/delivery/d1",
            request_headers={
                "Authorization": "Bearer secret",
                "X-Title": "test",
            },
            response_headers={
                "x-picnic-auth": "new-token",
                "Content-Type": "application/json",
            },
            request_body={"args": ["d1"]},
            response_body={"id": "d1", "items": []},
        )
        [row] = await _rows()
        assert row["provider"] == "picnic"
        assert row["operation"] == "get_delivery"
        assert row["target"] == "d1"
        assert row["http_method"] == "GET"
        assert row["url"] == "https://storefront.example/api/15/delivery/d1"
        assert row["status"] == "success"
        assert row["http_status"] is None
        assert row["duration_ms"] >= 0
        assert row["error_type"] is None
        assert row["error_message"] is None
        assert json.loads(row["request_headers"]) == {
            "Authorization": f"Bearer {api_log.REDACTED}",
            "X-Title": "test",
        }
        assert json.loads(row["response_headers"]) == {
            "x-picnic-auth": api_log.REDACTED,
            "Content-Type": "application/json",
        }
        assert json.loads(row["request_body"]) == {"args": ["d1"]}
        assert json.loads(row["response_body"]) == {"id": "d1", "items": []}
        assert row["requested_at"] == "2026-01-01T00:00:00+00:00"

    async def test_credentials_are_redacted_everywhere_in_a_body(self):
        await api_log.record(
            api_log.PROVIDER_PICNIC,
            "login",
            _timer(),
            request_body={
                "username": "me@example.test",
                "password": "hunter2",
                "nested": [{"api_key": "sk-or-secret"}],
            },
            response_body={"auth_token": "tok", "ok": True},
        )
        [row] = await _rows()
        assert json.loads(row["request_body"]) == {
            "username": "me@example.test",
            "password": api_log.REDACTED,
            "nested": [{"api_key": api_log.REDACTED}],
        }
        assert json.loads(row["response_body"]) == {
            "auth_token": api_log.REDACTED,
            "ok": True,
        }

    async def test_bodies_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(api_log, "BODIES_ENABLED", False)
        await api_log.record(
            api_log.PROVIDER_PICNIC,
            "op",
            _timer(),
            request_body={"a": 1},
            response_body={"b": 2},
        )
        [row] = await _rows()
        assert row["request_body"] is None
        assert row["response_body"] is None

    async def test_bodies_are_not_truncated(self):
        value = "x" * 5000
        await api_log.record(
            api_log.PROVIDER_PICNIC,
            "op",
            _timer(),
            request_body={"a": value},
            response_body={"b": value},
        )
        [row] = await _rows()
        assert json.loads(row["request_body"])["a"] == value
        assert json.loads(row["response_body"])["b"] == value

    async def test_json_string_bodies_are_redacted(self):
        await api_log.record(
            api_log.PROVIDER_PICNIC,
            "login",
            _timer(),
            request_body='{"key": "me@example.test", "secret": "abc", "otp": "123"}',
        )
        [row] = await _rows()
        assert json.loads(row["request_body"]) == {
            "key": "me@example.test",
            "secret": api_log.REDACTED,
            "otp": api_log.REDACTED,
        }

    async def test_error_row_records_type_and_http_status(self):
        await api_log.record(
            api_log.PROVIDER_OPENROUTER,
            "chat.completions.create",
            _timer(),
            target="openai/gpt-5",
            error=_ApiError("rate limited", status_code=429),
            input_tokens=10,
            output_tokens=20,
        )
        [row] = await _rows()
        assert row["status"] == "error"
        assert row["http_status"] == 429
        assert row["error_type"] == "_ApiError"
        assert row["error_message"] == "rate limited"
        assert (row["input_tokens"], row["output_tokens"]) == (10, 20)

    async def test_http_status_can_come_from_the_response(self):
        class _Response:
            status_code = 503

        error = _ApiError("unavailable")
        setattr(error, "response", _Response())
        await api_log.record(api_log.PROVIDER_OPENROUTER, "op", _timer(), error=error)
        [row] = await _rows()
        assert row["http_status"] == 503

    async def test_long_values_are_truncated(self):
        await api_log.record(
            api_log.PROVIDER_OPENROUTER,
            "op" * 200,
            _timer(),
            target="t" * 900,
            error=_ApiError("e" * 900),
        )
        [row] = await _rows()
        assert len(row["operation"]) == api_log._MAX_OPERATION
        assert len(row["target"]) == api_log._MAX_TARGET
        assert len(row["error_message"]) == api_log._MAX_ERROR

    async def test_an_empty_exception_message_is_stored_as_null(self):
        await api_log.record(api_log.PROVIDER_PICNIC, "op", _timer(), error=_ApiError(""))
        [row] = await _rows()
        assert row["error_message"] is None

    async def test_error_response_body_is_taken_from_the_exception(self):
        class _HttpError(Exception):
            status_code = 500

            def __init__(self, message: str):
                super().__init__(message)
                self.body = {"detail": "upstream failed"}

        error = _HttpError("boom")
        await api_log.record(
            api_log.PROVIDER_PICNIC,
            "op",
            _timer(),
            error=error,
            response_body=api_log.error_response_body(error),
        )
        [row] = await _rows()
        assert json.loads(row["response_body"]) == {"detail": "upstream failed"}

    async def test_a_database_failure_never_raises(self, monkeypatch, caplog):
        def broken():
            raise RuntimeError("db down")

        monkeypatch.setattr(api_log, "get_log_db", broken)
        with caplog.at_level(logging.WARNING):
            await api_log.record(api_log.PROVIDER_PICNIC, "get_deliveries", _timer())
        assert "Could not record API request" in caplog.text


class TestTimer:
    def test_duration_is_measured_from_the_start(self):
        timer = api_log.Timer(
            started_perf=time.perf_counter() - 0.05,
            requested_at="2026-01-01T00:00:00+00:00",
        )
        assert timer.duration_ms >= 40

    def test_start_stamps_an_utc_iso_time(self):
        timer = api_log.Timer.start()
        parsed = datetime.fromisoformat(timer.requested_at)
        assert parsed.tzinfo is not None
        assert abs((datetime.now(UTC) - parsed).total_seconds()) < 5


class TestRequestContext:
    async def test_source_and_run_id_are_attached(self):
        async with api_log.request_context("import_history", run_id="run-1") as run_id:
            assert run_id == "run-1"
            await api_log.record(api_log.PROVIDER_PICNIC, "get_deliveries", _timer())

        [row] = await _rows()
        assert (row["source"], row["run_id"]) == ("import_history", "run-1")

    async def test_generated_run_id_is_unique(self):
        async with api_log.request_context("a") as first:
            await api_log.record(api_log.PROVIDER_PICNIC, "op", _timer())
        async with api_log.request_context("a") as second:
            await api_log.record(api_log.PROVIDER_PICNIC, "op", _timer())

        assert first != second
        assert len(first) == 32

    async def test_contexts_nest_and_the_outer_one_resumes(self):
        async with api_log.request_context("outer", run_id="outer-run"):
            async with api_log.request_context("inner") as inner_run:
                await api_log.record(api_log.PROVIDER_PICNIC, "inner-op", _timer())
            await api_log.record(api_log.PROVIDER_PICNIC, "outer-op", _timer())

        rows = await _rows()
        assert [(r["source"], r["run_id"]) for r in rows] == [
            ("outer", "outer-run"),
            ("inner", inner_run),
        ]

    async def test_no_context_means_null_source(self):
        await api_log.record(api_log.PROVIDER_PICNIC, "op", _timer())
        [row] = await _rows()
        assert (row["source"], row["run_id"]) == (None, None)


class TestCaptureHttpRequests:
    async def test_fallback_success_is_logged(self):
        async with api_log.capture_http_requests(
            api_log.PROVIDER_PICNIC,
            "get_cart",
            request_body={"args": []},
        ) as session:
            session.response_body = {"items": [{"id": "p1"}]}

        [row] = await _rows()
        assert row["status"] == "success"
        assert row["http_method"] is None
        assert row["url"] is None
        assert json.loads(row["request_body"]) == {"args": []}
        assert json.loads(row["response_body"]) == {"items": [{"id": "p1"}]}

    async def test_fallback_error_is_logged_and_reraised(self):
        with pytest.raises(_ApiError, match="boom"):
            async with api_log.capture_http_requests(
                api_log.PROVIDER_PICNIC, "get_delivery", target="d1"
            ):
                raise _ApiError("boom")

        [row] = await _rows()
        assert row["status"] == "error"
        assert row["operation"] == "get_delivery"
        assert row["target"] == "d1"
        assert row["error_type"] == "_ApiError"

    async def test_captured_requests_become_rows(self):
        async with api_log.capture_http_requests(
            api_log.PROVIDER_PICNIC, "get_delivery", target="d1"
        ) as session:
            session.captures.append(
                api_log.HttpCapture(
                    method="GET",
                    url="https://storefront.example/api/15/delivery/d1",
                    request_headers={"x-picnic-auth": "old-token"},
                    response_headers={"x-picnic-auth": "new-token"},
                    response_body={"id": "d1"},
                    status_code=200,
                )
            )

        [row] = await _rows()
        assert row["http_method"] == "GET"
        assert row["url"] == "https://storefront.example/api/15/delivery/d1"
        assert json.loads(row["request_headers"]) == {
            "x-picnic-auth": api_log.REDACTED
        }
        assert json.loads(row["response_headers"]) == {
            "x-picnic-auth": api_log.REDACTED
        }
        assert json.loads(row["response_body"]) == {"id": "d1"}


class _Prepared:
    method = "POST"
    url = "https://storefront.example/api/15/user/login?x=1"
    headers = {
        "x-picnic-auth": "old-token",
        "User-Agent": "okhttp/4.9.0",
        "Content-Type": "application/json",
    }
    body = '{"key": "me@example.test", "secret": "md5", "client_id": 30100}'


class _Response:
    status_code = 200
    headers = {
        "content-type": "application/json",
        "x-picnic-auth": "new-token",
    }
    text = '{"user_id": "u1", "token": "body-token"}'
    request = _Prepared()


class _Session:
    headers = {"User-Agent": "okhttp/4.9.0", "x-picnic-auth": "old-token"}

    def request(self, method, url, **kwargs):
        return _Response()


class _Api:
    session = _Session()


class TestPicnicCapture:
    async def test_captures_prepared_request_and_response(self):
        api = _Api()
        assert api_log.install_picnic_capture(api) is True
        assert api_log.install_picnic_capture(api) is True  # idempotent

        async with api_log.capture_http_requests(
            api_log.PROVIDER_PICNIC, "login"
        ):
            await asyncio.to_thread(
                api.session.request,
                "POST",
                "https://storefront.example/api/15/user/login",
                json={"key": "me@example.test", "secret": "md5"},
            )

        [row] = await _rows()
        assert row["http_method"] == "POST"
        assert row["url"] == "https://storefront.example/api/15/user/login?x=1"
        assert row["status"] == "success"
        assert json.loads(row["request_headers"]) == {
            "x-picnic-auth": api_log.REDACTED,
            "User-Agent": "okhttp/4.9.0",
            "Content-Type": "application/json",
        }
        assert json.loads(row["request_body"]) == {
            "key": "me@example.test",
            "secret": api_log.REDACTED,
            "client_id": 30100,
        }
        assert json.loads(row["response_headers"]) == {
            "content-type": "application/json",
            "x-picnic-auth": api_log.REDACTED,
        }
        assert json.loads(row["response_body"]) == {
            "user_id": "u1",
            "token": api_log.REDACTED,
        }

    async def test_works_through_a_real_requests_session(self):
        class _Adapter(BaseAdapter):
            def send(self, request, **kwargs):
                response = requests.Response()
                response.status_code = 200
                response._content = b'{"ok": true}'
                response.headers["Content-Type"] = "application/json"
                response.headers["x-picnic-auth"] = "new-token"
                response.request = request
                response.url = request.url
                return response

            def close(self):
                pass

        session = requests.Session()
        session.headers.update(
            {"x-picnic-auth": "old-token", "Content-Type": "application/json"}
        )
        session.mount("https://", _Adapter())
        api = type("Api", (), {"session": session})()
        api_log.install_picnic_capture(api)

        async with api_log.capture_http_requests(
            api_log.PROVIDER_PICNIC, "login"
        ):
            await asyncio.to_thread(
                session.post,
                "https://storefront.example/api/15/user/login?x=1",
                json={"key": "me@example.test", "secret": "md5"},
            )

        [row] = await _rows()
        assert row["http_method"] == "POST"
        assert row["url"] == "https://storefront.example/api/15/user/login?x=1"
        assert json.loads(row["request_headers"])["x-picnic-auth"] == api_log.REDACTED
        assert json.loads(row["request_body"]) == {
            "key": "me@example.test",
            "secret": api_log.REDACTED,
        }
        assert json.loads(row["response_body"]) == {"ok": True}
        assert json.loads(row["response_headers"])["x-picnic-auth"] == api_log.REDACTED

    async def test_transport_failure_is_captured(self):
        class _FailingSession:
            headers = {}

            def request(self, method, url, **kwargs):
                raise ConnectionError("down")

        class _FailingApi:
            session = _FailingSession()

        api = _FailingApi()
        api_log.install_picnic_capture(api)

        with pytest.raises(ConnectionError, match="down"):
            async with api_log.capture_http_requests(
                api_log.PROVIDER_PICNIC, "get_cart"
            ):
                await asyncio.to_thread(
                    api.session.request, "GET", "https://x.test/cart"
                )

        [row] = await _rows()
        assert row["status"] == "error"
        assert row["error_type"] == "ConnectionError"
        assert row["url"] == "https://x.test/cart"


class TestBodyHelpers:
    def test_error_response_body_none(self):
        assert api_log.error_response_body(None) is None

    def test_error_response_body_prefers_the_exception_body(self):
        class _Error(Exception):
            body = {"detail": "nope"}

        assert api_log.error_response_body(_Error()) == {"detail": "nope"}

    def test_error_response_body_reads_json_from_the_response(self):
        class _Response:
            def json(self):
                return {"detail": "nope"}

        error = RuntimeError("boom")
        setattr(error, "response", _Response())
        assert api_log.error_response_body(error) == {"detail": "nope"}

    def test_error_response_body_falls_back_to_text(self):
        class _Response:
            text = "<html>oops</html>"

            def json(self):
                raise ValueError("not json")

        error = RuntimeError("boom")
        setattr(error, "response", _Response())
        assert api_log.error_response_body(error) == "<html>oops</html>"

    def test_error_response_body_without_a_response(self):
        assert api_log.error_response_body(RuntimeError("boom")) is None

    def test_jsonable_reads_raw_payloads_and_reprs_unknown_objects(self):
        class _WithRaw:
            raw = {"a": 1}

        class _Unknown:
            def __repr__(self):
                return "<unknown>"

        assert api_log._jsonable(_WithRaw()) == {"a": 1}
        assert api_log._jsonable(_Unknown()) == "<unknown>"

    def test_jsonable_falls_back_to_modelless_model_dump(self):
        class _V1Model:
            def model_dump(self, mode=None):
                if mode is not None:
                    raise TypeError("no mode")
                return {"a": 1}

        assert api_log._jsonable(_V1Model()) == {"a": 1}

    async def test_a_raw_string_body_is_kept_verbatim(self):
        await api_log.record(
            api_log.PROVIDER_PICNIC, "op", _timer(), request_body="not json"
        )
        [row] = await _rows()
        assert row["request_body"] == "not json"

    def test_an_unencodable_body_falls_back_to_repr(self, monkeypatch):
        def _raise(*_args, **_kwargs):
            raise TypeError("cannot encode")

        monkeypatch.setattr(api_log.json, "dumps", _raise)
        assert api_log._body_text({"a": 1}) == repr({"a": 1})


class TestQueryHelpers:
    async def test_list_is_newest_first_and_filters(self):
        await api_log.record(api_log.PROVIDER_PICNIC, "picnic-op", _timer())
        await api_log.record(api_log.PROVIDER_OPENROUTER, "openrouter-op", _timer())

        assert [r["operation"] for r in await _rows()] == [
            "openrouter-op",
            "picnic-op",
        ]
        assert [r["operation"] for r in await _rows(provider="picnic")] == ["picnic-op"]
        assert [r["operation"] for r in await _rows(status="success")] == [
            "openrouter-op",
            "picnic-op",
        ]

    async def test_prune_removes_only_old_rows(self):
        async with db.get_db() as session:
            for requested_at in (
                "2020-01-01T00:00:00+00:00",
                datetime.now(UTC).isoformat(),
            ):
                await db.insert_api_request(
                    session,
                    provider="picnic",
                    operation="op",
                    status="success",
                    duration_ms=1,
                    requested_at=requested_at,
                )
            await session.commit()

            assert await db.prune_api_requests(session, keep_days=30) == 1
            await session.commit()

        [row] = await _rows()
        assert row["requested_at"] != "2020-01-01T00:00:00+00:00"

    async def test_prune_rejects_a_negative_window(self):
        async with db.get_db() as session:
            with pytest.raises(ValueError):
                await db.prune_api_requests(session, keep_days=-1)


class TestLockedDatabase:
    """A lock held by another connection must not stall the logged request."""

    async def test_a_held_lock_costs_a_row_not_a_stall(self, db_path, caplog):
        import sqlite3
        import time as _time

        blocker = sqlite3.connect(db_path)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            started = _time.perf_counter()
            with caplog.at_level(logging.WARNING):
                await api_log.record(api_log.PROVIDER_PICNIC, "get_cart", _timer())
            elapsed = _time.perf_counter() - started
        finally:
            blocker.rollback()
            blocker.close()

        assert elapsed < 2, f"logging waited {elapsed:.1f}s on a held lock"
        assert "Could not record API request" in caplog.text
        assert await _rows() == []

    async def test_the_short_timeout_is_confined_to_the_log_engine(self):
        from picnic_meal_planner.db import engine as engine_mod

        # History and order writes keep sqlite3's default wait.
        assert engine_mod.get_log_engine() is not engine_mod.get_engine()
        assert "timeout" not in engine_mod.get_engine().dialect.create_connect_args(
            engine_mod.get_engine().url
        )[1]


class TestConsent:
    async def test_without_consent_the_query_string_is_dropped(self):
        async with api_log.request_context("bot", chat_id=3, store_bodies=False):
            await api_log.record(
                api_log.PROVIDER_PICNIC,
                "search",
                _timer(),
                http_method="GET",
                url="https://storefront/api/15/pages/search?search_term=pregnancy+test",
                request_body={"q": "pregnancy test"},
                response_body={"items": []},
            )
        [row] = await _rows()
        assert row["url"] == "https://storefront/api/15/pages/search"
        assert row["request_body"] is None and row["response_body"] is None
        assert row["chat_id"] == 3

    async def test_picnic_bodies_outside_a_context_are_kept(self):
        await api_log.record(
            api_log.PROVIDER_PICNIC, "get_cart", _timer(), response_body={"items": []}
        )
        [row] = await _rows()
        assert row["response_body"] is not None and row["chat_id"] is None


class TestRetention:
    async def _insert(self, requested_at: str) -> None:
        async with db.get_db() as session:
            await db.insert_api_request(
                session,
                provider="picnic",
                operation="op",
                status="success",
                duration_ms=1,
                requested_at=requested_at,
            )
            await session.commit()

    @pytest.mark.parametrize(
        ("value", "days"),
        [(None, 30), ("", 30), ("7", 7), ("0", 0), ("-3", 30), ("week", 30)],
    )
    def test_retention_days(self, monkeypatch, value, days):
        if value is None:
            monkeypatch.delenv("API_LOG_RETENTION_DAYS", raising=False)
        else:
            monkeypatch.setenv("API_LOG_RETENTION_DAYS", value)
        assert api_log.retention_days() == days

    async def test_prune_expired_uses_the_configured_window(self, monkeypatch):
        monkeypatch.setenv("API_LOG_RETENTION_DAYS", "30")
        await self._insert("2020-01-01T00:00:00+00:00")
        await self._insert(datetime.now(UTC).isoformat())
        assert await api_log.prune_expired() == 1
        assert len(await _rows()) == 1

    async def test_zero_keeps_everything(self, monkeypatch):
        monkeypatch.setenv("API_LOG_RETENTION_DAYS", "0")
        await self._insert("2020-01-01T00:00:00+00:00")
        assert await api_log.prune_expired() is None
        assert len(await _rows()) == 1

    async def test_a_failed_prune_never_raises(self, monkeypatch, caplog):
        def broken():
            raise RuntimeError("db down")

        monkeypatch.setattr(api_log, "get_log_db", broken)
        with caplog.at_level(logging.WARNING):
            assert await api_log.prune_expired() is None
        assert "Could not prune" in caplog.text
