import asyncio
import logging
from pyrogram import Client
from bot.commands import setup_bot_commands
from bot.config import EXPIRY_SWEEP_INTERVAL_SECONDS, load_config
from bot.database import Database
from bot.handlers.auto_payment_flow import register_auto_payment_handlers
from bot.handlers.admin_flow import register_admin_handlers
from bot.handlers.force_sub import register_force_sub_handlers
from bot.handlers.health_flow import register_health_handlers
from bot.handlers.payment_flow import register_payment_handlers
from bot.handlers.user_flow import register_user_handlers
from bot.services.fam_service import FamService
from bot.services.health_service import HealthService
from bot.services.session_service import SessionService
from bot.services.stock_service import StockService
from bot.ui import style_support

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# The UPI (Auto) verification API receives the Gmail App Password in the URL: never log request URLs.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger("tg_account_seller_bot")


async def expiry_worker(bot_client: Client, stock_service: StockService) -> None:
    while True:
        try:
            expired = await stock_service.expire_timed_out_orders()
            for order in expired:
                uid = order.get("user_id")
                phone = order.get("phone_number", "")
                if uid:
                    try:
                        await bot_client.send_message(
                            uid,
                            f"⏳ **Order Expired (20 Minutes)**\n\n"
                            f"Number `{phone}` was not completed within 20 minutes. "
                            f"Your order has been cancelled with **₹0** balance deduction and the number is back in stock.",
                        )
                    except Exception:
                        pass
        except Exception as exc:
            logger.exception("Error in order expiry sweep: %s", exc)
        await asyncio.sleep(EXPIRY_SWEEP_INTERVAL_SECONDS)


async def main() -> None:
    config = load_config()
    db = Database(uri=config.mongo_uri, db_name=config.db_name)
    await db.ensure_indexes()

    stock_service = StockService(db=db)
    session_service = SessionService(api_id=config.api_id, api_hash=config.api_hash)

    app = Client(
        name="seller_bot",
        api_id=config.api_id,
        api_hash=config.api_hash,
        bot_token=config.bot_token,
        in_memory=True,
    )

    register_force_sub_handlers(app, db, config)
    register_user_handlers(app, db, stock_service, session_service, config)
    register_admin_handlers(app, db, stock_service, session_service, config)
    register_payment_handlers(app, db, config)
    fam_service = FamService(
        upi=config.fam_upi,
        gmail=config.fam_gmail,
        apppass=config.fam_apppass,
        name=config.fam_name,
        api_base=config.fam_api_base,
    )
    register_auto_payment_handlers(app, db, config, fam_service)
    health_service = HealthService(app, stock_service, session_service, config.admins)
    register_health_handlers(app, stock_service, health_service, config)

    sweep_task: asyncio.Task | None = None
    health_task: asyncio.Task | None = None
    try:
        await app.start()
        await setup_bot_commands(app)
        logger.info("UPI (Auto) enabled: %s", fam_service.enabled)
        sweep_task = asyncio.create_task(expiry_worker(app, stock_service))
        health_task = asyncio.create_task(health_service.run_forever())
        logger.info("Bot started successfully. Coloured buttons supported: %s", style_support())
        await asyncio.Event().wait()
    finally:
        for task in (sweep_task, health_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        try:
            if app.is_connected:
                await app.stop()
        except Exception:
            pass
        await fam_service.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
