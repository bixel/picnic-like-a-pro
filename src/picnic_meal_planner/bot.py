"""Telegram bot entry point.

Each family member has their own per-chat conversation history, persisted
across restarts. All MCP tools are available in every conversation.

Model requests go through OpenRouter (``llm.py``), and each chat can run on its
own model — see ``llm.get_chat_model``.

History policy — truncation, serialization, storage, opt-out — lives in
``history.py``; this module only reads a context and commits a completed turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import AsyncIterator

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# Imported after load_dotenv(): both modules read configuration at import time.
from . import api_log, history, llm  # noqa: E402
from .db.engine import (  # noqa: E402
    SCHEMA_REVISION,
    init_db,
    missing_columns,
    pending_migration,
)
from .picnic import auth as picnic_auth  # noqa: E402
from .picnic import client as picnic  # noqa: E402

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a friendly and helpful family grocery and meal planning assistant.
You help the family manage their Picnic grocery orders, plan meals, track order history,
and forecast what they might need to reorder soon.

You have access to tools that let you:
- Search for products and browse categories on Picnic
- View and modify the shared family cart
- Check available delivery slots
- View order history and product statistics
- Forecast what the family is likely running low on
- Record completed orders to improve future forecasts

The Picnic account is shared across all family members. Be conversational and helpful.
When suggesting items to add to the cart, always confirm with the user before adding them.

Formatting: your replies are sent to Telegram as plain text, so no markup is rendered.
- Never use Markdown or HTML: no tables, no | pipes or --- rules, no # headings,
  no **bold**, _italics_ or `code`. They show up as literal symbols.
- Telegram has no tables. For tabular data (products, prices, orders, slots),
  write one item per line, e.g. "• Halfvolle melk 1L — 2x — €1.29".
- Use short paragraphs, blank lines between sections, "•" or "1." for lists,
  and emoji sparingly as section markers (e.g. "🛒 Cart").
- Keep replies concise; a phone screen is narrow.
"""

_mcp_tools: list[dict] | None = None


def _allowed_user_ids() -> set[int]:
    raw = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").strip()
    return {int(uid.strip()) for uid in raw.split(",") if uid.strip()}


def _is_allowed(user_id: int) -> bool:
    # Explicit opt-in required to allow all users (e.g. for local dev)
    if os.getenv("ALLOW_ALL_USERS", "").lower() == "true":
        return True
    return user_id in _allowed_user_ids()


def _may_log_in_to_picnic(user_id: int) -> bool:
    """Who may start a Picnic login — each start texts the account owner.

    ``PICNIC_LOGIN_USER_IDS`` narrows it to a subset of the allowed users;
    unset, every allowed user may.
    """
    if not _is_allowed(user_id):
        return False
    raw = os.getenv("PICNIC_LOGIN_USER_IDS", "").strip()
    if not raw:
        return True
    return user_id in {int(uid.strip()) for uid in raw.split(",") if uid.strip()}


async def _get_mcp_tools() -> list[dict]:
    """Return the MCP tool schemas (Anthropic shape; llm.py translates them)."""
    global _mcp_tools
    if _mcp_tools is not None:
        return _mcp_tools
    from .mcp_server import get_tool_schemas  # noqa: PLC0415
    _mcp_tools = get_tool_schemas()
    return _mcp_tools


async def _call_mcp_tool(name: str, input_data: dict):
    """Dispatch a tool call to the MCP server in-process."""
    from .mcp_server import call_tool  # noqa: PLC0415
    return await call_tool(name, input_data)


async def _run_llm(chat_id: int, user_text: str) -> str:
    """Send a message to the chat's model, handle tool use, return the reply."""
    # Every API request of this turn (OpenRouter and the Picnic tool calls) is
    # logged under this chat, so deleting its history deletes them too, and
    # with its bodies only if the chat consents to history being stored.
    # is_persistence_enabled fails closed, and so does the log.
    async with api_log.request_context(
        "bot",
        chat_id=chat_id,
        store_bodies=await history.is_persistence_enabled(chat_id),
    ):
        return await _run_turn(chat_id, user_text)


async def _run_turn(chat_id: int, user_text: str) -> str:
    """One turn: the model loop with tool calls, then commit to history."""
    tools = await _get_mcp_tools()
    # Resolved once per turn, so a model switch never lands mid-tool-loop.
    model = await llm.get_chat_model(chat_id)

    # Work on a copy; only commit to canonical history on success, so a failed
    # API call leaves no half-written turn behind. The shapes that 400 are a
    # non-user first message, an orphaned tool_result, and a dangling tool_use;
    # see the guards before commit_turn.)
    #
    # Trim BEFORE appending the new turn, and only at a safe boundary: trimming
    # afterwards could slice away the message the user just sent, and a blind
    # slice can leave an orphaned tool_result at the front, which the API
    # rejects. Everything from new_from onwards is what this turn produced.
    base = await history.get_context(chat_id)
    messages = history.trim_history(base, history.MAX_HISTORY_MESSAGES - 1)
    new_from = len(messages)
    messages.append({"role": "user", "content": user_text})

    while True:
        # llm.create_message speaks the same Anthropic-shaped blocks that
        # history stores, translating to and from OpenRouter internally.
        response = await llm.create_message(
            model=model,
            system=SYSTEM_PROMPT,
            tools=tools or [],
            messages=messages,
        )

        # Collect any text content from this response turn
        assistant_text = ""
        tool_uses = []
        for block in response.content:
            if block["type"] == "text":
                assistant_text += block["text"]
            elif block["type"] == "tool_use":
                tool_uses.append(block)

        # llm returns plain, JSON-safe dicts: the one representation used for
        # both the next request and the stored row.
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "tool_use" and tool_uses:
            # Execute all requested tool calls
            tool_results = []
            for tu in tool_uses:
                result = await _call_mcp_tool(tu["name"], tu["input"])
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": str(result),
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            continue

        # The turn ended without the tool_use branch running. Two shapes are
        # unsafe to archive, and both permanently break the chat if stored:
        #
        #  - a tool_use that will never be answered. Generation cut off mid
        #    block (stop_reason "max_tokens") leaves tool_uses set while
        #    stop_reason is not "tool_use", so control lands here.
        #  - empty content, which the API rejects on the next request.
        #
        # Neither is repairable later: trim_history only fixes the head, and
        # commit_turn runs only on success, so no subsequent turn can advance
        # past a poisoned one. Discard rather than persist.
        if tool_uses:
            logger.warning(
                "Discarding turn for chat %d: stop_reason=%r left a tool_use "
                "unanswered", chat_id, response.stop_reason,
            )
            return (
                "Sorry, I ran out of room mid-thought and didn't finish that. "
                "Could you ask again, ideally a bit more specifically?"
            )

        if not response.content:
            logger.warning(
                "Discarding empty response for chat %d (stop_reason=%r)",
                chat_id, response.stop_reason,
            )
            return "(no reply)"

        # Final response — commit the turn (memory + archive) and return text
        await history.commit_turn(chat_id, messages, new_from)
        return assistant_text or "(no reply)"


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def _reject_unauthorized(update: Update) -> None:
    await update.message.reply_text("Sorry, you're not authorised to use this bot.")


# The canned requests behind /plan, /order, … and the keyboard buttons.
PROMPTS = {
    "plan": "Let's plan meals for the week. What does the family feel like eating?",
    "order": (
        "I'd like to do some grocery shopping. Can you help me search for "
        "products and manage the cart?"
    ),
    "forecast": "What groceries are we likely running low on based on our order history?",
    "history": "Show me a summary of our recent grocery orders.",
    "cart": "Show me what's currently in the Picnic cart.",
}

# The keyboard under the message field.  A tap sends the label as an ordinary
# message, which ``on_text`` maps back to an action — so a label must never
# reach the model, and changing one orphans keyboards already on screen until
# the next /start or /menu.
SETTINGS_BUTTON = "⚙️ Settings"
MENU_BUTTONS = {
    "🍽 Plan meals": "plan",
    "🛒 Shop": "order",
    "📉 Running low?": "forecast",
    "🧾 Recent orders": "history",
    "🧺 Cart": "cart",
}
MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["🍽 Plan meals", "🛒 Shop"],
        ["📉 Running low?", "🧾 Recent orders"],
        ["🧺 Cart", SETTINGS_BUTTON],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Telegram's own "/" menu, for those who prefer commands.
BOT_COMMANDS = [
    BotCommand("menu", "Show the buttons"),
    BotCommand("plan", "Plan meals for the week"),
    BotCommand("order", "Shop: search products, manage the cart"),
    BotCommand("forecast", "What are we running low on?"),
    BotCommand("history", "Recent orders"),
    BotCommand("cart", "Current Picnic cart"),
    BotCommand("settings", "Privacy, stored conversation, Picnic login"),
]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/start`` and ``/menu`` — greet and (re)show the button keyboard."""
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    await update.message.reply_text(
        "Hi! I'm your family grocery and meal planning assistant.\n\n"
        "Use the buttons below, or just chat with me naturally. "
        f"{SETTINGS_BUTTON} has privacy, the stored conversation and the "
        "Picnic login.",
        reply_markup=MENU_KEYBOARD,
    )


async def _prompt_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, name: str
) -> None:
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    await _chat(update, context, PROMPTS[name])


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _prompt_command(update, context, "plan")


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _prompt_command(update, context, "order")


async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _prompt_command(update, context, "forecast")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _prompt_command(update, context, "history")


async def cmd_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _prompt_command(update, context, "cart")


# The actions below return the text to show, so a command can send it as a
# reply and a Settings button can put it in the menu message.

async def _forget(chat_id: int) -> str:
    """Delete this chat's stored conversation; the outcome as a message."""
    # history.clear_history deliberately propagates DB failures rather than
    # failing silently, so never report success without catching them first.
    try:
        removed = await history.clear_history(chat_id)
    except Exception:  # noqa: BLE001
        logger.exception("Could not clear history for chat %d", chat_id)
        return "Sorry, I couldn't clear the history just now. Please try again."
    if removed:
        return f"Forgotten — deleted {removed} stored message(s). We're starting fresh."
    return "There was nothing stored. We're starting fresh."


async def _set_privacy(chat_id: int, enable: bool) -> str:
    try:
        await history.set_persistence(chat_id, enable)
    except Exception:  # noqa: BLE001
        logger.exception("Could not change persistence for chat %d", chat_id)
        return "Sorry, I couldn't change that setting just now. Please try again."
    if enable:
        return "Saving is ON. I'll remember our conversations between restarts."
    return (
        "Saving is OFF, and anything already stored has been deleted. "
        "I'll still remember this conversation until the bot restarts."
    )


async def _model_line(chat_id: int) -> str:
    model = await llm.get_chat_model(chat_id)
    suffix = "" if model != llm.DEFAULT_MODEL else " (the default)"
    return f"This chat uses {model}{suffix}."


async def _picnic_login(user_id: int, channel: str) -> str:
    """Start a Picnic login for *user_id*; the outcome as a message."""
    if os.getenv("PICNIC_MOCK", "").lower() == "true":
        return "Picnic is mocked here; there is nothing to log in to."

    from python_picnic_api2 import PicnicAuthError  # noqa: PLC0415

    try:
        logged_in = await picnic.start_login(channel, owner=user_id)
    except ValueError:
        return "Usage: /picnic_login [sms|email]"
    except picnic.PicnicLoginRequired as exc:  # no credentials, or another login pending
        return str(exc)
    except PicnicAuthError:
        return "Picnic rejected the username or password in the bot's configuration."
    except Exception:  # noqa: BLE001
        logger.exception("Picnic login failed")
        return "Sorry, I couldn't reach Picnic. Please try again."

    if logged_in:
        return "Logged in to Picnic — no code was needed."
    minutes = picnic_auth.CODE_TTL_SECONDS // 60
    return (
        f"Picnic sent a login code by {channel.lower()}. Send it here as a "
        f"message within {minutes} minutes (just the digits, or "
        "/picnic_code 123456)."
    )


async def _picnic_code(code: str) -> str:
    """Finish the pending Picnic login with *code*; the outcome as a message."""
    from python_picnic_api2 import Picnic2FAError  # noqa: PLC0415

    try:
        await picnic.submit_login_code(code)
    except Picnic2FAError:
        return (
            "Picnic didn't accept that code. Check it and send it again, or "
            "start a new login (/picnic_login) for a new one."
        )
    except picnic.PicnicLoginRequired as exc:  # nothing pending, or it expired
        return str(exc)
    except Exception:  # noqa: BLE001
        logger.exception("Picnic 2FA verification failed")
        return "Sorry, I couldn't reach Picnic. Please try again."
    return "Logged in to Picnic. You're all set."


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete this chat's stored conversation and start fresh."""
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    await update.message.reply_text(await _forget(update.effective_chat.id))


async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/privacy`` shows the current setting; ``/privacy on|off`` changes it."""
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return

    chat_id = update.effective_chat.id
    arg = context.args[0].lower() if context.args else ""

    if arg not in ("on", "off"):
        enabled = await history.is_persistence_enabled(chat_id)
        await update.message.reply_text(
            f"Saving our conversation is currently {'ON' if enabled else 'OFF'}.\n\n"
            "/privacy off — stop saving, and delete what's already stored\n"
            "/privacy on — start saving again\n"
            "/forget — clear the history either way"
        )
        return

    await update.message.reply_text(await _set_privacy(chat_id, arg == "on"))


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/model`` shows the model this chat runs on.

    Read-only for now. Switching is a backend operation
    (``scripts/chat_model.py``) until users are given a picker; when they are,
    it should call ``llm.set_chat_model``, which already enforces
    ``LLM_ALLOWED_MODELS``.
    """
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    await update.message.reply_text(await _model_line(update.effective_chat.id))


async def cmd_picnic_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/picnic_login [sms|email]`` — log in, asking Picnic for a 2FA code if needed."""
    if not _may_log_in_to_picnic(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    channel = (context.args[0] if context.args else "sms").upper()
    await update.message.reply_text(
        await _picnic_login(update.effective_user.id, channel)
    )


async def cmd_picnic_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/picnic_code <code>`` — finish the login ``/picnic_login`` started."""
    if not _may_log_in_to_picnic(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: /picnic_code 123456")
        return
    await update.message.reply_text(await _picnic_code(context.args[0]))


# ---------------------------------------------------------------------------
# Settings menu (inline buttons)
# ---------------------------------------------------------------------------
#
# One message whose text and buttons are edited in place.  Whatever deletes
# data asks for confirmation first: a button is easier to hit by accident
# than a typed command.

def _buttons(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row]
         for row in rows]
    )


_BACK = ("« Back", "menu")


async def _settings_view(chat_id: int, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    saving = await history.is_persistence_enabled(chat_id)
    rows = [
        [("🗑 Forget our conversation", "forget")],
        [("🚫 Stop saving conversations", "privacy:off")]
        if saving
        else [("💾 Save conversations", "privacy:on")],
    ]
    if _may_log_in_to_picnic(user_id):
        rows.append([("🔑 Log in to Picnic", "login")])
    text = (
        f"{SETTINGS_BUTTON}\n\n"
        f"Saving our conversation: {'ON' if saving else 'OFF'}\n"
        f"{await _model_line(chat_id)}"
    )
    return text, _buttons(*rows)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/settings`` or the Settings button — open the settings menu."""
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    text, markup = await _settings_view(
        update.effective_chat.id, update.effective_user.id
    )
    await update.message.reply_text(text, reply_markup=markup)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap on an inline button of the settings menu."""
    query = update.callback_query
    user_id = update.effective_user.id
    # Checked on every tap: in a group anyone can press a button under a
    # message that was sent for somebody else.
    if not _is_allowed(user_id):
        await query.answer(
            "Sorry, you're not authorised to use this bot.", show_alert=True
        )
        return
    await query.answer()

    chat_id = update.effective_chat.id
    data = query.data or ""
    markup = _buttons([_BACK])

    if data == "menu":
        text, markup = await _settings_view(chat_id, user_id)
    elif data == "forget":
        text = "Delete everything stored from this chat's conversation?"
        markup = _buttons([("🗑 Yes, delete", "forget:yes"), ("Cancel", "menu")])
    elif data == "forget:yes":
        text = await _forget(chat_id)
    elif data == "privacy:off":
        text = "Stop saving this chat's conversation and delete what is already stored?"
        markup = _buttons([("🚫 Yes, stop and delete", "privacy:off:yes"), ("Cancel", "menu")])
    elif data == "privacy:off:yes":
        text = await _set_privacy(chat_id, False)
    elif data == "privacy:on":
        text = await _set_privacy(chat_id, True)
    elif data.startswith("login"):
        if not _may_log_in_to_picnic(user_id):
            text = "Sorry, you're not allowed to log in to Picnic from here."
        elif data == "login":
            text = "Log in to Picnic. If it asks for a code, how should it be sent?"
            markup = _buttons(
                [("📱 SMS", "login:sms"), ("✉️ Email", "login:email")], [_BACK]
            )
        else:
            text = await _picnic_login(user_id, data.removeprefix("login:").upper())
    else:
        logger.warning("Unknown button %r in chat %d", data, chat_id)
        return

    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest:
        # The message is gone or too old to edit, or already shows this.
        logger.debug("Could not edit the settings message", exc_info=True)
        await context.bot.send_message(chat_id, text, reply_markup=markup)


# ---------------------------------------------------------------------------
# Free text
# ---------------------------------------------------------------------------

_LOGIN_CODE = re.compile(r"\d{4,8}")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any non-command message: a keyboard button, a login code, or chat."""
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if text == SETTINGS_BUTTON:
        await cmd_settings(update, context)
    elif text in MENU_BUTTONS:
        await _prompt_command(update, context, MENU_BUTTONS[text])
    elif (
        _LOGIN_CODE.fullmatch(text)
        and picnic.pending_login_owner() == user_id
        and _may_log_in_to_picnic(user_id)
    ):
        # The code this user's pending login waits for.  Handled here so that
        # it is never sent to the model or stored in the history.
        await update.message.reply_text(await _picnic_code(text))
    else:
        await _chat(update, context)


async def _chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str | None = None,
) -> None:
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        await update.message.reply_text("Sorry, you're not authorised to use this bot.")
        return

    chat_id = update.effective_chat.id
    message_text = text or update.message.text or ""

    try:
        async with _keep_typing(context.bot, chat_id):
            reply = await _run_llm(chat_id, message_text)
    except Exception:  # noqa: BLE001
        logger.exception("Error running the LLM for chat %d", chat_id)
        reply = "Sorry, something went wrong. Please try again."

    # Telegram messages have a 4096 character limit
    for chunk in _split_message(reply):
        await update.message.reply_text(chunk)


# Telegram shows a chat action for about 5 seconds, or until the bot sends a
# message, so it has to be re-sent for as long as the reply is being worked on
# (LLM requests and tool calls alike).
TYPING_REFRESH_SECONDS = 4.0


async def _send_typing(bot, chat_id: int) -> None:
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:  # noqa: BLE001 — cosmetic; never break the reply
        logger.debug("Could not send typing action to chat %d", chat_id, exc_info=True)


async def _refresh_typing(bot, chat_id: int) -> None:
    while True:
        await asyncio.sleep(TYPING_REFRESH_SECONDS)
        await _send_typing(bot, chat_id)


@contextlib.asynccontextmanager
async def _keep_typing(bot, chat_id: int) -> AsyncIterator[None]:
    """Show "typing…" in *chat_id* until the block exits."""
    await _send_typing(bot, chat_id)
    task = asyncio.create_task(_refresh_typing(bot, chat_id))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _split_message(text: str, max_len: int = 4096) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class SchemaOutOfDateError(RuntimeError):
    """The database lacks columns the code needs; a migration was skipped."""


async def _post_init(app: Application) -> None:
    """Ensure the database is usable before the bot starts handling messages.

    Raises :class:`SchemaOutOfDateError` — stopping the bot — when a migration
    was skipped: Alembic's recorded revision is behind the code, or columns
    are missing. ``init_db()`` creates missing tables but cannot add columns
    to existing ones, and running on such a schema is worse than not running:
    every ``chat_settings`` read fails, persistence fails closed, and history
    silently stops being saved while the bot looks healthy.
    """
    stored = await pending_migration()
    if stored is not None:
        # Checked before init_db(), which would create the new tables outside
        # Alembic and make the pending migration fail on them.
        raise SchemaOutOfDateError(
            f"Database is at migration {stored}, the code needs "
            f"{SCHEMA_REVISION}. Run `alembic upgrade head` before starting the bot."
        )
    await init_db()
    missing = await missing_columns()
    if missing:
        raise SchemaOutOfDateError(
            f"Database schema is behind the code (missing {', '.join(missing)}). "
            "Run `alembic upgrade head` before starting the bot — see "
            "MIGRATION_OPENROUTER.md."
        )
    logger.info(
        "Database initialised. Conversation persistence: %s",
        "on"
        if history._persistence_enabled_globally()
        else "off (PERSIST_CONVERSATIONS)",
    )
    logger.info("Default LLM model (via OpenRouter): %s", llm.DEFAULT_MODEL)
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception:  # noqa: BLE001 — cosmetic; the commands work regardless
        logger.warning("Could not publish the command menu", exc_info=True)


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler(["start", "menu"], cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("forecast", cmd_forecast))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("cart", cmd_cart))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("picnic_login", cmd_picnic_login))
    app.add_handler(CommandHandler("picnic_code", cmd_picnic_code))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Starting bot with long polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
