from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .config import Config, hide_password, load_config
from .db import Database
from .handlers import admin, info, user
from .handlers.common import fmt_dt, location_name
from .shop import Shop
from .steelproxy import SteelProxyClient

log = logging.getLogger("proxy-shop")


class UserMiddleware(BaseMiddleware):
    """Регистрирует пользователя и отсекает заблокированных."""

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user:
            db: Database = data["db"]
            cfg: Config = data["cfg"]
            row = await db.get_user(tg_user.id)
            if row is None:
                await db.upsert_user(tg_user.id, tg_user.username, tg_user.first_name)
            elif row["banned"] and tg_user.id not in cfg.admin_ids:
                return None
        return await handler(event, data)


async def expiry_reminders(bot: Bot, db: Database, cfg: Config) -> None:
    """Раз в 30 минут напоминает о заказах, которые истекают в ближайшие сутки."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            rows = await db.orders_to_remind((now + timedelta(hours=24)).strftime(fmt),
                                             now.strftime(fmt))
            for o in rows:
                kb = InlineKeyboardBuilder()
                kb.button(text="🔄 Продлить",
                          callback_data=user.OrderCB(act="ext", id=o["id"]))
                try:
                    await bot.send_message(
                        o["user_id"],
                        f"⏰ Заказ №{o['sp_order_id']} "
                        f"({location_name(o['country_code'], o['location_title'])}, "
                        f"{o['quantity']} шт) истекает {fmt_dt(o['expires_at'], cfg)}.\n"
                        "Продлите, чтобы прокси продолжили работать с теми же данными.",
                        reply_markup=kb.as_markup())
                except Exception as exc:
                    log.info("reminder to %s failed: %s", o["user_id"], exc)
                await db.update_order(o["id"], reminded=1)
        except Exception:
            log.exception("expiry_reminders")
        await asyncio.sleep(1800)


def create_bot(cfg: Config) -> Bot:
    session = AiohttpSession(proxy=cfg.telegram_proxy) if cfg.telegram_proxy else None
    if cfg.telegram_proxy:
        log.info("Telegram — через прокси %s", hide_password(cfg.telegram_proxy))
    return Bot(cfg.bot_token, session=session,
               default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def build_dispatcher(db: Database, shop: Shop, cfg: Config) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage(), db=db, shop=shop, cfg=cfg)
    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())
    dp.include_router(admin.router)
    dp.include_router(info.router)
    dp.include_router(user.router)
    return dp


async def refresh_prices_page(bot: Bot, db: Database, shop: Shop, cfg: Config) -> None:
    """Раз в 6 часов обновляет опубликованную страницу цен на telegra.ph (если /publish делали)."""
    from . import docs, telegraph
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            if not await db.get_setting("telegraph_path_prices"):
                continue
            title, blocks = await info.build_doc("prices", bot, cfg, shop)
            ctx = await info.doc_context(bot, cfg)
            await telegraph.publish(db, telegraph.Telegraph(proxy=cfg.telegram_proxy),
                                    {"prices": (title, docs.to_telegraph(blocks))},
                                    author=cfg.shop_name,
                                    author_url=f"https://t.me/{ctx.bot_username}")
            log.info("Страница цен на telegra.ph обновлена")
        except Exception:
            log.exception("refresh_prices_page")


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    from .docs import DocContext
    missing = DocContext.from_config(cfg, "bot").missing()
    if missing:
        log.warning("Для документов (банк) не заполнено в .env: %s", "; ".join(missing))
    if not cfg.admin_ids:
        log.warning("ADMIN_IDS не задан — заявки на пополнение некому будет подтверждать!")

    db = Database(cfg.db_path)
    await db.connect()
    api = SteelProxyClient(cfg.api_key, cfg.api_base, proxy=cfg.steelproxy_proxy)
    if cfg.steelproxy_proxy:
        log.info("API Steel Proxy — через прокси %s", hide_password(cfg.steelproxy_proxy))
    shop = Shop(db, api, cfg.markup_percent)

    bot = create_bot(cfg)
    dp = build_dispatcher(db, shop, cfg)

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="buy", description="Купить прокси"),
        BotCommand(command="orders", description="Мои заказы"),
        BotCommand(command="balance", description="Баланс и пополнение"),
        BotCommand(command="prices", description="Цены и тарифы"),
        BotCommand(command="support", description="Поддержка"),
        BotCommand(command="terms", description="Пользовательское соглашение"),
        BotCommand(command="privacy", description="Политика конфиденциальности"),
    ])

    reminders = asyncio.create_task(expiry_reminders(bot, db, cfg))
    prices_task = asyncio.create_task(refresh_prices_page(bot, db, shop, cfg))
    try:
        log.info("Бот запущен")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        reminders.cancel()
        prices_task.cancel()
        await api.close()
        await db.close()
        await bot.session.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
