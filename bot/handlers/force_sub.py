"""Force-Subscribe: the user must join all required channels/groups before using the bot.

* Works for public channels, private channels (invite link) and "request to join" channels.
* A pending join request counts as joined (the bot records requests through on_chat_join_request).
* Admins in ADMINS are never blocked.

Setup (Heroku Config Vars):
  FSUB_IDS = -1001234567890,-1009876543210,@silentchannel828282
  (one id per link, SAME ORDER as FSUB_LINKS in bot/config.py)
  The bot must be ADMIN in every one of these channels/groups (with "Invite users" permission
  for request-to-join channels, otherwise Telegram does not send join requests to the bot).
"""
import logging
import time

from pyrogram import Client, StopPropagation, enums, filters
from pyrogram.errors import UserNotParticipant
from pyrogram.types import CallbackQuery, ChatJoinRequest, InlineKeyboardMarkup, Message

from bot.config import Config
from bot.database import Database
from bot.handlers.common import format_welcome_text, main_menu_keyboard
from bot.ui import InlineKeyboardButton, LINE, bold

logger = logging.getLogger("tg_account_seller_bot")

CACHE_SECONDS = 10 * 60  # a user who passed the check is not re-checked for 10 minutes
_OK_STATUSES = (
    enums.ChatMemberStatus.MEMBER,
    enums.ChatMemberStatus.ADMINISTRATOR,
    enums.ChatMemberStatus.OWNER,
)


def _ref(raw: str):
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        if not raw.startswith("-") and len(raw) >= 10:
            raw = "-100" + raw  # channel id pasted without the -100 prefix
        return int(raw)
    if raw and not raw.startswith("@"):
        raw = "@" + raw
    return raw


def register_force_sub_handlers(app: Client, db: Database, config: Config) -> None:
    links = list(config.fsub_links)
    ids = list(config.fsub_ids)
    # (chat ref, invite link) pairs; a channel without an id cannot be checked
    channels: list[tuple[object, str]] = []
    for i, link in enumerate(links):
        if i < len(ids) and ids[i].strip():
            channels.append((_ref(ids[i]), link))
        else:
            logger.warning("Force-sub: no chat id for link %s (set FSUB_IDS) - it is NOT enforced", link)
    if not channels:
        logger.warning("Force-sub is OFF (no FSUB_IDS configured).")
        return
    logger.info("Force-sub ON for %d channel(s).", len(channels))

    reqs = db.db["join_requests"]
    verified: dict[int, float] = {}
    warned: set = set()

    # ---------- remember join requests (request-to-join channels) ----------
    @app.on_chat_join_request()
    async def on_join_request(client: Client, req: ChatJoinRequest) -> None:
        try:
            await reqs.update_one(
                {"user_id": req.from_user.id, "chat_id": req.chat.id},
                {"$set": {"user_id": req.from_user.id, "chat_id": req.chat.id}},
                upsert=True,
            )
        except Exception:
            logger.exception("force-sub: could not save join request")

    async def _joined(client: Client, uid: int, ref) -> bool:
        try:
            m = await client.get_chat_member(ref, uid)
            if m.status in _OK_STATUSES:
                return True
            if m.status == enums.ChatMemberStatus.RESTRICTED:
                return bool(getattr(m, "is_member", True))
            return False  # left / banned
        except UserNotParticipant:
            if isinstance(ref, int):
                return bool(await reqs.find_one({"user_id": uid, "chat_id": ref}))
            return False
        except Exception as exc:
            # bot not admin / wrong id / API trouble: do not lock everybody out
            if ref not in warned:
                warned.add(ref)
                logger.warning("force-sub: cannot check %s (%s) - skipped. Make the bot admin there.", ref, type(exc).__name__)
            return True

    async def _all_joined(client: Client, uid: int) -> bool:
        if verified.get(uid, 0) > time.monotonic():
            return True
        for ref, _ in channels:
            if not await _joined(client, uid, ref):
                verified.pop(uid, None)
                return False
        verified[uid] = time.monotonic() + CACHE_SECONDS
        return True

    def _kb() -> InlineKeyboardMarkup:
        rows = [
            [InlineKeyboardButton(f"📢 Join Channel {i}", url=link, style="primary")]
            for i, (_, link) in enumerate(channels, 1)
        ]
        rows.append([InlineKeyboardButton("✅ Verify", callback_data="fs:verify", style="success")])
        return InlineKeyboardMarkup(rows)

    def _text() -> str:
        return (
            f"🔒 {bold('JOIN REQUIRED')}\n"
            f"{LINE}\n"
            "To use this bot you must first join all the channels below.\n\n"
            "1️⃣ Tap every **Join** button (send a join request if asked).\n"
            "2️⃣ Then tap **✅ Verify**."
        )

    # ---------- gate: runs before every other handler ----------
    @app.on_message(filters.private, group=-10)
    async def gate_message(client: Client, message: Message) -> None:
        user = message.from_user
        if not user or user.id in config.admins:
            return
        if await _all_joined(client, user.id):
            return
        await message.reply_text(_text(), reply_markup=_kb())
        raise StopPropagation

    @app.on_callback_query(group=-10)
    async def gate_callback(client: Client, cq: CallbackQuery) -> None:
        user = cq.from_user
        if not user or user.id in config.admins or (cq.data or "") == "fs:verify":
            return
        if await _all_joined(client, user.id):
            return
        try:
            await cq.answer("🔒 Join all channels first.", show_alert=True)
            await client.send_message(user.id, _text(), reply_markup=_kb())
        except Exception:
            pass
        raise StopPropagation

    # ---------- Verify button ----------
    @app.on_callback_query(filters.regex(r"^fs:verify$"))
    async def cb_verify(client: Client, cq: CallbackQuery) -> None:
        user = cq.from_user
        if not user:
            return
        verified.pop(user.id, None)
        if not await _all_joined(client, user.id):
            await cq.answer("❌ You have not joined all channels yet.", show_alert=True)
            return
        await cq.answer("✅ Verified!")
        try:
            await cq.message.delete()
        except Exception:
            pass
        doc = await db.upsert_user(user.id, user.username, user.first_name)
        await client.send_message(
            user.id,
            format_welcome_text(user.first_name or "User", int(doc.get("balance", 0)), user.id),
            reply_markup=main_menu_keyboard(),
        )
