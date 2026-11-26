from typing import Any
from pyrogram import Client
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from bot.config import SERVERS, SERVER_TAGS
from bot.ui import InlineKeyboardButton, LINE, bold, sc


def is_admin(user_id: int, admins: set[int]) -> bool:
    return user_id in admins


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒 Buy Number", callback_data="menu:buy", style="success")],
            [InlineKeyboardButton("💳 Recharge", callback_data="menu:recharge", style="primary")],
            [
                InlineKeyboardButton("📦 My Orders", callback_data="menu:orders", style="primary"),
                InlineKeyboardButton("👤 Profile", callback_data="menu:profile", style="primary"),
            ],
            [InlineKeyboardButton("📖 How To Use", callback_data="menu:help", style="primary")],
        ]
    )


def _safe_name(name: str) -> str:
    cleaned = "".join(ch for ch in (name or "") if ch not in "[]()`*_~|\\")
    return cleaned.strip() or "User"


def format_welcome_text(first_name: str, balance: int, user_id: int) -> str:
    # Name is a clickable mention (opens the user's profile).
    mention = f"[{_safe_name(first_name)}](tg://user?id={user_id})"
    return (
        f"✨ {bold('WELCOME TO OTP BOT')} ✨\n"
        f"{LINE}\n"
        f"👋 Hello, {mention}\n\n"
        "Your premium marketplace for fresh Telegram numbers. "
        "Fast, secure and fully automated.\n\n"
        f"⚡ {sc('instant delivery')} • number & login code in one place\n"
        f"🔐 {sc('secure otp')} • code arrives right inside the bot\n"
        f"🛡 {sc('safe purchase')} • balance is charged only after OTP delivery\n"
        f"{LINE}\n"
        f"💰 {bold('Balance')} : ₹{balance}\n"
        f"{LINE}\n"
        f"👇 {sc('choose an option below')}"
    )


def server_label(server_key: str) -> str:
    return f"{SERVERS[server_key]} {SERVER_TAGS.get(server_key, '')}".strip()


def server_select_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(server_label("server_1"), callback_data=f"{prefix}:srv:server_1", style="success")],
            [InlineKeyboardButton(server_label("server_2"), callback_data=f"{prefix}:srv:server_2", style="primary")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu:home", style="danger")],
        ]
    )


async def send_or_edit(
    client: Client,
    target: Message | CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    if isinstance(target, CallbackQuery):
        try:
            await target.answer()
        except Exception:
            pass
        if target.message:
            try:
                return await target.message.edit_text(text, reply_markup=reply_markup)
            except Exception:
                return await client.send_message(target.from_user.id, text, reply_markup=reply_markup)
    return await target.reply_text(text, reply_markup=reply_markup)


def get_actor(event: Message | CallbackQuery) -> Any:
    return event.from_user
