"""Slash commands for normal users + the Telegram command menu (the 'Menu' button near the text box).

Admin commands (/admin, /addbal) are intentionally NOT listed here, so users never see them.
"""
import logging

from pyrogram import Client
from pyrogram.types import BotCommand, BotCommandScopeDefault

logger = logging.getLogger("tg_account_seller_bot")

# (command, description shown in the Telegram menu)
USER_COMMANDS: list[tuple[str, str]] = [
    ("start", "START BOT"),
    ("buy", "BUY NUMBER"),
    ("recharge", "ADD MONEY"),
    ("balance", "CHECK BALANCE"),
    ("orders", "MY ORDERS"),
    ("profile", "MY PROFILE"),
    ("help", "HOW TO USE"),
    ("support", "SUPPORT"),
]

USER_COMMAND_NAMES: list[str] = [name for name, _ in USER_COMMANDS]


async def setup_bot_commands(app: Client) -> None:
    """Publish the command list so Telegram shows it in the bot's Menu."""
    try:
        await app.set_bot_commands(
            [BotCommand(name, desc) for name, desc in USER_COMMANDS],
            scope=BotCommandScopeDefault(),
        )
        logger.info("Bot command menu updated (%d commands).", len(USER_COMMANDS))
    except Exception as exc:
        logger.warning("Could not set bot commands: %s", type(exc).__name__)
