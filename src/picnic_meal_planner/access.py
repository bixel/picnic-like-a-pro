"""Who may use the bot and the reporting UI: the Telegram user allowlist."""

from __future__ import annotations

import os


def allowed_user_ids() -> set[int]:
    raw = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").strip()
    return {int(uid.strip()) for uid in raw.split(",") if uid.strip()}


def is_allowed(user_id: int) -> bool:
    # Explicit opt-in required to allow all users (e.g. for local dev)
    if os.getenv("ALLOW_ALL_USERS", "").lower() == "true":
        return True
    return user_id in allowed_user_ids()
