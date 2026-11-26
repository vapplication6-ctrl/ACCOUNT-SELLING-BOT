from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from bot.ui import InlineKeyboardButton, LINE, bold

from bot.config import Config
from bot.database import Database
from bot.handlers.common import get_actor, is_admin, main_menu_keyboard, send_or_edit

SETTINGS_ID = "payment"


async def get_payment_settings(db: Database) -> dict[str, Any]:
    doc = await db.db["settings"].find_one({"_id": SETTINGS_ID})
    return doc or {}


def register_payment_handlers(app: Client, db: Database, config: Config) -> None:
    settings = db.db["settings"]
    # admin_id -> True while waiting for QR photo
    qr_wait: dict[int, bool] = {}

    # ---------------- ADMIN: SET QR + UPI ID ----------------
    @app.on_callback_query(filters.regex(r"^adm:setqr$"))
    async def cb_set_qr(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        qr_wait[user.id] = True
        cfg = await get_payment_settings(db)
        current = f"`{cfg.get('upi_id')}`" if cfg.get("upi_id") else "Not set"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Cancel", callback_data="adm:home")]])
        await send_or_edit(
            client,
            cq,
            "💳 **Payment QR Setup**\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Current UPI ID: {current}\n"
            f"QR: {'✅ Set' if cfg.get('qr_file_id') else '❌ Not set'}\n\n"
            "Send your **UPI QR photo** now, and write your **UPI ID in the caption** (e.g. `name@upi`).",
            reply_markup=kb,
        )

    @app.on_message(filters.private & filters.command(["start", "admin"]), group=1)
    async def clear_qr_wait(client: Client, message: Message) -> None:
        user = get_actor(message)
        if user:
            qr_wait.pop(user.id, None)

    # ---------------- PHOTO / SCREENSHOT HANDLER ----------------
    @app.on_message(filters.private & (filters.photo | filters.document), group=1)
    async def handle_photo(client: Client, message: Message) -> None:
        user = get_actor(message)
        if not user:
            return

        # Admin is setting QR
        if is_admin(user.id, config.admins) and qr_wait.get(user.id):
            if not message.photo:
                await message.reply_text("Please send the QR as a **photo** (not a file).")
                return
            qr_wait.pop(user.id, None)
            upi_id = (message.caption or "").strip()
            await settings.update_one(
                {"_id": SETTINGS_ID},
                {"$set": {"qr_file_id": message.photo.file_id, "upi_id": upi_id}},
                upsert=True,
            )
            await message.reply_text(
                "✅ Payment QR saved."
                + (f"\nUPI ID: `{upi_id}`" if upi_id else "\n(No UPI ID in caption, only QR saved.)")
            )
            return

        # User is sending payment proof for latest pending recharge request
        req = await db.recharge_requests.find_one(
            {"user_id": user.id, "status": "pending", "proof_sent": {"$ne": True}},
            sort=[("_id", -1)],
        )
        if not req:
            return
        utr = (message.caption or "").strip()
        await db.recharge_requests.update_one(
            {"_id": req["_id"]},
            {"$set": {"proof_sent": True, "utr": utr}},
        )
        await message.reply_text(
            f"✅ {bold('PROOF RECEIVED')}\n{LINE}\nThanks! Our admin is verifying your payment.\nYour balance will be credited shortly after approval.",
            reply_markup=main_menu_keyboard(),
        )
        rid = str(req["_id"])
        uname = f"@{user.username}" if user.username else "no username"
        caption = (
            "💳 **New Recharge Request**\n━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 User: {user.first_name or 'User'} ({uname})\n"
            f"🆔 ID: `{user.id}`\n"
            f"💰 Amount: **₹{req['amount']}**\n"
            f"🔖 UTR/Note: {utr or '—'}"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(f"✅ Approve ₹{req['amount']}", callback_data=f"pay:ok:{rid}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"pay:no:{rid}"),
                ]
            ]
        )
        for admin_id in config.admins:
            try:
                await message.copy(admin_id, caption=caption, reply_markup=kb)
            except Exception:
                pass

    # ---------------- ADMIN: APPROVE / REJECT ----------------
    async def _finish(client: Client, cq: CallbackQuery, approve: bool, rid: str) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        try:
            oid = ObjectId(rid)
        except InvalidId:
            await cq.answer("Invalid request.", show_alert=True)
            return
        req = await db.recharge_requests.find_one_and_update(
            {"_id": oid, "status": "pending"},
            {
                "$set": {
                    "status": "approved" if approve else "rejected",
                    "handled_by": user.id,
                    "handled_at": datetime.now(timezone.utc),
                }
            },
        )
        if req is None:
            await cq.answer("Already processed.", show_alert=True)
            try:
                await cq.message.edit_reply_markup(None)
            except Exception:
                pass
            return

        amount = int(req["amount"])
        target_id = int(req["user_id"])
        if approve:
            new_bal = await db.add_balance(target_id, amount)
            user_text = f"✅ {bold('RECHARGE APPROVED')}\n{LINE}\n💵 {bold('Added')} : ₹{amount}\n💰 {bold('New Balance')} : ₹{new_bal}\n{LINE}\nYou can now buy numbers. Happy shopping! 🛒"
            status_line = f"\n\n✅ **Approved** by `{user.id}` — new balance ₹{new_bal}"
        else:
            user_text = f"❌ {bold('RECHARGE REJECTED')}\n{LINE}\nYour request for ₹{amount} could not be verified.\nIf this is a mistake, please contact @{config.support_username}."
            status_line = f"\n\n❌ **Rejected** by `{user.id}`"
        try:
            await client.send_message(target_id, user_text, reply_markup=main_menu_keyboard())
        except Exception:
            pass
        try:
            await cq.message.edit_caption((cq.message.caption or "") + status_line)
        except Exception:
            pass
        await cq.answer("Done.")

    @app.on_callback_query(filters.regex(r"^pay:ok:([0-9a-fA-F]{24})$"))
    async def cb_pay_ok(client: Client, cq: CallbackQuery) -> None:
        await _finish(client, cq, True, cq.matches[0].group(1))

    @app.on_callback_query(filters.regex(r"^pay:no:([0-9a-fA-F]{24})$"))
    async def cb_pay_no(client: Client, cq: CallbackQuery) -> None:
        await _finish(client, cq, False, cq.matches[0].group(1))

    # ---------------- ADMIN: /addbal user_id amount ----------------
    @app.on_message(filters.private & filters.command("addbal"), group=1)
    async def cmd_addbal(client: Client, message: Message) -> None:
        user = get_actor(message)
        if not user or not is_admin(user.id, config.admins):
            return
        parts = message.command
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit() or int(parts[2]) <= 0:
            await message.reply_text("Usage: `/addbal user_id amount`\nExample: `/addbal 123456789 100`")
            return
        target_id, amount = int(parts[1]), int(parts[2])
        if not await db.get_user(target_id):
            await message.reply_text("⚠️ User not found. They must press /start in the bot first.")
            return
        new_bal = await db.add_balance(target_id, amount)
        await message.reply_text(f"✅ Added ₹{amount} to `{target_id}`. New balance: ₹{new_bal}")
        try:
            await client.send_message(
                target_id,
                f"✅ {bold('WALLET RECHARGED')}\n{LINE}\n💵 {bold('Added')} : ₹{amount}\n💰 {bold('New Balance')} : ₹{new_bal}",
                reply_markup=main_menu_keyboard(),
            )
        except Exception:
            pass
