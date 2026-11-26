from typing import Any
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from bot.ui import InlineKeyboardButton
from bot.config import Config, SERVERS
from bot.database import Database
from bot.handlers.common import get_actor, is_admin, send_or_edit, server_select_keyboard
from bot.services.session_service import LoginSessionHandle, SessionService
from bot.services.stock_service import StockService
from bot.utils.country import (
    CountryInfo,
    detect_country,
    lookup_country,
    normalize_dial_code,
    normalize_phone,
    validate_phone_for_country,
)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Stock", callback_data="adm:stock"),
                InlineKeyboardButton("📈 Status", callback_data="adm:status"),
            ],
            [
                InlineKeyboardButton("➕ Add Stock", callback_data="adm:addstock"),
                InlineKeyboardButton("🧾 History", callback_data="adm:history"),
            ],
            [
                InlineKeyboardButton("🧊 Frozen IDs", callback_data="adm:frozen"),
                InlineKeyboardButton("⌛ Expired Sessions", callback_data="adm:expired"),
            ],
            [InlineKeyboardButton("💳 Payment QR", callback_data="adm:setqr")],
        ]
    )


def register_admin_handlers(
    app: Client,
    db: Database,
    stock_service: StockService,
    session_service: SessionService,
    config: Config,
) -> None:
    # admin_id -> state dict
    admin_states: dict[int, dict[str, Any]] = {}

    async def _clear_login_handle(admin_id: int) -> None:
        st = admin_states.get(admin_id)
        if st and isinstance(st.get("handle"), LoginSessionHandle):
            await st["handle"].close_quietly()

    def _add_cancel_kb() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="adm:add_cancel")]])

    @app.on_callback_query(filters.regex(r"^adm:add_cancel$"))
    async def cb_add_cancel(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        await _clear_login_handle(user.id)
        admin_states.pop(user.id, None)
        await send_or_edit(client, cq, "❌ Add Stock cancelled.", reply_markup=admin_panel_keyboard())

    @app.on_message(filters.private & filters.command("admin"))
    async def cmd_admin(client: Client, message: Message) -> None:
        user = get_actor(message)
        if not user or not is_admin(user.id, config.admins):
            return
        await _clear_login_handle(user.id)
        admin_states.pop(user.id, None)
        await message.reply_text(
            "Here is the button for admins:",
            reply_markup=admin_panel_keyboard(),
        )

    @app.on_callback_query(filters.regex(r"^adm:home$"))
    async def cb_adm_home(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        await _clear_login_handle(user.id)
        admin_states.pop(user.id, None)
        await send_or_edit(
            client,
            cq,
            "Here is the button for admins:",
            reply_markup=admin_panel_keyboard(),
        )

    # ---------------- STOCK BUTTON FLOW ----------------
    @app.on_callback_query(filters.regex(r"^adm:stock$"))
    async def cb_adm_stock(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Server 1", callback_data="adm:stk_srv:server_1"),
                    InlineKeyboardButton("Server 2", callback_data="adm:stk_srv:server_2"),
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data="adm:home")],
            ]
        )
        await send_or_edit(client, cq, "Select the server", reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^adm:stk_srv:(server_1|server_2)$"))
    async def cb_adm_stock_server(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        server_key = cq.matches[0].group(1)
        items = await stock_service.list_admin_server_countries(server_key)

        rows: list[list[InlineKeyboardButton]] = []
        for item in items:
            price_txt = f"₹{item['price']}" if item["price"] is not None else "No Price"
            label = f"{item['flag']} {item['country_name']} | {price_txt} ({item['available']})"
            rows.append(
                [
                    InlineKeyboardButton(
                        label,
                        callback_data=f"adm:stk_cty:{server_key}:{item['country_code']}",
                    )
                ]
            )
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="adm:stock")])

        text = (
            f"📦 **{SERVERS[server_key]} — Countries**\n\n"
            + ("No countries in stock yet." if not items else "Select a country button below:")
        )
        await send_or_edit(client, cq, text, reply_markup=InlineKeyboardMarkup(rows))

    @app.on_callback_query(filters.regex(r"^adm:stk_cty:(server_1|server_2):(\+[0-9]+)$"))
    async def cb_adm_country_detail(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        server_key = cq.matches[0].group(1)
        country_code = cq.matches[0].group(2)
        admin_states.pop(user.id, None)
        items = await stock_service.list_admin_server_countries(server_key)
        entry = next((x for x in items if x["country_code"] == country_code), None)

        c_name = entry["country_name"] if entry else country_code
        c_flag = entry["flag"] if entry else "🌍"
        avail = entry["available"] if entry else 0
        locked = entry["locked"] if entry else 0
        sold = entry["sold"] if entry else 0
        price_val = entry["price"] if entry and entry["price"] is not None else "Not Set"

        text = (
            "🌍 **Country Stock Detail**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 **Server:** {SERVERS[server_key]}\n"
            f"🏳️ **Country:** {c_flag} {c_name}\n"
            f"📞 **Country Code:** `{country_code}`\n"
            f"💰 **Price:** ₹{price_val if price_val != 'Not Set' else price_val}\n"
            f"📦 **Available Stock:** {avail} *(Locked: {locked}, Sold: {sold})*\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📄 List of Number",
                        callback_data=f"adm:nums:{server_key}:{country_code}",
                    ),
                    InlineKeyboardButton(
                        "💲 Change Price",
                        callback_data=f"adm:chprice:{server_key}:{country_code}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🗑 Remove Country Stock",
                        callback_data=f"adm:rmcty:{server_key}:{country_code}",
                    )
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"adm:stk_srv:{server_key}")],
            ]
        )
        await send_or_edit(client, cq, text, reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^adm:rmcty:(server_1|server_2):(\+[0-9]+)$"))
    async def cb_adm_remove_country_confirm(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        server_key = cq.matches[0].group(1)
        country_code = cq.matches[0].group(2)
        items = await stock_service.list_admin_server_countries(server_key)
        entry = next((x for x in items if x["country_code"] == country_code), None)
        if entry:
            label = f"{entry['flag']} {entry['country_name']} (`{country_code}`)"
            counts = f"Available: {entry['available']}, Locked: {entry['locked']}, Sold: {entry['sold']}"
        else:
            label = f"`{country_code}`"
            counts = "No stock found."
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Yes, Remove All",
                        callback_data=f"adm:rmctyok:{server_key}:{country_code}",
                    )
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"adm:stk_cty:{server_key}:{country_code}")],
            ]
        )
        await send_or_edit(
            client,
            cq,
            f"🗑 **Remove Country Stock**\n━━━━━━━━━━━━━━━━━━━━\n"
            f"📡 **Server:** {SERVERS[server_key]}\n"
            f"🏳️ **Country:** {label}\n"
            f"📦 {counts}\n━━━━━━━━━━━━━━━━━━━━\n"
            "All unsold numbers of this country (and its price) will be deleted.\n"
            "Numbers that are reserved by a buyer or already sold are kept.\n\n"
            "Are you sure?",
            reply_markup=kb,
        )

    @app.on_callback_query(filters.regex(r"^adm:rmctyok:(server_1|server_2):(\+[0-9]+)$"))
    async def cb_adm_remove_country_do(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        server_key = cq.matches[0].group(1)
        country_code = cq.matches[0].group(2)
        res = await stock_service.remove_country_stock(server_key, country_code)
        extra = ""
        if res["locked"]:
            extra += f"\n🔒 {res['locked']} reserved number(s) kept (active orders)."
        if res["sold"]:
            extra += f"\n✅ {res['sold']} sold number(s) kept for records."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"adm:stk_srv:{server_key}")]])
        await send_or_edit(
            client,
            cq,
            f"✅ Removed **{res['removed']}** number(s) of `{country_code}` from **{SERVERS[server_key]}**.{extra}",
            reply_markup=kb,
        )

    @app.on_callback_query(filters.regex(r"^adm:nums:(server_1|server_2):(\+[0-9]+)$"))
    async def cb_adm_list_numbers(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        server_key = cq.matches[0].group(1)
        country_code = cq.matches[0].group(2)
        admin_states.pop(user.id, None)
        nums = await stock_service.get_country_numbers(server_key, country_code, limit=40)

        if not nums:
            body = "No numbers added for this country yet."
        else:
            lines = [f"📄 **Numbers ({country_code})**\n━━━━━━━━━━━━━━━━━━━━"]
            for i, doc in enumerate(nums, start=1):
                lines.append(f"{i}. `{doc['phone_number']}` — **{doc.get('status', 'available').upper()}**")
            body = "\n".join(lines)

        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🗑 Remove", callback_data=f"adm:rmnum:{server_key}:{country_code}")],
                [InlineKeyboardButton("⬅️ Back", callback_data=f"adm:stk_cty:{server_key}:{country_code}")],
            ]
        )
        await send_or_edit(client, cq, body, reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^adm:rmnum:(server_1|server_2):(\+[0-9]+)$"))
    async def cb_adm_remove_number_prompt(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        server_key = cq.matches[0].group(1)
        country_code = cq.matches[0].group(2)
        admin_states[user.id] = {
            "mode": "awaiting_remove_number",
            "server": server_key,
            "country_code": country_code,
        }
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=f"adm:nums:{server_key}:{country_code}")]]
        )
        await send_or_edit(
            client,
            cq,
            f"🗑 **Remove Number** (`{SERVERS[server_key]}` | `{country_code}`)\n\n"
            "Which number do you want to remove? Send the number with country code "
            f"(example: `{country_code}XXXXXXXXXX`):",
            reply_markup=kb,
        )

    @app.on_callback_query(filters.regex(r"^adm:chprice:(server_1|server_2):(\+[0-9]+)$"))
    async def cb_adm_change_price_prompt(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        server_key = cq.matches[0].group(1)
        country_code = cq.matches[0].group(2)
        admin_states[user.id] = {
            "mode": "awaiting_new_price",
            "server": server_key,
            "country_code": country_code,
        }
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Cancel", callback_data=f"adm:stk_cty:{server_key}:{country_code}")]]
        )
        await send_or_edit(
            client,
            cq,
            f"💲 **Change Price** (`{SERVERS[server_key]}` | `{country_code}`)\n\nPlease enter the new price (in ₹):",
            reply_markup=kb,
        )

    @app.on_callback_query(filters.regex(r"^adm:cfmprice:(server_1|server_2):(\+[0-9]+):([0-9]+)$"))
    async def cb_adm_confirm_price(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        server_key = cq.matches[0].group(1)
        country_code = cq.matches[0].group(2)
        new_price = int(cq.matches[0].group(3))
        info = detect_country(country_code)

        await stock_service.set_country_price(
            server=server_key,
            country_code=country_code,
            country_name=info.name,
            flag=info.flag,
            price=new_price,
        )
        admin_states.pop(user.id, None)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Country", callback_data=f"adm:stk_cty:{server_key}:{country_code}")]]
        )
        await send_or_edit(
            client,
            cq,
            f"✅ Price updated to **₹{new_price}** for {info.flag} {info.name} (`{country_code}`) on **{SERVERS[server_key]}**.",
            reply_markup=kb,
        )

    # ---------------- ADD STOCK BUTTON FLOW ----------------
    @app.on_callback_query(filters.regex(r"^adm:addstock$"))
    async def cb_adm_addstock(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        await _clear_login_handle(user.id)
        admin_states.pop(user.id, None)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Server 1", callback_data="adm:add_srv:server_1"),
                    InlineKeyboardButton("Server 2", callback_data="adm:add_srv:server_2"),
                ],
                [InlineKeyboardButton("⬅️ Back", callback_data="adm:home")],
            ]
        )
        await send_or_edit(client, cq, "➕ **Add Stock**\n\nSelect the target server:", reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^adm:add_srv:(server_1|server_2)$"))
    async def cb_adm_add_server(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        server_key = cq.matches[0].group(1)
        admin_states[user.id] = {
            "mode": "add_awaiting_country_code",
            "server": server_key,
        }
        await send_or_edit(
            client,
            cq,
            f"➕ **Add Stock — {SERVERS[server_key]}**\n\nStep 1: Send the **Country Code** (for example: `+91` or `+81`).",
            reply_markup=_add_cancel_kb(),
        )

    # ---------------- STATUS & HISTORY BUTTONS ----------------
    @app.on_callback_query(filters.regex(r"^adm:status$"))
    async def cb_adm_status(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        summary = await stock_service.get_status_summary()
        text = (
            "📈 **Bot & Stock Status**\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 **Total Users:** {summary['total_users']}\n"
            f"📦 **Total Accounts:** {summary['total_accounts']}\n"
            f"🟢 **Available:** {summary['available']}\n"
            f"🔒 **Locked (Active 20m):** {summary['locked']}\n"
            f"✅ **Sold Accounts:** {summary['sold']}\n"
            f"🧾 **Completed Orders:** {summary['completed_orders']}\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm:home")]])
        await send_or_edit(client, cq, text, reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^adm:history$"))
    async def cb_adm_history(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        orders = await stock_service.get_purchase_history(limit=15)
        if not orders:
            text = "🧾 **Purchase History**\n\nNo completed purchases recorded yet."
        else:
            lines = ["🧾 **Recent Completed Purchases**\n━━━━━━━━━━━━━━━━━━━━"]
            for i, o in enumerate(orders, start=1):
                lines.append(
                    f"{i}. User `{o.get('user_id')}` bought `{o.get('phone_number')}` "
                    f"({o.get('flag', '🌍')} {o.get('country_name', '')}) — ₹{o.get('price', 0)}"
                )
            text = "\n".join(lines)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="adm:home")]])
        await send_or_edit(client, cq, text, reply_markup=kb)

    # ---------------- ADMIN TEXT INPUT HANDLER ----------------
    @app.on_message(filters.private & filters.text & ~filters.command(["start", "admin"]))
    async def handle_admin_text_input(client: Client, message: Message) -> None:
        user = get_actor(message)
        if not user or not is_admin(user.id, config.admins):
            return
        st = admin_states.get(user.id)
        if not st:
            return

        mode = st.get("mode")
        raw_input = (message.text or "").strip()

        # 0. Remove one number from stock
        if mode == "awaiting_remove_number":
            phone = normalize_phone(raw_input)
            server_key = st["server"]
            country_code = st["country_code"]
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to List", callback_data=f"adm:nums:{server_key}:{country_code}")]]
            )
            if len(phone) < 6:
                await message.reply_text("Please send a valid phone number with country code.", reply_markup=kb)
                return
            ok, msg = await stock_service.remove_number(server_key, country_code, phone)
            admin_states.pop(user.id, None)
            await message.reply_text(f"{'✅' if ok else '⚠️'} {msg}", reply_markup=kb)
            return

        # 1. Confirm flow when changing existing country price
        if mode == "awaiting_new_price":
            cleaned_price = raw_input.lstrip("₹").strip()
            if not cleaned_price.isdigit() or int(cleaned_price) <= 0:
                await message.reply_text("Please enter a valid positive number for price (e.g. `60`).")
                return
            new_price = int(cleaned_price)
            server_key = st["server"]
            country_code = st["country_code"]
            info = detect_country(country_code)
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"✅ Confirm ₹{new_price}",
                            callback_data=f"adm:cfmprice:{server_key}:{country_code}:{new_price}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ Cancel",
                            callback_data=f"adm:stk_cty:{server_key}:{country_code}",
                        )
                    ],
                ]
            )
            await message.reply_text(
                f"Confirm new price **₹{new_price}** for {info.flag} {info.name} (`{country_code}`) on **{SERVERS[server_key]}**?",
                reply_markup=kb,
            )
            return

        # 2. Add Stock Step 1: Country Code first
        if mode == "add_awaiting_country_code":
            dial = normalize_dial_code(raw_input)
            info: CountryInfo | None = lookup_country(dial)
            if info is None:
                await message.reply_text(
                    f"❌ Country code `{dial or raw_input}` is not a valid country code.\n"
                    "Send a real one, for example `+91` (India), `+94` (Sri Lanka) or `+1` (USA).",
                    reply_markup=_add_cancel_kb(),
                )
                return
            server_key = st["server"]
            existing_price = await stock_service.get_country_price(server_key, dial)

            if existing_price is not None:
                st["country_code"] = dial
                st["country_info"] = info
                st["mode"] = "add_awaiting_phone"
                await message.reply_text(
                    f"🏳️ Country: {info.flag} **{info.name}** (`{dial}`)\n"
                    f"💰 Price already set: **₹{existing_price}**\n\n"
                    "Now send the **Phone Number** with country code (e.g. `+819012345678`)\n\n— OR paste a **Telethon String Session** to add the account directly (no OTP):",
                    reply_markup=_add_cancel_kb(),
                )
            else:
                st["country_code"] = dial
                st["country_info"] = info
                st["mode"] = "add_awaiting_new_country_price"
                await message.reply_text(
                    f"🏳️ New Country Detected: {info.flag} **{info.name}** (`{dial}`)\n\n"
                    "Price is not set yet for this country. Send the **Price (in ₹)** first:",
                    reply_markup=_add_cancel_kb(),
                )
            return

        # 3. Add Stock Step 2 (if new country): Price before Phone Number
        if mode == "add_awaiting_new_country_price":
            cleaned_price = raw_input.lstrip("₹").strip()
            if not cleaned_price.isdigit() or int(cleaned_price) <= 0:
                await message.reply_text(
                    "Please enter a valid positive price in ₹ (e.g. `75`).",
                    reply_markup=_add_cancel_kb(),
                )
                return
            price_val = int(cleaned_price)
            info = st["country_info"]
            await stock_service.set_country_price(
                server=st["server"],
                country_code=st["country_code"],
                country_name=info.name,
                flag=info.flag,
                price=price_val,
            )
            st["mode"] = "add_awaiting_phone"
            await message.reply_text(
                f"✅ Price **₹{price_val}** saved for {info.flag} {info.name} (`{st['country_code']}`).\n\n"
                "Now send the **Phone Number** with country code\n\n— OR paste a **Telethon String Session** to add the account directly (no OTP):",
                reply_markup=_add_cancel_kb(),
            )
            return

        # 3b. Session added, account has 2FA -> ask for / verify its password
        if mode == "add_awaiting_session_2fa":
            try:
                await message.delete()
            except Exception:
                pass
            sess = st.get("pending_session")
            sess_phone = st.get("pending_phone")
            if not sess or not sess_phone:
                admin_states.pop(user.id, None)
                await message.reply_text("Session expired. Run `/admin` -> `Add Stock` again.")
                return
            two_fa = None
            if raw_input.strip().lower() != "skip":
                pw_ok, pw_msg = await session_service.verify_2fa_for_session(sess, raw_input.strip())
                if not pw_ok:
                    await message.reply_text(f"⚠️ {pw_msg}", reply_markup=_add_cancel_kb())
                    return
                two_fa = raw_input.strip()
            c_info = st["country_info"]
            added_ok, added_msg = await stock_service.add_account_stock(
                server=st["server"],
                country_code=st["country_code"],
                country_name=c_info.name,
                flag=c_info.flag,
                phone_number=sess_phone,
                session_string=sess,
                added_by=user.id,
                two_fa_password=two_fa,
            )
            admin_states.pop(user.id, None)
            await message.reply_text(
                f"{'✅' if added_ok else '⚠️'} {added_msg}",
                reply_markup=admin_panel_keyboard(),
            )
            return

        # 4. Add Stock Step 3: Phone Number -> Send Telegram Login Code
        if mode == "add_awaiting_phone":
            # Option B: admin pasted a Telethon string session instead of a phone number
            if len(raw_input) > 60 and " " not in raw_input:
                try:
                    await message.delete()
                except Exception:
                    pass
                ok_s, msg_s, sess_phone, has_2fa = await session_service.import_session(raw_input)
                if not ok_s or not sess_phone:
                    await message.reply_text(f"⚠️ {msg_s}", reply_markup=_add_cancel_kb())
                    return
                phone_ok, phone_err = validate_phone_for_country(sess_phone, st["country_code"])
                if not phone_ok:
                    await message.reply_text(
                        f"⚠️ Session belongs to `{sess_phone}`. {phone_err}", reply_markup=_add_cancel_kb()
                    )
                    return
                if has_2fa:
                    st["pending_session"] = raw_input.strip()
                    st["pending_phone"] = sess_phone
                    st["mode"] = "add_awaiting_session_2fa"
                    await message.reply_text(
                        f"✅ Session valid for `{sess_phone}`.\n"
                        "🔐 **2-Step Verification is ON** for this account.\n\n"
                        "Send the **2FA Password** (saved with the number, so the buyer can get it), "
                        "or send `skip` to add without it:",
                        reply_markup=_add_cancel_kb(),
                    )
                    return
                c_info = st["country_info"]
                added_ok, added_msg = await stock_service.add_account_stock(
                    server=st["server"],
                    country_code=st["country_code"],
                    country_name=c_info.name,
                    flag=c_info.flag,
                    phone_number=sess_phone,
                    session_string=raw_input.strip(),
                    added_by=user.id,
                )
                admin_states.pop(user.id, None)
                await message.reply_text(
                    f"{'✅' if added_ok else '⚠️'} {added_msg}",
                    reply_markup=admin_panel_keyboard(),
                )
                return
            phone = normalize_phone(raw_input)
            if len(phone) < 6:
                await message.reply_text(
                    "Please enter a valid phone number including country code.",
                    reply_markup=_add_cancel_kb(),
                )
                return
            phone_ok, phone_err = validate_phone_for_country(phone, st["country_code"])
            if not phone_ok:
                await message.reply_text(f"⚠️ {phone_err}", reply_markup=_add_cancel_kb())
                return
            ok, status_msg, handle = await session_service.start_login(user.id, phone)
            if not ok or not handle:
                await message.reply_text(f"⚠️ {status_msg}", reply_markup=_add_cancel_kb())
                return
            st["phone_number"] = phone
            st["handle"] = handle
            st["mode"] = "add_awaiting_otp"
            await message.reply_text(
                f"📲 Login code requested for `{phone}`.\n"
                f"{status_msg}\n\n"
                "Send the **OTP** here:",
                reply_markup=_add_cancel_kb(),
            )
            return

        # 5. Add Stock Step 4: OTP -> Sign in or ask 2FA
        if mode == "add_awaiting_otp":
            try:
                await message.delete()
            except Exception:
                pass
            handle: LoginSessionHandle | None = st.get("handle")
            if not handle:
                admin_states.pop(user.id, None)
                await message.reply_text("Login session expired. Run `/admin` -> `Add Stock` again.")
                return

            outcome, info_or_session = await session_service.submit_code(handle, raw_input)
            if outcome == "need_2fa":
                st["mode"] = "add_awaiting_2fa"
                await message.reply_text(
                    "🔐 **2-Step Verification** is enabled on this account.\n"
                    "Please send the **2FA Password** (it is saved with the number, so the buyer can get it):",
                    reply_markup=_add_cancel_kb(),
                )
                return
            if outcome == "error":
                await message.reply_text(f"⚠️ {info_or_session}", reply_markup=_add_cancel_kb())
                return

            # Logged in directly; generate/save session into stock
            session_str = info_or_session or ""
            c_info: CountryInfo = st["country_info"]
            added_ok, added_msg = await stock_service.add_account_stock(
                server=st["server"],
                country_code=st["country_code"],
                country_name=c_info.name,
                flag=c_info.flag,
                phone_number=st["phone_number"],
                session_string=session_str,
                added_by=user.id,
            )
            admin_states.pop(user.id, None)
            await message.reply_text(
                f"{'✅' if added_ok else '⚠️'} {added_msg}",
                reply_markup=admin_panel_keyboard(),
            )
            return

        # 6. Add Stock Step 5 (optional 2FA): Password -> Sign in & save session
        if mode == "add_awaiting_2fa":
            try:
                await message.delete()
            except Exception:
                pass
            handle = st.get("handle")
            if not handle:
                admin_states.pop(user.id, None)
                await message.reply_text("Login session expired. Run `/admin` -> `Add Stock` again.")
                return

            ok, result_str = await session_service.submit_2fa_password(handle, raw_input)
            if not ok:
                admin_states.pop(user.id, None)
                await message.reply_text(f"⚠️ {result_str}", reply_markup=admin_panel_keyboard())
                return

            c_info = st["country_info"]
            added_ok, added_msg = await stock_service.add_account_stock(
                server=st["server"],
                country_code=st["country_code"],
                country_name=c_info.name,
                flag=c_info.flag,
                phone_number=st["phone_number"],
                session_string=result_str,
                added_by=user.id,
                two_fa_password=raw_input.strip(),
            )
            admin_states.pop(user.id, None)
            await message.reply_text(
                f"{'✅' if added_ok else '⚠️'} {added_msg}",
                reply_markup=admin_panel_keyboard(),
            )
