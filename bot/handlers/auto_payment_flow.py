"""UPI (Auto): user pays a fixed-amount QR, sends the UTR, balance is credited automatically.

Flow
  Recharge -> "UPI (Auto)" -> user sends amount -> QR is shown -> user pays and sends the 12-digit UTR
  -> bot verifies it (FamPay mail via the verification API) -> balance credited, exactly once.

Safety rules (same as the reference bot)
  * one UTR can be used only once  (unique _id in `used_refs`)
  * the payment time must fall inside the deposit's time window
  * the amount must match exactly
  * a deposit is credited only by the caller that wins the pending -> done switch
  * 5 wrong UTRs -> short block
The manual UPI flow (payment_flow.py) is untouched.
"""
import asyncio
import io
import logging
import time
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.errors import DuplicateKeyError
from pyrogram import Client, ContinuePropagation, filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from bot.config import Config
from bot.database import Database
from bot.handlers.common import get_actor, main_menu_keyboard, send_or_edit
from bot.services.fam_service import FamService, classify, parse_time, qr_png
from bot.ui import InlineKeyboardButton, LINE, bold

logger = logging.getLogger("tg_account_seller_bot")

VERIFY_TRIES = 3  # the FamPay email can arrive a little late, so look again
VERIFY_GAP_SECONDS = 15
GRACE = timedelta(minutes=3)  # clock tolerance around the deposit window
LATE_GRACE = timedelta(minutes=15)  # user may send the UTR a bit after the QR expired


def _aware(dt: datetime | None) -> datetime | None:
    """Mongo may hand back naive UTC datetimes - make them aware."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def register_auto_payment_handlers(app: Client, db: Database, config: Config, fam: FamService) -> None:
    # uid -> {"step": "amount" | "utr", "dep_id": ObjectId | None}
    state: dict[int, dict] = {}
    fails: dict[int, list] = {}  # uid -> [wrong attempts, blocked-until (monotonic)]
    locks: dict[int, asyncio.Lock] = {}
    alert_at = {"t": -1e9}

    def _lock(uid: int) -> asyncio.Lock:
        if uid not in locks:
            locks[uid] = asyncio.Lock()
        return locks[uid]

    def _blocked_minutes(uid: int) -> int:
        t = fails.get(uid)
        if t and t[1] > time.monotonic():
            return max(1, int((t[1] - time.monotonic()) // 60) + 1)
        return 0

    def _register_fail(uid: int) -> None:
        t = fails.setdefault(uid, [0, 0.0])
        if t[1] and t[1] <= time.monotonic():
            t[0], t[1] = 0, 0.0
        t[0] += 1
        if t[0] >= config.utr_max_fails:
            t[0], t[1] = 0, time.monotonic() + config.utr_block_min * 60

    async def _reserve(ref: str, uid: int, dep_id) -> bool:
        """One UTR can only ever be used once: the unique _id makes the database refuse a second claim."""
        try:
            await db.used_refs.insert_one(
                {"_id": ref, "user_id": uid, "dep_id": dep_id, "state": "reserved", "created_at": _now()}
            )
            return True
        except DuplicateKeyError:
            return False

    async def _release(ids) -> None:
        ids = [i for i in ids if i]
        if ids:
            await db.used_refs.delete_many({"_id": {"$in": ids}, "state": "reserved"})

    async def _alert_admins(client: Client, text: str) -> None:
        if time.monotonic() - alert_at["t"] < 600:  # at most one alert / 10 min
            return
        alert_at["t"] = time.monotonic()
        for admin_id in config.admins:
            try:
                await client.send_message(admin_id, text)
            except Exception:
                pass

    def _cancel_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="rc:cancel", style="danger")]])

    # ---------------- reset auto state when the user moves elsewhere ----------------
    @app.on_callback_query(filters.regex(r"^(menu|buy):"), group=-2)
    async def reset_on_menu(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if user:
            state.pop(user.id, None)

    @app.on_message(
        filters.private & filters.command(["start", "buy", "recharge", "balance", "orders", "profile", "help", "support"]),
        group=-2,
    )
    async def reset_on_command(client: Client, message: Message) -> None:
        user = get_actor(message)
        if user:
            state.pop(user.id, None)

    # ---------------- START: UPI (Auto) button ----------------
    @app.on_callback_query(filters.regex(r"^rc:auto$"))
    async def cb_auto(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        if not fam.enabled:
            await cq.answer("⚠️ UPI (Auto) is not configured yet. Contact support.", show_alert=True)
            return
        await db.upsert_user(user.id, user.username, user.first_name)
        # a fresh start cancels any older unpaid auto deposit
        await db.fam_deposits.update_many({"user_id": user.id, "status": "pending"}, {"$set": {"status": "cancelled"}})
        state[user.id] = {"step": "amount", "dep_id": None}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:recharge", style="danger")]])
        await send_or_edit(
            client,
            cq,
            f"⚡ {bold('UPI (AUTO) RECHARGE')}\n"
            f"{LINE}\n"
            f"Send the **amount in ₹** you want to add.\n"
            f"Min ₹{config.min_deposit} • Max ₹{config.max_deposit}\n\n"
            "Pay the QR, send your **12-digit UTR**, and your balance is added automatically.",
            reply_markup=kb,
        )

    @app.on_callback_query(filters.regex(r"^rc:cancel$"))
    async def cb_cancel(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        st = state.pop(user.id, None)
        await db.fam_deposits.update_many({"user_id": user.id, "status": "pending"}, {"$set": {"status": "cancelled"}})
        await cq.answer("Cancelled.")
        try:
            await cq.message.delete()
        except Exception:
            pass
        await client.send_message(
            user.id,
            f"❌ {bold('DEPOSIT CANCELLED')}\n{LINE}\nYou can start a new recharge anytime.",
            reply_markup=main_menu_keyboard(),
        )

    # ---------------- text: amount, then UTR ----------------
    @app.on_message(filters.private & filters.text & ~filters.command(["start", "admin"]), group=2)
    async def handle_text(client: Client, message: Message) -> None:
        user = get_actor(message)
        text = (message.text or "").strip()
        if not user or not text or text.startswith("/"):
            raise ContinuePropagation
        st = state.get(user.id)

        if st and st["step"] == "amount":
            await _handle_amount(client, message, user, text)
            return

        got = classify(text)
        if not got:
            if st and st["step"] == "utr":
                await message.reply_text(
                    "❌ **That doesn't look like a UTR.**\n"
                    "Send the 12-digit number (UPI Ref No / UTR), or the FamPay ID that starts with FMP."
                )
                return
            raise ContinuePropagation

        # UTR sent: use the in-memory deposit, or (after a bot restart) the user's latest open deposit
        dep = None
        if st and st.get("dep_id"):
            dep = await db.fam_deposits.find_one({"_id": st["dep_id"]})
        if dep is None:
            dep = await db.fam_deposits.find_one({"user_id": user.id, "status": "pending"}, sort=[("_id", -1)])
        if dep is None:
            raise ContinuePropagation
        async with _lock(user.id):
            await _verify(client, message, user, dep, got[0], got[1])

    async def _handle_amount(client: Client, message: Message, user, text: str) -> None:
        raw = text.replace("₹", "").replace(",", "").strip()
        if not raw.isdigit():
            await message.reply_text("⚠️ Please send a valid amount in ₹ (for example `100`).")
            return
        amount = int(raw)
        if amount < config.min_deposit or amount > config.max_deposit:
            await message.reply_text(f"⚠️ Amount must be between ₹{config.min_deposit} and ₹{config.max_deposit}.")
            return

        await db.fam_deposits.update_many({"user_id": user.id, "status": "pending"}, {"$set": {"status": "cancelled"}})
        created = _now()
        ins = await db.fam_deposits.insert_one(
            {
                "user_id": user.id,
                "username": user.username or "",
                "amount": amount,
                "status": "pending",
                "created_at": created,
                "expires_at": created + timedelta(minutes=config.deposit_expiry_min),
            }
        )
        state[user.id] = {"step": "utr", "dep_id": ins.inserted_id}

        caption = (
            f"⚡ {bold('PAY VIA UPI (AUTO)')}\n"
            f"{LINE}\n"
            f"🏦 {bold('UPI ID')} : `{fam.upi}`\n"
            f"💰 {bold('Amount')} : **₹{amount}**\n"
            f"⏳ {bold('Valid for')} : {config.deposit_expiry_min} min\n"
            f"{LINE}\n"
            "1️⃣ Scan the QR and pay the **exact amount**.\n"
            "2️⃣ Send your **12-digit UTR** here (UPI Ref No / Transaction ID).\n"
            "   Paid from FamPay? You can send the ID starting with **FMP**."
        )
        kb = _cancel_kb()
        url = await fam.gen_qr_url(amount)
        if url:
            try:
                await message.reply_photo(url, caption=caption, reply_markup=kb)
                return
            except Exception:
                pass
        png = qr_png(fam.upi, amount, fam.name)
        if png:
            buf = io.BytesIO(png)
            buf.name = "upi_qr.png"
            await message.reply_photo(buf, caption=caption, reply_markup=kb)
        else:
            await message.reply_text(caption, reply_markup=kb)

    # ---------------- verification ----------------
    async def _verify(client: Client, message: Message, user, dep: dict, kind: str, ref: str) -> None:
        uid = user.id
        amount = int(dep["amount"])
        expires_at = _aware(dep["expires_at"])
        if dep["status"] != "pending" or _now() > expires_at + LATE_GRACE:
            state.pop(uid, None)
            await message.reply_text(
                "❌ **This deposit request expired.**\nPlease start the recharge again.",
                reply_markup=main_menu_keyboard(),
            )
            return
        left = _blocked_minutes(uid)
        if left:
            await message.reply_text(f"⏳ **Too many wrong attempts.**\nPlease try again in {left} min.")
            return
        if not await _reserve(ref, uid, dep["_id"]):
            _register_fail(uid)
            await message.reply_text("❌ **This UTR was already used.**\nSend the UTR of your own new payment.")
            return

        wait = await message.reply_text("⏳ **Verifying your payment...**\nThis can take up to a minute.")
        res: dict = {"ok": False, "reason": "error"}
        for i in range(VERIFY_TRIES):
            res = await fam.check(kind, ref, amount)
            if res["ok"] or res["reason"] not in ("not_found", "rate"):
                break
            if i < VERIFY_TRIES - 1:
                await asyncio.sleep(VERIFY_GAP_SECONDS)
        try:
            await wait.delete()
        except Exception:
            pass

        if res["ok"]:
            await _success(client, message, user, dep, ref, res["data"])
            return

        await _release([ref])
        reason = res["reason"]
        if reason in ("auth", "error", "rate"):  # our side / the API is having trouble, not the user
            if reason != "rate":
                await _alert_admins(
                    client,
                    "⚠️ **UPI (Auto) verification is failing.**\n"
                    "Check the Gmail App Password, IMAP, FAM_GMAIL and that the verification API is up.",
                )
            await message.reply_text(
                "⚠️ **Auto verification is temporarily unavailable.**\n"
                f"Please try again in a few minutes, or contact @{config.support_username}."
            )
            return
        _register_fail(uid)
        if reason == "mismatch":
            text = f"❌ **Amount mismatch.**\nYou requested ₹{amount}"
            if res.get("found") is not None:
                text += f" but the payment found is ₹{res['found']}"
            text += ".\nPay the exact amount shown on the QR."
        elif reason == "duplicate":
            text = "❌ **This payment was already used.**"
        else:
            text = (
                "❌ **Payment not found yet.**\n"
                "If you just paid, wait a minute and send the UTR again. Also check that the UTR is correct.\n"
                f"Still stuck? Contact @{config.support_username}."
            )
        await message.reply_text(text, reply_markup=_cancel_kb())

    async def _success(client: Client, message: Message, user, dep: dict, ref: str, d: dict) -> None:
        uid = user.id
        amount = int(dep["amount"])
        created = _aware(dep["created_at"])
        expires = _aware(dep["expires_at"])

        # 1) the payment must have been made inside this deposit's time window
        pay_dt = parse_time(d.get("payment_time_ist"))
        if pay_dt and not (created - GRACE <= pay_dt <= expires + GRACE):
            await _release([ref])
            _register_fail(uid)
            await message.reply_text(
                "❌ **This payment is older than your deposit request.**\n"
                "Pay using the QR shown in this deposit, then send that payment's UTR."
            )
            return

        # 2) the same payment may also be known by its other id (UTR vs FamPay txn id)
        others = {str(d.get("utr") or "").strip().upper(), str(d.get("transaction_id") or "").strip().upper()}
        got: list[str] = []
        for x in [i for i in others if i and i != ref]:
            if await _reserve(x, uid, dep["_id"]):
                got.append(x)
            else:
                await _release(got + [ref])
                _register_fail(uid)
                await message.reply_text("❌ **This payment was already used.**")
                return

        # 3) credit exactly once
        if not await db.claim(
            db.fam_deposits, dep["_id"], "pending", "done",
            ref=ref, sender=d.get("sender_name"), paid_at=pay_dt, done_at=_now(),
        ):
            await _release(got + [ref])
            state.pop(uid, None)
            await message.reply_text("ℹ️ This deposit was already processed.", reply_markup=main_menu_keyboard())
            return
        try:
            new_bal = await db.add_balance(uid, amount)
        except Exception:
            logger.exception("Credit failed for deposit %s", dep["_id"])
            await db.fam_deposits.update_one({"_id": dep["_id"]}, {"$set": {"status": "credit_failed"}})
            await message.reply_text(
                f"⚠️ Payment verified but crediting failed. Contact @{config.support_username} with UTR `{ref}`."
            )
            for admin_id in config.admins:
                try:
                    await client.send_message(
                        admin_id, f"🚨 Credit FAILED after verified payment.\nUser `{uid}` • ₹{amount} • UTR `{ref}`"
                    )
                except Exception:
                    pass
            return
        await db.used_refs.update_many({"_id": {"$in": got + [ref]}}, {"$set": {"state": "used"}})
        fails.pop(uid, None)
        state.pop(uid, None)

        await message.reply_text(
            f"✅ {bold('PAYMENT APPROVED')}\n"
            f"{LINE}\n"
            f"💵 {bold('Added')} : ₹{amount}\n"
            f"🔖 {bold('UTR')} : `{ref}`\n"
            f"💰 {bold('New Balance')} : ₹{new_bal}\n"
            f"{LINE}\n"
            "You can now buy numbers. Happy shopping! 🛒",
            reply_markup=main_menu_keyboard(),
        )
        uname = f"@{user.username}" if user.username else "no username"
        for admin_id in config.admins:
            try:
                await client.send_message(
                    admin_id,
                    f"⚡ **UPI (Auto) deposit**\n{user.first_name or 'User'} ({uname}) `{uid}`\n"
                    f"💰 ₹{amount} • UTR `{ref}`",
                )
            except Exception:
                pass
