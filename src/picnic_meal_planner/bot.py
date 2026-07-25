"""Telegram bot entry point.

Each family member has their own per-chat conversation history with Claude.
All MCP tools are available in every conversation.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict

import anthropic
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

from .db.engine import init_db  # noqa: E402

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
"""

MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "20"))

# Per-chat conversation history: chat_id → list of messages
_histories: dict[int, list[dict]] = defaultdict(list)

_anthropic_client: anthropic.AsyncAnthropic | None = None
_mcp_tools: list[dict] | None = None


def _get_anthropic_client() -> anthropic.AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"]
        )
    return _anthropic_client


def _allowed_user_ids() -> set[int]:
    raw = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").strip()
    return {int(uid.strip()) for uid in raw.split(",") if uid.strip()}


def _is_allowed(user_id: int) -> bool:
    # Explicit opt-in required to allow all users (e.g. for local dev)
    if os.getenv("ALLOW_ALL_USERS", "").lower() == "true":
        return True
    return user_id in _allowed_user_ids()


async def _get_mcp_tools() -> list[dict]:
    """Return the list of MCP tool schemas for the Anthropic API."""
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


async def _run_claude(chat_id: int, user_text: str) -> str:
    """Send a message to Claude, handle tool use, and return the final reply."""
    client = _get_anthropic_client()
    tools = await _get_mcp_tools()

    # Work on a copy; only commit to canonical history on success so that a
    # failed API call never leaves two consecutive "user" turns in the history.
    messages = list(_histories[chat_id])
    messages.append({"role": "user", "content": user_text})

    if len(messages) > MAX_HISTORY_TURNS * 2:
        messages = messages[-(MAX_HISTORY_TURNS * 2):]

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=tools or [],
            messages=messages,
        )

        # Collect any text content from this response turn
        assistant_text = ""
        tool_uses = []
        for block in response.content:
            if block.type == "text":
                assistant_text += block.text
            elif block.type == "tool_use":
                tool_uses.append(block)

        # Append the assistant message to the conversation
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "tool_use" and tool_uses:
            # Execute all requested tool calls
            tool_results = []
            for tu in tool_uses:
                result = await _call_mcp_tool(tu.name, tu.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": str(result),
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            continue

        # Final response — update canonical history and return text
        _histories[chat_id] = messages
        return assistant_text or "(no reply)"


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def _reject_unauthorized(update: Update) -> None:
    await update.message.reply_text("Sorry, you're not authorised to use this bot.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    await update.message.reply_text(
        "Hi! I'm your family grocery and meal planning assistant.\n\n"
        "Commands:\n"
        "/plan — start a meal planning session\n"
        "/order — open a shopping session\n"
        "/forecast — see what you might be running low on\n"
        "/history — recent order summary\n"
        "/cart — current Picnic cart\n\n"
        "Or just chat with me naturally!"
    )


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    await _chat(update, context, "Let's plan meals for the week. What does the family feel like eating?")


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    await _chat(update, context, "I'd like to do some grocery shopping. Can you help me search for products and manage the cart?")


async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    await _chat(update, context, "What groceries are we likely running low on based on our order history?")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    await _chat(update, context, "Show me a summary of our recent grocery orders.")


async def cmd_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        await _reject_unauthorized(update)
        return
    await _chat(update, context, "Show me what's currently in the Picnic cart.")


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

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        reply = await _run_claude(chat_id, message_text)
    except Exception:  # noqa: BLE001
        logger.exception("Error running Claude for chat %d", chat_id)
        reply = "Sorry, something went wrong. Please try again."

    # Telegram messages have a 4096 character limit
    for chunk in _split_message(reply):
        await update.message.reply_text(chunk)


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

async def _post_init(app: Application) -> None:
    """Ensure database tables exist before the bot starts handling messages."""
    await init_db()
    logger.info("Database initialised.")


def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("forecast", cmd_forecast))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("cart", cmd_cart))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _chat))

    logger.info("Starting bot with long polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
