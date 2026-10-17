"""Все команды бота — единый список для /help, /admin и меню команд в Telegram."""
from __future__ import annotations

import html

# (команда, аргументы, описание)
USER_COMMANDS: list[tuple[str, str, str]] = [
    ("start", "", "Главное меню"),
    ("buy", "", "Купить прокси"),
    ("orders", "", "Мои заказы: данные прокси, продление"),
    ("balance", "", "Баланс и пополнение"),
    ("prices", "", "Цены и тарифы"),
    ("support", "", "Поддержка и обращения"),
    ("docs", "", "Документы"),
    ("terms", "", "Пользовательское соглашение"),
    ("privacy", "", "Политика конфиденциальности"),
    ("help", "", "Список команд"),
    ("cancel", "", "Отменить текущее действие"),
]

ADMIN_COMMANDS: list[tuple[str, str, str]] = [
    ("admin", "", "Админ-панель (этот список)"),
    ("stats", "", "Статистика продаж и прибыль"),
    ("user", "<id|@username>", "Карточка пользователя"),
    ("give", "<id|@username> <сумма>", "Начислить на баланс"),
    ("take", "<id|@username> <сумма>", "Списать с баланса"),
    ("ban", "<id|@username>", "Заблокировать пользователя"),
    ("unban", "<id|@username>", "Разблокировать пользователя"),
    ("deluser", "<id|@username>", "Удалить персональные данные по запросу"),
    ("tickets", "", "Открытые обращения в поддержку"),
    ("check", "<№ заказа>", "Сверить заказ после сбоя связи"),
    ("sporders", "", "Последние заказы в Steel Proxy"),
    ("reload", "", "Обновить каталог и цены из API"),
    ("publish", "", "Опубликовать документы и цены на telegra.ph"),
    ("broadcast", "", "Рассылка: ответьте этой командой на сообщение"),
]

ADMIN_NOTES = [
    "Ответ на обращение — реплай на сообщение с обращением.",
    "Заявки на пополнение подтверждаются кнопками под чеком.",
]


def _lines(commands: list[tuple[str, str, str]]) -> list[str]:
    out = []
    for cmd, args, desc in commands:
        a = f" {html.escape(args)}" if args else ""
        out.append(f"/{cmd}{a} — {html.escape(desc)}")
    return out


def help_text(is_admin: bool) -> str:
    parts = ["<b>Команды</b>", *_lines(USER_COMMANDS)]
    if is_admin:
        parts += ["", admin_text()]
    return "\n".join(parts)


def admin_text() -> str:
    return "\n".join(["<b>Команды администратора</b>", *_lines(ADMIN_COMMANDS), "",
                      *(f"• {html.escape(n)}" for n in ADMIN_NOTES)])


def menu(commands: list[tuple[str, str, str]]):
    from aiogram.types import BotCommand
    return [BotCommand(command=c, description=d[:256]) for c, _, d in commands
            if c not in ("cancel",)]
