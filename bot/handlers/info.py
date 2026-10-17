"""Документы, цены и поддержка (обращения) — то, что требует банк для эквайринга."""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import docs
from ..config import Config
from ..db import Database
from ..shop import Shop
from ..steelproxy import SteelProxyError
from .common import BTN_DOCS, BTN_PRICES, BTN_SUPPORT, MAIN_KB, esc

log = logging.getLogger(__name__)
router = Router(name="info")

DOC_TITLES = {"terms": "📄 Пользовательское соглашение",
              "privacy": "🔒 Политика конфиденциальности",
              "prices": "💲 Цены и тарифы"}


class DocCB(CallbackData, prefix="doc"):
    name: str


class SupportCB(CallbackData, prefix="sup"):
    act: str           # new / reply
    id: int = 0


class SupportStates(StatesGroup):
    message = State()


async def doc_context(bot: Bot, cfg: Config) -> docs.DocContext:
    me = await bot.me()
    return docs.DocContext.from_config(cfg, me.username or "bot")


async def build_doc(name: str, bot: Bot, cfg: Config, shop: Shop):
    ctx = await doc_context(bot, cfg)
    if name == "terms":
        return docs.terms(ctx)
    if name == "privacy":
        return docs.privacy(ctx)
    return await docs.prices(shop, ctx, cfg.tz)


def docs_kb(exclude: str | None = None):
    kb = InlineKeyboardBuilder()
    for name, title in DOC_TITLES.items():
        if name != exclude:
            kb.button(text=title, callback_data=DocCB(name=name))
    kb.adjust(1)
    return kb.as_markup()


async def send_doc(name: str, chat_id: int, bot: Bot, cfg: Config, shop: Shop, db: Database):
    try:
        title, blocks = await build_doc(name, bot, cfg, shop)
    except SteelProxyError as exc:
        log.error("prices: %s", exc)
        await bot.send_message(chat_id, "Не удалось получить цены, попробуйте через минуту.")
        return
    url = await db.get_setting(f"telegraph_url_{name}")
    footer = f"Эта страница в интернете: {esc(url)}" if url else ""
    chunks = docs.to_html_chunks(title, blocks, footer)
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        await bot.send_message(chat_id, chunk, disable_web_page_preview=True,
                               reply_markup=docs_kb(exclude=name) if last else None)


@router.message(F.text == BTN_DOCS)
@router.message(Command("docs"))
async def docs_menu(message: Message, db: Database):
    lines = ["<b>Документы</b>\n",
             "Пользуясь ботом, вы принимаете Пользовательское соглашение и Политику "
             "конфиденциальности."]
    links = [(DOC_TITLES[n], await db.get_setting(f"telegraph_url_{n}")) for n in DOC_TITLES]
    if any(u for _, u in links):
        lines.append("")
        lines += [f"{t}: {esc(u)}" for t, u in links if u]
    await message.answer("\n".join(lines), reply_markup=docs_kb(),
                         disable_web_page_preview=True)


@router.message(Command("terms"))
async def cmd_terms(message: Message, bot: Bot, cfg: Config, shop: Shop, db: Database):
    await send_doc("terms", message.chat.id, bot, cfg, shop, db)


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, bot: Bot, cfg: Config, shop: Shop, db: Database):
    await send_doc("privacy", message.chat.id, bot, cfg, shop, db)


@router.message(F.text == BTN_PRICES)
@router.message(Command("prices"))
async def cmd_prices(message: Message, bot: Bot, cfg: Config, shop: Shop, db: Database):
    await send_doc("prices", message.chat.id, bot, cfg, shop, db)


@router.callback_query(DocCB.filter())
async def doc_button(call: CallbackQuery, callback_data: DocCB, bot: Bot, cfg: Config,
                     shop: Shop, db: Database):
    await call.answer()
    if callback_data.name in DOC_TITLES:
        await send_doc(callback_data.name, call.from_user.id, bot, cfg, shop, db)


# ============================ поддержка ============================

def support_text(cfg: Config) -> str:
    lines = ["<b>🆘 Поддержка</b>\n"]
    if cfg.support_contact:
        lines.append(f"Telegram: {esc(cfg.support_contact)}")
    if cfg.support_email:
        lines.append(f"E-mail: {esc(cfg.support_email)}")
    lines += ["",
              "Или создайте обращение прямо здесь — ответ придёт в этот чат. "
              "Если вопрос по заказу, укажите его номер."]
    return "\n".join(lines)


@router.message(F.text == BTN_SUPPORT)
@router.message(Command("support"))
async def support(message: Message, cfg: Config, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Написать в поддержку", callback_data=SupportCB(act="new"))
    kb.button(text="📄 Документы", callback_data=DocCB(name="terms"))
    kb.adjust(1)
    await message.answer(support_text(cfg), reply_markup=kb.as_markup())


@router.callback_query(SupportCB.filter(F.act.in_({"new", "reply"})))
async def support_start(call: CallbackQuery, callback_data: SupportCB, db: Database,
                        state: FSMContext):
    ticket_id = 0
    if callback_data.act == "reply":
        t = await db.get_ticket(callback_data.id)
        if not t or t["user_id"] != call.from_user.id:
            await call.answer("Обращение не найдено.", show_alert=True)
            return
        ticket_id = t["id"]
    await state.set_state(SupportStates.message)
    await state.update_data(ticket_id=ticket_id)
    await call.message.answer(
        ("Напишите ответ" if ticket_id else "Опишите вопрос") +
        " одним сообщением — можно приложить скриншот или файл. /cancel — отмена.")
    await call.answer()


@router.message(SupportStates.message, ~F.text.startswith("/"))
async def support_message(message: Message, db: Database, cfg: Config, state: FSMContext,
                          bot: Bot):
    data = await state.get_data()
    await state.clear()
    ticket_id = data.get("ticket_id") or await db.create_ticket(message.from_user.id)
    await db.touch_ticket(ticket_id, "open")
    u = message.from_user
    header = (f"🆘 <b>Обращение #{ticket_id}</b>" +
              ("" if not data.get("ticket_id") else " — новое сообщение") +
              f"\n{esc(u.full_name)} (@{esc(u.username) if u.username else '—'}, "
              f"<code>{u.id}</code>)\n\n<i>Ответьте реплаем на это или следующее сообщение — "
              "ответ уйдёт пользователю.</i>")
    delivered = 0
    for admin_id in cfg.admin_ids:
        try:
            h = await bot.send_message(admin_id, header)
            c = await message.copy_to(admin_id)
            await db.link_ticket_message(admin_id, h.message_id, ticket_id)
            await db.link_ticket_message(admin_id, c.message_id, ticket_id)
            delivered += 1
        except Exception as exc:
            log.warning("support → admin %s: %s", admin_id, exc)
    if not delivered:
        await message.answer("Не удалось передать обращение. Напишите, пожалуйста, напрямую: "
                             f"{esc(cfg.support_contact or cfg.support_email)}",
                             reply_markup=MAIN_KB)
        return
    await message.answer(f"✅ Обращение #{ticket_id} принято. Ответ придёт в этот чат, "
                         "обычно в течение суток.", reply_markup=MAIN_KB)


async def reply_to_user(bot: Bot, db: Database, ticket, admin_message: Message) -> None:
    """Пересылает ответ админа пользователю (вызывается из админ-роутера)."""
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Ответить", callback_data=SupportCB(act="reply", id=ticket["id"]))
    await bot.send_message(ticket["user_id"],
                           f"💬 <b>Ответ поддержки</b> · обращение #{ticket['id']}")
    await admin_message.copy_to(ticket["user_id"], reply_markup=kb.as_markup())
    await db.touch_ticket(ticket["id"], "answered")
