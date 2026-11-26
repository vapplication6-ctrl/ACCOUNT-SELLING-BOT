import asyncio
from pyrogram import Client, filters
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup
from bot.config import Config, SERVERS
from bot.handlers.common import get_actor, is_admin, send_or_edit
from bot.services.health_service import HealthService
from bot.services.stock_service import StockService
from bot.ui import InlineKeyboardButton, LINE, bold

VIEWS = {
    "frozen": {
        "statuses": ["frozen", "deleted"],
        "title": "🧊 FROZEN / DELETED IDS",
        "empty": "No frozen or deleted IDs right now. 🎉",
    },
    "expired": {
        "statuses": ["expired"],
        "title": "⌛ EXPIRED SESSIONS",
        "empty": "No expired sessions right now. 🎉",
    },
}


def register_health_handlers(
    app: Client,
    stock_service: StockService,
    health: HealthService,
    config: Config,
) -> None:
    async def _show(client: Client, cq: CallbackQuery, view: str) -> None:
        cfg = VIEWS[view]
        accounts, total = await stock_service.list_dead_accounts(cfg["statuses"], limit=40)
        lines = [f"{bold(cfg['title'])}\n{LINE}"]
        if not accounts:
            lines.append(cfg["empty"])
        else:
            for i, a in enumerate(accounts, start=1):
                when = a["dead_at"].strftime("%d %b %H:%M UTC") if a.get("dead_at") else "-"
                tag = {"frozen": "Frozen", "deleted": "Deleted"}.get(a.get("status"), "")
                tag = f" • {tag}" if tag and view == "frozen" else ""
                lines.append(
                    f"{i}. `{a.get('phone_number', 'N/A')}` • {a.get('flag', '🌍')} {a.get('country_name', '')}"
                    f" • {SERVERS.get(a.get('server', ''), '')}{tag} • {when}"
                )
            if total > len(accounts):
                lines.append(f"…and {total - len(accounts)} more.")
            lines.append(f"{LINE}\nTotal: **{total}**")
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Scan Now", callback_data=f"adm:scan:{view}", style="primary")],
                [InlineKeyboardButton("🗑 Clear List", callback_data=f"adm:clr:{view}", style="danger")]
                if accounts
                else [InlineKeyboardButton("🏠 Admin Panel", callback_data="adm:home", style="primary")],
                [InlineKeyboardButton("⬅️ Back", callback_data="adm:home", style="danger")],
            ]
        )
        await send_or_edit(client, cq, "\n".join(lines), reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^adm:(frozen|expired)$"))
    async def cb_list(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        await _show(client, cq, cq.matches[0].group(1))

    @app.on_callback_query(filters.regex(r"^adm:scan:(frozen|expired)$"))
    async def cb_scan(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        if health.scanning:
            await cq.answer("A scan is already running. Please wait.", show_alert=True)
            return
        await cq.answer("Scan started. You will get a report when it finishes.", show_alert=True)
        asyncio.create_task(health.scan_once(report_to=user.id))

    @app.on_callback_query(filters.regex(r"^adm:clr:(frozen|expired)$"))
    async def cb_clear(client: Client, cq: CallbackQuery) -> None:
        user = get_actor(cq)
        if not user or not is_admin(user.id, config.admins):
            await cq.answer("Admin only.", show_alert=True)
            return
        view = cq.matches[0].group(1)
        removed = await stock_service.clear_dead_accounts(VIEWS[view]["statuses"])
        await cq.answer(f"Cleared {removed} entries.", show_alert=False)
        await _show(client, cq, view)
