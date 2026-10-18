"""Tests for scripts/preflight.py's LLM checks.

HTTP is served by ``httpx.MockTransport``, so nothing leaves the process.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

import picnic_meal_planner

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "preflight.py"
_spec = importlib.util.spec_from_file_location("preflight", _SCRIPT)
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

BASE = "https://openrouter.test/api/v1"


@pytest.fixture(autouse=True)
def fresh_results(monkeypatch):
    monkeypatch.setattr(preflight, "_results", [])


def _results(check: str) -> list[tuple[str, str]]:
    return [(status, detail) for status, name, detail in preflight._results if name == check]


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestLoadLlmConfig:
    def _reimport(self, monkeypatch):
        # llm parses its settings at import; force a fresh import under the
        # patched environment. monkeypatch restores the original afterwards.
        monkeypatch.delitem(sys.modules, "picnic_meal_planner.llm", raising=False)
        monkeypatch.delattr(picnic_meal_planner, "llm", raising=False)
        return preflight.load_llm_config()

    def test_non_integer_max_tokens_fails(self, monkeypatch):
        monkeypatch.setenv("LLM_MAX_TOKENS", "abc")
        assert self._reimport(monkeypatch) is None
        [(status, detail)] = _results("LLM_MAX_TOKENS")
        assert status == preflight.FAIL and "abc" in detail

    def test_non_positive_max_tokens_fails(self, monkeypatch):
        monkeypatch.setenv("LLM_MAX_TOKENS", "0")
        assert self._reimport(monkeypatch) is not None
        assert _results("LLM_MAX_TOKENS")[0][0] == preflight.FAIL

    def test_reports_the_bots_own_default_model(self, monkeypatch):
        monkeypatch.delenv("LLM_MODEL", raising=False)
        llm = self._reimport(monkeypatch)
        [(status, detail)] = _results("LLM_MODEL")
        assert status == preflight.OK and llm.DEFAULT_MODEL in detail


class TestCheckKey:
    @pytest.mark.parametrize(
        "code,expected",
        [(401, "invalid key"), (402, "no credits"), (403, "HTTP 403"), (500, "HTTP 500")],
    )
    async def test_any_error_status_fails(self, code, expected):
        async with _client(lambda req: httpx.Response(code)) as client:
            await preflight._check_key(client, BASE, "k")
        [(status, detail)] = _results("openrouter api")
        assert status == preflight.FAIL and expected in detail

    async def test_unreachable_base_url_fails(self):
        def handler(req):
            raise httpx.ConnectError("no route")

        async with _client(handler) as client:
            await preflight._check_key(client, BASE, "k")
        assert _results("openrouter api")[0][0] == preflight.FAIL

    async def test_valid_key_passes_and_shows_credits(self):
        body = {"data": {"limit_remaining": 7.5}}
        async with _client(lambda req: httpx.Response(200, json=body)) as client:
            await preflight._check_key(client, BASE, "k")
        [(status, detail)] = _results("openrouter api")
        assert status == preflight.OK and "7.5" in detail


class TestCheckModel:
    async def test_known_model_passes(self):
        body = {"data": [{"id": "openai/gpt-5"}]}
        async with _client(lambda req: httpx.Response(200, json=body)) as client:
            await preflight._check_model(client, BASE, "openai/gpt-5")
        assert _results("llm model")[0][0] == preflight.OK

    async def test_unknown_model_fails(self):
        body = {"data": [{"id": "openai/gpt-5"}]}
        async with _client(lambda req: httpx.Response(200, json=body)) as client:
            await preflight._check_model(client, BASE, "openai/gtp-5")
        assert _results("llm model")[0][0] == preflight.FAIL

    async def test_error_listing_models_fails(self):
        async with _client(lambda req: httpx.Response(503)) as client:
            await preflight._check_model(client, BASE, "openai/gpt-5")
        assert _results("llm model")[0][0] == preflight.FAIL


class TestCheckOpenrouter:
    async def test_model_is_checked_even_when_the_key_fails(self, monkeypatch):
        """Regression: a bad key used to skip the model check entirely."""
        from picnic_meal_planner import llm

        monkeypatch.setattr(llm, "OPENROUTER_BASE_URL", BASE)
        monkeypatch.setenv("OPENROUTER_API_KEY", "bad")

        def handler(req):
            if req.url.path.endswith("/key"):
                return httpx.Response(401)
            return httpx.Response(200, json={"data": [{"id": llm.DEFAULT_MODEL}]})

        real = httpx.AsyncClient
        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: real(transport=httpx.MockTransport(handler)),
        )
        await preflight.check_openrouter(llm)
        assert _results("openrouter api")[0][0] == preflight.FAIL
        assert _results("llm model")[0][0] == preflight.OK

    async def test_missing_key_is_reported_and_stale_anthropic_key_named(
        self, monkeypatch
    ):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-old")
        await preflight.check_openrouter(None)
        details = " ".join(d for _, d in _results("openrouter api"))
        assert "ANTHROPIC_API_KEY" in details
