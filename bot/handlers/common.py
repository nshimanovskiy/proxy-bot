from __future__ import annotations

import html
import io
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from ..config import Config
from .. import locations
from ..shop import term_label

log = logging.getLogger(__name__)

BTN_BUY = "🛒 Купить прокси"
BTN_ORDERS = "📦 Мои заказы"
BTN_BALANCE = "💰 Баланс"
BTN_SUPPORT = "🆘 Поддержка"
BTN_PRICES = "💲 Цены"
BTN_DOCS = "📄 Документы"

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_BUY)],
              [KeyboardButton(text=BTN_ORDERS), KeyboardButton(text=BTN_BALANCE)],
              [KeyboardButton(text=BTN_PRICES), KeyboardButton(text=BTN_SUPPORT)],
              [KeyboardButton(text=BTN_DOCS)]],
    resize_keyboard=True,
)

PROXY_TYPE_TITLES = {"server": "Серверные", "residential": "Резидентские"}

# Если прокси больше этого числа — отдаём файлом, а не текстом.
INLINE_PROXY_LIMIT = 10


def esc(s) -> str:
    return html.escape(str(s or ""))


def fmt_dt(iso: str | None, cfg: Config) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    return dt.astimezone(cfg.tz).strftime("%d.%m.%Y %H:%M")


def location_name(country_code: str | None, title: str | None) -> str:
    return locations.display(country_code, title)


def proxies_text(proxies: list[dict]) -> str:
    lines = []
    for i, p in enumerate(proxies, 1):
        lines.append(
            f"<b>#{i}</b>  {esc(p.get('host'))}\n"
            f"Логин: <code>{esc(p.get('username'))}</code>  Пароль: <code>{esc(p.get('password'))}</code>\n"
            f"HTTP: <code>{esc(p.get('connection_string_http'))}</code>\n"
            f"SOCKS5: <code>{esc(p.get('connection_string_socks5'))}</code>"
        )
    return "\n\n".join(lines)


def proxies_file(order: dict) -> BufferedInputFile:
    buf = io.StringIO()
    proxies = order.get("proxies") or []
    buf.write("# HTTP (login:pass@host:port)\n")
    for p in proxies:
        buf.write(f"{p.get('connection_string_http')}\n")
    buf.write("\n# SOCKS5 (login:pass@host:port)\n")
    for p in proxies:
        buf.write(f"{p.get('connection_string_socks5')}\n")
    return BufferedInputFile(buf.getvalue().encode(),
                             filename=f"proxies_{order.get('order_id', 'order')}.txt")


async def send_proxies(bot: Bot, chat_id: int, order: dict, header: str,
                       markup: InlineKeyboardMarkup | None = None) -> None:
    proxies = order.get("proxies") or []
    if 0 < len(proxies) <= INLINE_PROXY_LIMIT:
        text = header + "\n\n" + proxies_text(proxies)
        if len(text) < 4000:
            await bot.send_message(chat_id, text, reply_markup=markup)
            return
    await bot.send_message(chat_id, header, reply_markup=markup)
    if proxies:
        await bot.send_document(chat_id, proxies_file(order),
                                caption="Список прокси одним файлом")


async def notify_admins(bot: Bot, cfg: Config, text: str, **kwargs) -> None:
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, text, **kwargs)
        except Exception as exc:  # админ мог не запускать бота
            log.warning("Не удалось уведомить админа %s: %s", admin_id, exc)


def term_name(days: int) -> str:
    return term_label(days)
