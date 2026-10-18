"""End-to-end tests for scripts/chat_model.py, run as a real subprocess.

Synchronous on purpose, like test_migrations.py: Alembic's env.py calls
``asyncio.run()``, which cannot nest inside a running loop.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "chat_model.py"


def _run(db_file: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DB_PATH": str(db_file)}
    env.pop("LLM_ALLOWED_MODELS", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )


def _migrate(db_file: Path, monkeypatch, revision: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    monkeypatch.setenv("DB_PATH", str(db_file))
    command.upgrade(cfg, revision)


def test_refuses_a_database_missing_the_migration(tmp_path, monkeypatch):
    db_file = tmp_path / "at_0002.db"
    _migrate(db_file, monkeypatch, "0002")
    for args in (["list"], ["set", "1", "openai/gpt-5"]):
        result = _run(db_file, *args)
        assert result.returncode == 1
        assert "chat_settings.model" in result.stderr
        assert "alembic upgrade head" in result.stderr


def test_does_not_create_a_database_at_a_mistyped_path(tmp_path):
    db_file = tmp_path / "typo.db"
    result = _run(db_file, "list")
    assert result.returncode == 1
    assert "no database" in result.stderr
    assert not db_file.exists()


def test_set_list_reset_on_a_migrated_database(tmp_path, monkeypatch):
    db_file = tmp_path / "head.db"
    _migrate(db_file, monkeypatch, "head")

    assert _run(db_file, "set", "42", "openai/gpt-5").returncode == 0
    listed = _run(db_file, "list")
    assert listed.returncode == 0
    assert "42" in listed.stdout and "openai/gpt-5" in listed.stdout

    reset = _run(db_file, "reset", "42")
    assert reset.returncode == 0
    assert "(default)" in _run(db_file, "list").stdout
