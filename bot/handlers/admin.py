from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Config
from ..db import Database, fmt_rub, to_kop
from ..shop import Shop
from ..steelproxy import SteelProxyError
from .common import esc

log = logging.getLogger(__name__)
router = Router(name="admin")


class IsAdmin(BaseFilter):
    async def __call__(self, event: TelegramObject, cfg: Config) -> bool:
        user = getattr(event, "from_user", None)
        return bool(user and user.id in cfg.admin_ids)


router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class TopupDecisionCB(CallbackData, prefix="td"):
    act: str  # ok / no
    id: int


def topup_decision_kb(topup_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Зачислить", callback_data=TopupDecisionCB(act="ok", id=topup_id))
    kb.button(text="❌ Отклонить", callback_data=TopupDecisionCB(act="no", id=topup_id))
    return kb.as_markup()





@router.message(Command("admin"))
async def admin_help(message: Message):
    from ..commands import admin_text
    await message.answer(admin_text())


@router.message(Command("stats"))
async def stats(message: Message, db: Database, shop: Shop):
    s = await db.stats()
    xs = await db.xr_stats()
    await message.answer(
        "<b>Статистика</b>\n\n"
        f"Пользователей: {s['users']}\n"
        f"Сумма балансов покупателей: {fmt_rub(s['balances'])}\n\n"
        f"Заказов всего: {s['orders']}\n"
        f"Выручка: {fmt_rub(s['revenue'])}\n"
        f"Закупка: {fmt_rub(s['cost'])}\n"
        f"Прибыль: <b>{fmt_rub(s['revenue'] - s['cost'])}</b>\n\n"
        f"Сегодня (UTC): {s['today_orders']} заказ(ов) на {fmt_rub(s['today_revenue'])}\n"
        f"Пополнений через xRocket: {xs['count']} на {fmt_rub(xs['rub'])}\n"
        f"Баланс Steel Proxy (после последней операции): "
        f"{esc(shop.last_api_balance or 'неизвестен')} ₽")


async def _target(message: Message, db: Database, arg: str | None):
    if not arg:
        await message.answer("Укажите ID или @username пользователя.")
        return None
    user = await db.find_user(arg.split()[0])
    if not user:
        await message.answer("Пользователь не найден (он должен хотя бы раз нажать /start).")
    return user


@router.message(Command("user"))
async def user_card(message: Message, command: CommandObject, db: Database):
    u = await _target(message, db, command.args)
    if not u:
        return
    n = await db.count_user_orders(u["id"])
    await message.answer(
        f"<b>{esc(u['first_name'])}</b> (@{esc(u['username'] or '—')})\n"
        f"ID: <code>{u['id']}</code>\nБаланс: {fmt_rub(u['balance'])}\n"
        f"Заказов: {n}\nЗаблокирован: {'да' if u['banned'] else 'нет'}\n"
        f"С нами с: {esc(u['created_at'][:10])}")


async def _money(message: Message, command: CommandObject, db: Database, bot: Bot, sign: int):
    parts = (command.args or "").split()
    if len(parts) != 2:
        await message.answer(f"Формат: /{command.command} &lt;id|@username&gt; &lt;сумма&gt;")
        return
    u = await _target(message, db, parts[0])
    if not u:
        return
    try:
        amount = to_kop(Decimal(parts[1].replace(",", ".")))
    except InvalidOperation:
        await message.answer("Сумма должна быть числом.")
        return
    if amount <= 0:
        await message.answer("Сумма должна быть больше нуля.")
        return
    bal = await db.change_balance(u["id"], sign * amount, "admin",
                                  f"by {message.from_user.id}", allow_negative=True)
    await message.answer(f"Готово. Баланс {u['id']}: {fmt_rub(bal)}")
    if sign > 0:
        try:
            await bot.send_message(u["id"], f"💰 На ваш баланс зачислено {fmt_rub(amount)}.\n"
                                            f"Текущий баланс: <b>{fmt_rub(bal)}</b>")
        except Exception:
            pass


@router.message(Command("give"))
async def give(message: Message, command: CommandObject, db: Database, bot: Bot):
    await _money(message, command, db, bot, +1)


@router.message(Command("take"))
async def take(message: Message, command: CommandObject, db: Database, bot: Bot):
    await _money(message, command, db, bot, -1)


@router.message(Command("ban", "unban"))
async def ban(message: Message, command: CommandObject, db: Database):
    u = await _target(message, db, command.args)
    if not u:
        return
    await db.set_banned(u["id"], command.command == "ban")
    await message.answer("Заблокирован." if command.command == "ban" else "Разблокирован.")


@router.message(Command("check"))
async def check(message: Message, command: CommandObject, db: Database, shop: Shop, bot: Bot):
    arg = (command.args or "").strip().lstrip("л")
    if not arg.isdigit():
        await message.answer("Формат: /check &lt;номер локального заказа&gt;")
        return
    o = await db.get_order(int(arg))
    if not o:
        await message.answer("Заказ не найден.")
        return
    try:
        result = await shop.resolve_unknown(o["id"])
    except SteelProxyError as exc:
        await message.answer(f"API недоступен: {esc(exc)}")
        return
    if result == "active":
        o = await db.get_order(o["id"])
        await message.answer(f"Заказ найден у Steel Proxy (№{o['sp_order_id']}), выдан покупателю.")
        await bot.send_message(o["user_id"], f"✅ Заказ №{o['sp_order_id']} оформлен. "
                                             "Прокси — в разделе «📦 Мои заказы».")
    elif result == "refunded":
        await message.answer("У Steel Proxy такого заказа нет — деньги возвращены покупателю.")
        await bot.send_message(o["user_id"], f"↩️ Заказ л{o['id']} не был создан, "
                                             f"{fmt_rub(o['price'])} возвращены на баланс.")
    else:
        await message.answer(f"Статус заказа: {o['status']} — сверка не требуется.")


@router.message(Command("sporders"))
async def sp_orders(message: Message, shop: Shop):
    try:
        data = await shop.api.list_orders(page=1, page_size=15)
    except SteelProxyError as exc:
        await message.answer(f"Ошибка API: {esc(exc)}")
        return
    lines = [f"Всего заказов в Steel Proxy: {data.get('total')}\n"]
    for it in data.get("items", []):
        lines.append(f"№{it['order_id']} · {esc(it.get('status'))} · {esc(it.get('location'))} · "
                     f"{it.get('quantity')} шт · {it.get('term_days')} дн · {esc(it.get('price'))} ₽ · "
                     f"{esc(it.get('buyer_reference') or '')}")
    await message.answer("\n".join(lines))


@router.message(Command("reload"))
async def reload_cache(message: Message, shop: Shop):
    shop.invalidate_cache()
    try:
        await shop.proxy_types()
        await shop.locations("server")
        await message.answer("Кэш каталога и цен обновлён.")
    except SteelProxyError as exc:
        await message.answer(f"Ошибка API: {esc(exc)}")


@router.message(Command("broadcast"))
async def broadcast(message: Message, db: Database, bot: Bot):
    src = message.reply_to_message
    if not src:
        await message.answer("Ответьте командой /broadcast на сообщение, которое нужно разослать.")
        return
    ids = await db.all_user_ids()
    await message.answer(f"Рассылка на {len(ids)} пользователей началась…")
    ok = 0
    for uid in ids:
        try:
            await src.copy_to(uid)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)  # ~20 сообщений/с, в пределах лимитов Telegram
    await message.answer(f"Рассылка завершена: доставлено {ok} из {len(ids)}.")


# ============================ поддержка и документы ============================

class IsTicketReply(BaseFilter):
    """Админ ответил реплаем на сообщение, относящееся к обращению."""

    async def __call__(self, message: Message, db: Database) -> bool | dict:
        r = message.reply_to_message
        if not r or (message.text or "").startswith("/"):
            return False
        ticket = await db.ticket_by_message(message.chat.id, r.message_id)
        return {"ticket": ticket} if ticket else False


@router.message(IsTicketReply())
async def ticket_reply(message: Message, ticket, db: Database, bot: Bot):
    from .info import reply_to_user
    try:
        await reply_to_user(bot, db, ticket, message)
    except Exception as exc:
        await message.reply(f"Не удалось отправить: {esc(exc)}")
        return
    await message.reply(f"✅ Ответ по обращению #{ticket['id']} отправлен.")


@router.message(Command("tickets"))
async def tickets(message: Message, db: Database):
    rows = await db.open_tickets()
    if not rows:
        await message.answer("Открытых обращений нет.")
        return
    lines = ["<b>Открытые обращения</b> (ответьте реплаем на сообщение с обращением):"]
    for t in rows:
        u = await db.get_user(t["user_id"])
        who = f"@{u['username']}" if u and u["username"] else str(t["user_id"])
        lines.append(f"#{t['id']} · {esc(who)} · обновлено {esc(t['updated_at'][:16].replace('T', ' '))}")
    await message.answer("\n".join(lines))


@router.message(Command("publish"))
async def publish_docs(message: Message, db: Database, cfg: Config, shop: Shop, bot: Bot):
    from .. import docs, telegraph
    from .info import build_doc, doc_context
    ctx = await doc_context(bot, cfg)
    missing = ctx.missing()
    if missing:
        await message.answer("Сначала заполните в .env и перезапустите бота "
                             "(<code>docker compose up -d</code>):\n• " +
                             "\n• ".join(esc(m) for m in missing))
        return
    await message.answer("⏳ Публикую на telegra.ph…")
    try:
        pages = {}
        for name in ("terms", "privacy", "prices"):
            title, blocks = await build_doc(name, bot, cfg, shop)
            pages[name] = (title, docs.to_telegraph(blocks))
        urls = await telegraph.publish(db, telegraph.Telegraph(proxy=cfg.telegram_proxy), pages,
                                       author=cfg.shop_name, author_url=f"https://t.me/{ctx.bot_username}")
    except (telegraph.TelegraphError, SteelProxyError) as exc:
        await message.answer(f"❌ Не удалось опубликовать: {esc(exc)}")
        return
    await message.answer(
        "✅ Опубликовано. Ссылки для банка:\n\n"
        f"Пользовательское соглашение: {urls['terms']}\n"
        f"Политика конфиденциальности: {urls['privacy']}\n"
        f"Цены и тарифы: {urls['prices']}\n\n"
        "Ссылки постоянные. Страница цен обновляется сама раз в 6 часов; после правки "
        ".env запустите /publish ещё раз.", disable_web_page_preview=True)


@router.message(Command("deluser"))
async def deluser(message: Message, command: CommandObject, db: Database):
    u = await _target(message, db, command.args)
    if not u:
        return
    await db.forget_user(u["id"])
    await message.answer(f"Персональные данные пользователя <code>{u['id']}</code> удалены: "
                         "имя, @username и обращения. Заказы и платежи остались без имени.\n"
                         "Если он снова напишет боту, Telegram передаст имя заново — "
                         "чтобы этого не было, заблокируйте: /ban " + str(u["id"]))


@router.callback_query(TopupDecisionCB.filter())
async def topup_decide(call: CallbackQuery, callback_data: TopupDecisionCB, db: Database, bot: Bot):
    status = "approved" if callback_data.act == "ok" else "declined"
    t = await db.decide_topup(callback_data.id, status, call.from_user.id)
    if not t:
        await call.answer("Заявка уже обработана.", show_alert=True)
        await call.message.edit_reply_markup(reply_markup=None)
        return
    if status == "approved":
        bal = await db.change_balance(t["user_id"], t["amount"], "topup", f"topup#{t['id']}")
        note = f"✅ Зачислено {fmt_rub(t['amount'])} (админ {call.from_user.id})"
        user_text = (f"💰 Пополнение #{t['id']} на {fmt_rub(t['amount'])} зачислено.\n"
                     f"Баланс: <b>{fmt_rub(bal)}</b>")
    else:
        note = f"❌ Отклонено (админ {call.from_user.id})"
        user_text = (f"Заявка на пополнение #{t['id']} отклонена. "
                     "Если это ошибка — напишите в поддержку.")
    await call.message.edit_text(f"{call.message.html_text}\n\n{note}")
    await call.answer()
    try:
        await bot.send_message(t["user_id"], user_text)
    except Exception:
        pass
