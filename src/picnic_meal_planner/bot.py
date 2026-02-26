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


def _allowed_user_ids() -> set[int] | None:
    raw = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").strip()
    if not raw:
        return None  # allow everyone
    return {int(uid.strip()) for uid in raw.split(",") if uid.strip()}


def _is_allowed(user_id: int) -> bool:
    allowed = _allowed_user_ids()
    return allowed is None or user_id in allowed


async def _get_mcp_tools() -> list[dict]:
    """Return the list of MCP tool schemas for the Anthropic API."""
    global _mcp_tools
    if _mcp_tools is not None:
        return _mcp_tools

    # Import here to avoid circular imports and to defer Picnic auth
    from .mcp_server import mcp  # noqa: PLC0415

    tools = []
    for tool_name, tool_fn in mcp._tool_manager._tools.items():  # type: ignore[attr-defined]
        schema = tool_fn.parameters if hasattr(tool_fn, "parameters") else {}
        tools.append(
            {
                "name": tool_name,
                "description": tool_fn.description or "",
                "input_schema": schema or {"type": "object", "properties": {}},
            }
        )
    _mcp_tools = tools
    return _mcp_tools


async def _call_mcp_tool(name: str, input_data: dict):
    """Dispatch a tool call to the MCP server in-process."""
    from .mcp_server import mcp  # noqa: PLC0415

    tool_fn = mcp._tool_manager._tools.get(name)  # type: ignore[attr-defined]
    if tool_fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        result = await tool_fn.fn(**input_data)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("MCP tool %s raised an error", name)
        return {"error": str(exc)}


async def _run_claude(chat_id: int, user_text: str) -> str:
    """Send a message to Claude, handle tool use, and return the final reply."""
    client = _get_anthropic_client()
    tools = await _get_mcp_tools()

    history = _histories[chat_id]
    history.append({"role": "user", "content": user_text})

    # Keep history bounded
    if len(history) > MAX_HISTORY_TURNS * 2:
        history[:] = history[-(MAX_HISTORY_TURNS * 2):]

    messages = list(history)

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

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
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
        return
    await _chat(update, context, "Let's plan meals for the week. What does the family feel like eating?")


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await _chat(update, context, "I'd like to do some grocery shopping. Can you help me search for products and manage the cart?")


async def cmd_forecast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await _chat(update, context, "What groceries are we likely running low on based on our order history?")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await _chat(update, context, "Show me a summary of our recent grocery orders.")


async def cmd_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update.effective_user.id):
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
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error running Claude for chat %d", chat_id)
        reply = f"Sorry, something went wrong: {exc}"

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

def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = (
        Application.builder()
        .token(token)
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
