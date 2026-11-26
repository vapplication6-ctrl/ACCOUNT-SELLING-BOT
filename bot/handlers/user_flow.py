from datetime import timedelta
from typing import Any

from bson import ObjectId
from pyrogram import Client, ContinuePropagation, filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from bot.commands import USER_COMMAND_NAMES
from bot.config import Config, MAX_OTP_ATTEMPTS, SERVERS
from bot.database import Database
from bot.handlers.common import (
    format_welcome_text,
    get_actor,
    main_menu_keyboard,
    send_or_edit,
    server_label,
    server_select_keyboard,
)
from bot.handlers.payment_flow import get_payment_settings
from bot.services.session_service import SessionService
from bot.services.stock_service import StockService
from bot.ui import InlineKeyboardButton, LINE, bold, iso_from_flag, sc

COUNTRIES_PER_PAGE = 16  # 2 per row x 8 rows


def _otp_text(order: dict[str, Any], otp_code: str, balance: int, password: str | None = None) -> str:
    pwd_line = f"🔐 {bold('2FA Password')} : `{password}`\n" if password else ""
    return (
        f"🔐 {bold('OTP DELIVERED')}\n"
        f"{LINE}\n"
        f"📞 {bold('Number')} : `{order['phone_number']}`\n"
        f"🔢 {bold('OTP Code')} : `{otp_code}`\n"
        f"{pwd_line}"
        f"💸 {bold('Charged')} : ₹{order['price']}  •  {bold('Balance')} : ₹{balance}\n"
        f"{LINE}\n"
        "Log in to your Telegram account now, then tap **Done** to secure it."
    )


def _otp_keyboard(order_id_str: str, show_2fa: bool) -> InlineKeyboardMarkup:
    rows: list[list[Any]] = []
    if show_2fa:
        rows.append([InlineKeyboardButton("🔐 2FA Password", callback_data=f"ord:2fa:{order_id_str}", style="primary")])
    rows.append([InlineKeyboardButton("✅ Done", callback_data=f"ord:done:{order_id_str}", style="success")])
    return InlineKeyboardMarkup(rows)


def _country_label(c: dict[str, Any]) -> str:
    iso = iso_from_flag(c["flag"])
    return f"{c['flag']}{iso}{c['country_code']} | ₹{c['price']}"


def _filter_countries(countries: list[dict[str, Any]], query: str | None) -> list[dict[str, Any]]:
    if not query:
        return countries
    q = query.strip().lower().lstrip("+")
    if not q:
        return countries
    out = []
    for c in countries:
        iso = iso_from_flag(c["flag"]).lower()
        if q in str(c["country_name"]).lower() or q in c["country_code"].lstrip("+") or q in iso:
            out.append(c)
    return out


def register_user_handlers(
    app: Client,
    db: Database,
    stock_service: StockService,
    session_service: SessionService,
    config: Config,
) -> None:
    pending_recharge: dict[int, bool] = {}
    pending_search: dict[int, str] = {}  # user_id -> server_key while waiting for search text
    last_query: dict[int, str] = {}  # user_id -> active country search text

    async def _country_list_view(
        user_id: int, server_key: str, page: int, query: str | None
    ) -> tuple[str, InlineKeyboardMarkup]:
        all_countries = await stock_service.list_server_countries(server_key)
        countries = _filter_countries(all_countries, query)
        bal = await db.get_balance(user_id)

        if not countries:
            if query:
                body = f"🔎 No country found for **{query}**."
            else:
                body = "📡 No stock available on this server right now.\nPlease check back soon."
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔍 Search", callback_data=f"buy:search:{server_key}", style="primary")]
                    if all_countries
                    else [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home", style="primary")],
                    [InlineKeyboardButton("⬅️ Back", callback_data="menu:buy", style="danger")],
                ]
            )
            return f"{bold('SERVER')} • {server_label(server_key)}\n{LINE}\n{body}", kb

        total_pages = max(1, -(-len(countries) // COUNTRIES_PER_PAGE))
        page = min(max(1, page), total_pages)
        chunk = countries[(page - 1) * COUNTRIES_PER_PAGE : page * COUNTRIES_PER_PAGE]

        rows: list[list[Any]] = []
        for i in range(0, len(chunk), 2):
            rows.append(
                [
                    InlineKeyboardButton(
                        _country_label(c),
                        callback_data=f"buy:cty:{server_key}:{c['country_code']}",
                        style="success",
                    )
                    for c in chunk[i : i + 2]
                ]
            )

        if total_pages > 1:
            start = max(1, min(page - 2, total_pages - 4))
            end = min(total_pages, start + 4)
            rows.append(
                [
                    InlineKeyboardButton(
                        f"[{n}]" if n == page else str(n),
                        callback_data=f"buy:pg:{server_key}:{n}",
                        style="primary",
                    )
                    for n in range(start, end + 1)
                ]
            )
        rows.append(
            [
                InlineKeyboardButton("🔍 Search", callback_data=f"buy:search:{server_key}", style="primary"),
                InlineKeyboardButton("⬅️ Back", callback_data="menu:buy", style="danger"),
            ]
        )

        title = "Search results:" if query else "Click country to view price and stock:"
        text = (
            f"{title}\n"
            f"{LINE}\n"
            f"✅ {bold('Total balance')}: ₹{bal}\n"
            f"✅ {bold('Server')}: {server_label(server_key)}\n"
            f"✅ {bold('Page')} {page} of {total_pages}\n"
            f"{LINE}"
        )
        return text, InlineKeyboardMarkup(rows)

    @app.on_message(filters.private & filters.command(USER_COMMAND_NAMES), group=-1)
    async def reset_on_command(client: Client, message: Message) -> None:
        """Any slash command starts fresh: forget half-finished input and make sure the user exists."""
        user = get_actor(message)
        if not user:
            return
        pending_recharge.pop(user.id, None)
        pending_search.pop(user.id, None)
        last_query.pop(user.id, None)
        await db.upsert_user(user.id, user.username, user.first_name)

    @app.on_message(filters.private & filters.command("start"))
    async def cmd_start(client: Client, message: Message) -> None:
        user = get_actor(message)
        if not user:
            return
        pending_recharge.pop(user.id, None)
        pending_search.pop(user.id, None)
        last_query.pop(user.id, None)
        doc = await db.upsert_user(user.id, user.username, user.first_name)
        bal = int(doc.get("balance", 0))
        await message.reply_text(
            format_welcome_text(user.first_name or "User", bal, user.id),
            reply_markup=main_menu_keyboard(),
        )

    @app.on_callback_query(filters.regex(r"^menu:home$"))
    async def cb_home(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        pending_recharge.pop(user.id, None)
        pending_search.pop(user.id, None)
        last_query.pop(user.id, None)
        doc = await db.upsert_user(user.id, user.username, user.first_name)
        bal = int(doc.get("balance", 0))
        await send_or_edit(
            client,
            cq,
            format_welcome_text(user.first_name or "User", bal, user.id),
            reply_markup=main_menu_keyboard(),
        )

    @app.on_message(filters.private & filters.command("buy"))
    @app.on_callback_query(filters.regex(r"^menu:buy$"))
    async def cb_menu_buy(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if user:
            pending_search.pop(user.id, None)
            last_query.pop(user.id, None)
        await send_or_edit(
            client,
            cq,
            f"🛒 {bold('BUY NUMBER')}\n"
            f"{LINE}\n"
            "Choose the server that fits your need.\n\n"
            f"🟢 **Server 1** • {sc('good quality acc')}\n"
            f"⚪ **Server 2** • {sc('spam acc')}\n"
            f"{LINE}\n"
            f"👇 {sc('select a server')}",
            reply_markup=server_select_keyboard("buy"),
        )

    @app.on_callback_query(filters.regex(r"^buy:srv:(server_1|server_2)$"))
    async def cb_buy_server(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        server_key = cq.matches[0].group(1)
        pending_search.pop(user.id, None)
        last_query.pop(user.id, None)
        text, kb = await _country_list_view(user.id, server_key, 1, None)
        await send_or_edit(client, cq, text, reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^buy:pg:(server_1|server_2):(\d+)$"))
    async def cb_buy_page(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        server_key = cq.matches[0].group(1)
        page = int(cq.matches[0].group(2))
        text, kb = await _country_list_view(user.id, server_key, page, last_query.get(user.id))
        await send_or_edit(client, cq, text, reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^buy:search:(server_1|server_2)$"))
    async def cb_buy_search(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        server_key = cq.matches[0].group(1)
        pending_search[user.id] = server_key
        pending_recharge.pop(user.id, None)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Cancel", callback_data=f"buy:srv:{server_key}", style="danger")]]
        )
        await send_or_edit(
            client,
            cq,
            f"🔍 {bold('SEARCH COUNTRY')}\n"
            f"{LINE}\n"
            "Send a country **name**, **code** or **ISO**.\n"
            "Examples: `India`, `+91`, `BD`",
            reply_markup=kb,
        )

    @app.on_callback_query(filters.regex(r"^buy:cty:(server_1|server_2):(\+[0-9]+)$"))
    async def cb_country_detail(client: Client, cq: CallbackQuery) -> None:
        server_key = cq.matches[0].group(1)
        country_code = cq.matches[0].group(2)
        countries = await stock_service.list_server_countries(server_key)
        selected = next((c for c in countries if c["country_code"] == country_code), None)

        if not selected or selected["available_stock"] <= 0:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Countries", callback_data=f"buy:srv:{server_key}", style="danger")]]
            )
            await send_or_edit(
                client,
                cq,
                f"⚠️ {bold('OUT OF STOCK')}\n{LINE}\nThis country has just sold out. Please pick another one.",
                reply_markup=kb,
            )
            return

        text = (
            f"📋 {bold('COUNTRY DETAILS')}\n"
            f"{LINE}\n"
            f"🌐 {bold('Server')} : {server_label(server_key)}\n"
            f"🏳️ {bold('Country')} : {selected['flag']} {selected['country_name']}\n"
            f"📞 {bold('Code')} : `{selected['country_code']}`\n"
            f"📦 {bold('In Stock')} : {selected['available_stock']}\n"
            f"💰 {bold('Price')} : ₹{selected['price']}\n"
            f"{LINE}\n"
            f"👇 {sc('tap buy to reserve a number')}"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Buy",
                        callback_data=f"buy:exec:{server_key}:{country_code}",
                        style="success",
                    )
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"buy:srv:{server_key}", style="danger")],
            ]
        )
        await send_or_edit(client, cq, text, reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^buy:exec:(server_1|server_2):(\+[0-9]+)$"))
    async def cb_buy_execute(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        await db.upsert_user(user.id, user.username, user.first_name)
        server_key = cq.matches[0].group(1)
        country_code = cq.matches[0].group(2)

        ok, msg, order = await stock_service.create_locked_order(
            user_id=user.id,
            server=server_key,
            country_code=country_code,
        )
        if not ok or not order:
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("💳 Recharge", callback_data="menu:recharge", style="success")],
                    [InlineKeyboardButton("⬅️ Back", callback_data=f"buy:cty:{server_key}:{country_code}", style="danger")],
                ]
            )
            await send_or_edit(client, cq, f"⚠️ {msg}", reply_markup=kb)
            return

        order_id_str = str(order["_id"])
        text = (
            f"📱 {bold('NUMBER RESERVED')}\n"
            f"{LINE}\n"
            f"📞 {bold('Number')} : `{order['phone_number']}`\n"
            f"🏳️ {bold('Country')} : {order.get('flag', '🌍')} {order.get('country_name', country_code)}\n"
            f"💰 {bold('Price')} : ₹{order['price']} *(charged only after OTP delivery)*\n"
            f"⏳ {bold('Timer')} : 20 minutes to complete login\n"
            f"{LINE}\n"
            "1️⃣ Enter this number in Telegram to request the login code.\n"
            "2️⃣ Tap **Get OTP** below to receive your code.\n"
            "3️⃣ Once logged in, tap **Done** to secure the account."
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔑 Get OTP", callback_data=f"ord:otp:{order_id_str}", style="success")],
                [InlineKeyboardButton("✅ Done", callback_data=f"ord:done:{order_id_str}", style="success")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home", style="primary")],
            ]
        )
        await send_or_edit(client, cq, text, reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^ord:otp:([0-9a-fA-F]{24})$"))
    async def cb_order_otp(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        order_id_str = cq.matches[0].group(1)
        order = await stock_service.get_order(order_id_str)
        if not order or order["user_id"] != user.id:
            await cq.answer("Order not found.", show_alert=True)
            return

        status = order.get("status")
        if status in ("expired", "cancelled"):
            await send_or_edit(
                client,
                cq,
                f"⏳ {bold('ORDER EXPIRED')}\n{LINE}\n"
                "This 20-minute order has expired and the number went back to stock.\n"
                "No balance was deducted.",
                reply_markup=main_menu_keyboard(),
            )
            return

        if status == "completed" or order.get("new_device_logged_in"):
            await cq.answer("Order already completed.", show_alert=True)
            return

        attempts_used = int(order.get("otp_attempts_used", 0))
        max_attempts = int(order.get("max_otp_attempts", MAX_OTP_ATTEMPTS))
        if attempts_used >= max_attempts:
            await cq.answer("The OTP for this order was already delivered.", show_alert=True)
            return

        account = await stock_service.get_account_for_order(order)
        if not account or not account.get("session_string"):
            await cq.answer("Session unavailable for this account.", show_alert=True)
            return

        await cq.answer("Checking for your OTP...", show_alert=False)

        ok, status_msg, otp_code, latest_msg_id, baseline = await session_service.fetch_otp_from_777000(
            session_string=account["session_string"],
            not_before=(order.get("created_at") - timedelta(seconds=60)) if order.get("created_at") else None,
        )
        if not ok or not otp_code:
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔑 Get OTP", callback_data=f"ord:otp:{order_id_str}", style="success")],
                    [InlineKeyboardButton("✅ Done", callback_data=f"ord:done:{order_id_str}", style="success")],
                ]
            )
            await send_or_edit(
                client,
                cq,
                f"⏳ {bold('WAITING FOR OTP')}\n"
                f"{LINE}\n"
                f"📞 {bold('Number')} : `{order['phone_number']}`\n"
                f"ℹ️ {status_msg}\n"
                f"{LINE}\n"
                "Request the code in Telegram first, then tap **Get OTP**.",
                reply_markup=kb,
            )
            return

        # balance is cut here, the moment the OTP is delivered
        charged_ok, charge_msg, new_bal = await stock_service.mark_otp_delivered_and_charge(
            order_id=order["_id"],
            user_id=user.id,
            price=int(order["price"]),
            otp_code=otp_code,
            msg_id=latest_msg_id,
            baseline_hashes=baseline,
        )
        if not charged_ok:
            await send_or_edit(client, cq, f"⚠️ {charge_msg}", reply_markup=main_menu_keyboard())
            return

        await send_or_edit(
            client,
            cq,
            _otp_text(order, otp_code, new_bal),
            reply_markup=_otp_keyboard(order_id_str, show_2fa=bool(account.get("two_fa_password"))),
        )

    @app.on_callback_query(filters.regex(r"^ord:2fa:([0-9a-fA-F]{24})$"))
    async def cb_order_2fa(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        order = await stock_service.get_order(cq.matches[0].group(1))
        if not order or order["user_id"] != user.id:
            await cq.answer("Order not found.", show_alert=True)
            return
        if order.get("status") != "otp_sent":
            await cq.answer("Get your OTP first.", show_alert=True)
            return
        account = await stock_service.get_account_for_order(order)
        password = account.get("two_fa_password") if account else None
        if not password:
            await cq.answer("This account has no 2FA password.", show_alert=True)
            return
        bal = await db.get_balance(user.id)
        await send_or_edit(
            client,
            cq,
            _otp_text(order, order.get("last_otp") or "-", bal, password=password),
            reply_markup=_otp_keyboard(str(order["_id"]), show_2fa=False),
        )

    @app.on_callback_query(filters.regex(r"^ord:done:([0-9a-fA-F]{24})$"))
    async def cb_order_done(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        order_id_str = cq.matches[0].group(1)
        order = await stock_service.get_order(order_id_str)
        if not order or order["user_id"] != user.id:
            await cq.answer("Order not found.", show_alert=True)
            return

        if order.get("status") == "completed":
            await send_or_edit(
                client,
                cq,
                f"✅ Order for `{order['phone_number']}` is already completed.",
                reply_markup=main_menu_keyboard(),
            )
            return

        if order.get("status") in ("expired", "cancelled"):
            await send_or_edit(
                client,
                cq,
                f"⏳ {bold('ORDER EXPIRED')}\n{LINE}\nThis order had already expired before completion.",
                reply_markup=main_menu_keyboard(),
            )
            return

        if order.get("status") != "otp_sent":
            await cq.answer("Tap Get OTP first, log in with the code, then tap Done.", show_alert=True)
            return

        account = await stock_service.get_account_for_order(order)
        if not account or not account.get("session_string"):
            await cq.answer("Session unavailable for this account.", show_alert=True)
            return

        await cq.answer("Checking your login...", show_alert=False)
        result, cleanup_note = await session_service.cleanup_after_new_login(
            account["session_string"],
            order.get("baseline_auth_hashes"),
            order.get("otp_delivered_at"),
        )

        if result == "no_new_login":
            await send_or_edit(
                client,
                cq,
                f"⚠️ {bold('LOGIN NOT DETECTED')}\n"
                f"{LINE}\n"
                f"📞 {bold('Number')} : `{order['phone_number']}`\n"
                "We could not see your new login yet.\n"
                "Enter the OTP in Telegram and finish logging in, then tap **Done** again.\n"
                f"{LINE}\n"
                "Old devices are logged out only after your login is detected.",
                reply_markup=_otp_keyboard(order_id_str, show_2fa=bool(account.get("two_fa_password"))),
            )
            return

        await stock_service.mark_order_done(order["_id"], account["_id"], user.id)
        if result == "cleanup_failed":
            for admin_id in config.admins:
                try:
                    await client.send_message(
                        admin_id,
                        f"⚠️ Old devices were NOT fully logged out for `{order['phone_number']}` "
                        f"(buyer `{user.id}`).\n{cleanup_note}",
                    )
                except Exception:
                    pass

        await send_or_edit(
            client,
            cq,
            f"🎉 {bold('ORDER COMPLETED')}\n"
            f"{LINE}\n"
            f"📞 {bold('Number')} : `{order['phone_number']}`\n"
            f"🔒 {bold('Session Cleanup')} : {cleanup_note or 'Completed'}\n"
            f"{LINE}\n"
            "Thank you for choosing us. Come back anytime! 💙",
            reply_markup=main_menu_keyboard(),
        )

    @app.on_message(filters.private & filters.command("recharge"))
    @app.on_callback_query(filters.regex(r"^menu:recharge$"))
    async def cb_recharge(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        pending_recharge.pop(user.id, None)
        pending_search.pop(user.id, None)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🏦 UPI (Manual)", callback_data="rc:manual", style="primary")],
                [InlineKeyboardButton("⚡ UPI (Auto)", callback_data="rc:auto", style="success")],
                [InlineKeyboardButton("⬅️ Back", callback_data="menu:home", style="danger")],
            ]
        )
        await send_or_edit(
            client,
            cq,
            f"💳 {bold('RECHARGE WALLET')}\n"
            f"{LINE}\n"
            f"🆔 {bold('Your ID')} : `{user.id}`\n"
            f"💬 {bold('Support')} : @{config.support_username}\n"
            f"{LINE}\n"
            "Choose how you want to add money:\n\n"
            "🏦 **UPI (Manual)** • pay, send screenshot, admin approves\n"
            "⚡ **UPI (Auto)** • pay, send UTR, balance added instantly",
            reply_markup=kb,
        )

    @app.on_callback_query(filters.regex(r"^rc:manual$"))
    async def cb_recharge_manual(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        pending_recharge[user.id] = True
        pending_search.pop(user.id, None)
        # leaving the auto flow: drop any unpaid auto deposit
        await db.fam_deposits.update_many({"user_id": user.id, "status": "pending"}, {"$set": {"status": "cancelled"}})
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:recharge", style="danger")]])
        await send_or_edit(
            client,
            cq,
            f"💳 {bold('RECHARGE WALLET')} • UPI (Manual)\n"
            f"{LINE}\n"
            f"🆔 {bold('Your ID')} : `{user.id}`\n"
            f"💬 {bold('Support')} : @{config.support_username}\n"
            f"{LINE}\n"
            "Send the **amount in ₹** you want to add.\n"
            "You will get a QR code to pay and your balance is credited after admin approval.",
            reply_markup=kb,
        )

    @app.on_callback_query(filters.regex(r"^rc:paid:([0-9a-fA-F]{24})$"))
    async def cb_recharge_paid(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        req = await db.recharge_requests.find_one({"_id": ObjectId(cq.matches[0].group(1)), "user_id": user.id})
        if not req or req.get("status") != "pending":
            await cq.answer("This recharge request is no longer active.", show_alert=True)
            return
        if req.get("proof_sent"):
            await cq.answer("Your payment proof is already received. Admin is verifying it.", show_alert=True)
            return
        await cq.answer()
        await client.send_message(
            user.id,
            f"📸 {bold('SEND PAYMENT PROOF')}\n{LINE}\n"
            f"Send the **screenshot** of your ₹{req['amount']} payment here "
            "(add your UTR in the caption if you can).",
        )

    @app.on_callback_query(filters.regex(r"^rc:cancel:([0-9a-fA-F]{24})$"))
    async def cb_recharge_cancel(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        rid = ObjectId(cq.matches[0].group(1))
        req = await db.recharge_requests.find_one({"_id": rid, "user_id": user.id})
        if not req or req.get("status") != "pending":
            await cq.answer("This recharge request is no longer active.", show_alert=True)
            return
        if req.get("proof_sent"):
            await cq.answer("Payment proof already sent. Please wait for admin approval.", show_alert=True)
            return
        if not await db.claim(db.recharge_requests, rid, "pending", "cancelled"):
            await cq.answer("This recharge request is no longer active.", show_alert=True)
            return
        await cq.answer("Recharge cancelled.")
        try:
            await cq.message.edit_caption(
                f"❌ {bold('RECHARGE CANCELLED')}\n{LINE}\nYour ₹{req['amount']} request was cancelled.",
                reply_markup=main_menu_keyboard(),
            )
        except Exception:
            await client.send_message(user.id, "❌ Recharge cancelled.", reply_markup=main_menu_keyboard())

    @app.on_message(filters.private & filters.command("balance"))
    async def cmd_balance(client: Client, message: Message) -> None:
        user = get_actor(message)
        if not user:
            return
        bal = await db.get_balance(user.id)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💳 Recharge", callback_data="menu:recharge", style="success")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home", style="primary")],
            ]
        )
        await message.reply_text(
            f"💰 {bold('YOUR BALANCE')}\n{LINE}\n💰 {bold('Balance')} : ₹{bal}\n{LINE}",
            reply_markup=kb,
        )

    @app.on_message(filters.private & filters.command("support"))
    async def cmd_support(client: Client, message: Message) -> None:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("💬 Contact Support", url=f"https://t.me/{config.support_username}", style="primary")]]
        )
        await message.reply_text(
            f"💬 {bold('SUPPORT')}\n{LINE}\nNeed help? Contact @{config.support_username}.",
            reply_markup=kb,
        )

    @app.on_message(filters.private & filters.command("orders"))
    @app.on_callback_query(filters.regex(r"^menu:orders$"))
    async def cb_orders(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        orders = await stock_service.get_user_orders(user.id, limit=10)
        if not orders:
            text = f"📦 {bold('MY ORDERS')}\n{LINE}\nYou have no orders yet.\nTap **Buy Number** to place your first one."
        else:
            lines = [f"📦 {bold('MY RECENT ORDERS')}\n{LINE}"]
            for idx, o in enumerate(orders, start=1):
                lines.append(
                    f"{idx}. `{o.get('phone_number', 'N/A')}` | "
                    f"{o.get('flag', '🌍')} {o.get('country_name', '')} | "
                    f"₹{o.get('price', 0)} | **{o.get('status', '').upper()}**"
                )
            text = "\n".join(lines)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:home", style="danger")]])
        await send_or_edit(client, cq, text, reply_markup=kb)

    @app.on_message(filters.private & filters.command("profile"))
    @app.on_callback_query(filters.regex(r"^menu:profile$"))
    async def cb_profile(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user:
            return
        doc = await db.upsert_user(user.id, user.username, user.first_name)
        orders = await stock_service.get_user_orders(user.id, limit=50)
        completed = sum(1 for o in orders if o.get("status") == "completed")
        text = (
            f"👤 {bold('YOUR PROFILE')}\n"
            f"{LINE}\n"
            f"🆔 {bold('User ID')} : `{user.id}`\n"
            f"👋 {bold('Name')} : {user.first_name or 'User'}\n"
            f"💰 {bold('Balance')} : ₹{int(doc.get('balance', 0))}\n"
            f"✅ {bold('Completed Orders')} : {completed}\n"
            f"{LINE}"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:home", style="danger")]])
        await send_or_edit(client, cq, text, reply_markup=kb)

    @app.on_message(filters.private & filters.command("help"))
    @app.on_callback_query(filters.regex(r"^menu:help$"))
    async def cb_help(client: Client, cq: CallbackQuery) -> None:
        text = (
            f"📖 {bold('HOW TO USE')}\n"
            f"{LINE}\n"
            "1️⃣ Tap **Buy Number** and pick a server.\n"
            "2️⃣ Choose a country and tap **Buy**.\n"
            "3️⃣ You get **20 minutes** to log in with the number.\n"
            "4️⃣ Tap **Get OTP** to receive your code. Balance is charged only when the OTP is delivered.\n"
            "5️⃣ After logging in, tap **Done**. Old devices are logged out once your login is detected.\n"
            f"{LINE}\n"
            f"💬 {sc('need help')}? @{config.support_username}"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:home", style="danger")]])
        await send_or_edit(client, cq, text, reply_markup=kb)

    @app.on_message(filters.private & filters.text & ~filters.command(["start", "admin"]))
    async def handle_recharge_amount_input(client: Client, message: Message) -> None:
        user = get_actor(message)
        if not user or (message.text or "").lstrip().startswith("/"):
            raise ContinuePropagation

        # ---- country search text
        if user.id in pending_search:
            server_key = pending_search.pop(user.id)
            query = (message.text or "").strip()
            if not query:
                raise ContinuePropagation
            last_query[user.id] = query
            text, kb = await _country_list_view(user.id, server_key, 1, query)
            await message.reply_text(text, reply_markup=kb)
            return

        if not pending_recharge.get(user.id):
            raise ContinuePropagation
        raw_text = (message.text or "").strip().lstrip("₹").strip()
        if not raw_text.isdigit() or int(raw_text) <= 0:
            await message.reply_text("⚠️ Please send a valid amount in ₹ (for example `100`).")
            return
        amount = int(raw_text)
        pending_recharge.pop(user.id, None)
        inserted = await db.recharge_requests.insert_one(
            {
                "user_id": user.id,
                "username": user.username or "",
                "amount": amount,
                "status": "pending",
            }
        )
        pay_cfg = await get_payment_settings(db)
        if pay_cfg.get("qr_file_id"):
            upi_line = f"🏦 {bold('UPI ID')} : `{pay_cfg['upi_id']}`\n" if pay_cfg.get("upi_id") else ""
            await message.reply_photo(
                pay_cfg["qr_file_id"],
                caption=(
                    f"💳 {bold('PAY')} ₹{amount}\n"
                    f"{LINE}\n"
                    "Scan the QR and pay the **exact amount**.\n"
                    f"{upi_line}"
                    f"{LINE}\n"
                    "📸 After paying, send the **payment screenshot** here "
                    "(add your UTR in the caption if you can).\n"
                    "✅ Your balance is credited once the admin approves."
                ),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "✅ I've Paid", callback_data=f"rc:paid:{inserted.inserted_id}", style="success"
                            ),
                            InlineKeyboardButton(
                                "❌ Cancel", callback_data=f"rc:cancel:{inserted.inserted_id}", style="danger"
                            ),
                        ]
                    ]
                ),
            )
            return
        await message.reply_text(
            f"✅ {bold('REQUEST LOGGED')}\n"
            f"{LINE}\n"
            f"💰 {bold('Amount')} : ₹{amount}\n"
            f"Please share the payment proof with @{config.support_username} and mention your ID `{user.id}`.",
            reply_markup=main_menu_keyboard(),
        )
