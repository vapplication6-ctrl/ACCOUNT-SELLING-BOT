import asyncio
import logging
from pyrogram import Client
from bot.config import HEALTH_CHECK_ACCOUNT_DELAY_SECONDS, HEALTH_CHECK_INTERVAL_SECONDS
from bot.config import SERVERS
from bot.services.session_service import SessionService
from bot.services.stock_service import StockService
from bot.ui import LINE, bold

logger = logging.getLogger("tg_account_seller_bot.health")

_TITLES = {
    "frozen": ("🧊", "ACCOUNT FROZEN", "This number has been frozen by Telegram."),
    "deleted": ("🗑", "ACCOUNT DELETED", "This number was deleted or banned by Telegram."),
    "expired": ("⌛", "SESSION EXPIRED", "The saved session of this number is no longer valid."),
}


class HealthService:
    """Checks unsold stock sessions and alerts admins when one is frozen, deleted or expired."""

    def __init__(
        self,
        bot_client: Client,
        stock_service: StockService,
        session_service: SessionService,
        admins: set[int],
    ) -> None:
        self._bot = bot_client
        self._stock = stock_service
        self._sessions = session_service
        self._admins = admins
        self.scanning = False

    async def _alert(self, account: dict, kind: str) -> None:
        icon, title, line = _TITLES[kind]
        text = (
            f"{icon} {bold(title)}\n"
            f"{LINE}\n"
            f"📞 {bold('Number')} : `{account.get('phone_number', 'N/A')}`\n"
            f"🌐 {bold('Server')} : {SERVERS.get(account.get('server', ''), account.get('server', ''))}\n"
            f"🏳️ {bold('Country')} : {account.get('flag', '🌍')} {account.get('country_name', '')}\n"
            f"{LINE}\n"
            f"{line}\n"
            "It has been removed from the sale list, so users can no longer buy it."
        )
        for admin_id in self._admins:
            try:
                await self._bot.send_message(admin_id, text)
            except Exception:
                pass

    async def scan_once(self, report_to: int | None = None) -> dict[str, int] | None:
        if self.scanning:
            return None
        self.scanning = True
        stats = {"checked": 0, "frozen": 0, "deleted": 0, "expired": 0, "skipped": 0}
        try:
            accounts = await self._stock.list_scannable_accounts()
            for acc in accounts:
                try:
                    status, detail = await self._sessions.check_account_health(acc["session_string"])
                except Exception as exc:
                    logger.warning("Health check failed for %s: %s", acc.get("phone_number"), exc)
                    stats["skipped"] += 1
                    continue
                stats["checked"] += 1
                if status in ("frozen", "deleted", "expired"):
                    updated = await self._stock.mark_account_dead(acc["_id"], status, detail)
                    if updated:
                        stats[status] += 1
                        await self._alert(updated, status)
                elif status == "unknown":
                    stats["skipped"] += 1
                await asyncio.sleep(HEALTH_CHECK_ACCOUNT_DELAY_SECONDS)
        except Exception as exc:
            logger.exception("Health scan error: %s", exc)
        finally:
            self.scanning = False

        if report_to is not None:
            try:
                await self._bot.send_message(
                    report_to,
                    f"🔍 {bold('SCAN COMPLETE')}\n"
                    f"{LINE}\n"
                    f"✅ {bold('Checked')} : {stats['checked']}\n"
                    f"🧊 {bold('Frozen')} : {stats['frozen']}\n"
                    f"🗑 {bold('Deleted')} : {stats['deleted']}\n"
                    f"⌛ {bold('Expired')} : {stats['expired']}\n"
                    f"⏭ {bold('Skipped (network/flood)')} : {stats['skipped']}",
                )
            except Exception:
                pass
        return stats

    async def run_forever(self) -> None:
        await asyncio.sleep(120)  # let the bot settle after start
        while True:
            try:
                await self.scan_once()
            except Exception as exc:
                logger.exception("Periodic health scan failed: %s", exc)
            await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)
