from __future__ import annotations

import logging
import math
import time

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import locations
from ..config import Config
from ..db import Database, fmt_rub, to_kop
from ..shop import Shop, ShopError
from ..steelproxy import EXTEND_MONTHS, SteelProxyError, SteelProxyUnknownResult
from .common import (BTN_BALANCE, BTN_BUY, BTN_ORDERS, MAIN_KB,
                     PROXY_TYPE_TITLES, esc, fmt_dt, location_name, notify_admins,
                     send_proxies, term_name)

log = logging.getLogger(__name__)
router = Router(name="user")

QTY_PRESETS = (1, 2, 3, 5, 10, 20)
MAX_QTY = 1000
ORDERS_PAGE = 8


class BuyCB(CallbackData, prefix="b"):
    step: str            # types / type / loc / term / qty / pay
    t: str = ""          # proxy_type
    n: int = 0           # node_id
    d: int = 0           # term_days
    q: int = 0           # quantity


class OrderCB(CallbackData, prefix="o"):
    act: str             # list / view / file / ext / extok
    id: int = 0          # локальный id заказа
    p: int = 0           # страница / месяцы


class TopupCB(CallbackData, prefix="tu"):
    act: str             # start / svc / amount / xr / xrcheck
    a: int = 0           # сумма, ₽
    c: str = ""          # способ (xrocket / manual) или валюта xRocket


class BuyStates(StatesGroup):
    custom_qty = State()


class TopupStates(StatesGroup):
    amount = State()
    receipt = State()


def btn(text: str, cb: CallbackData) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


# ============================ старт и меню ============================

@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, cfg: Config, state: FSMContext):
    await state.clear()
    u = message.from_user
    await db.upsert_user(u.id, u.username, u.first_name)
    await message.answer(
        f"👋 Добро пожаловать в <b>{esc(cfg.shop_name)}</b>!\n\n"
        "Здесь можно купить серверные и резидентские прокси — выдача мгновенная, "
        "сразу после оплаты. Пополните баланс и выберите локацию.\n\n"
        "Цены — /prices, поддержка — /support, все команды — /help.\n"
        "Пользуясь ботом, вы принимаете Пользовательское соглашение (/terms) "
        "и Политику конфиденциальности (/privacy).",
        reply_markup=MAIN_KB,
    )


@router.message(Command("help"))
async def cmd_help(message: Message, cfg: Config):
    from ..commands import help_text
    await message.answer(help_text(message.from_user.id in cfg.admin_ids))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=MAIN_KB)


# ============================ покупка ============================

def _types_kb(types: list[dict]):
    kb = InlineKeyboardBuilder()
    for t in types:
        title = t.get("title") or PROXY_TYPE_TITLES.get(t["code"], t["code"])
        kb.button(text=title, callback_data=BuyCB(step="type", t=t["code"]))
    kb.adjust(1)
    return kb.as_markup()


@router.message(F.text == BTN_BUY)
@router.message(Command("buy"))
async def buy_start(message: Message, shop: Shop, state: FSMContext):
    await state.clear()
    try:
        types = await shop.proxy_types()
    except SteelProxyError as exc:
        log.error("proxy_types: %s", exc)
        await message.answer(exc.customer_message)
        return
    await message.answer("Выберите тип прокси:", reply_markup=_types_kb(types))


@router.callback_query(BuyCB.filter(F.step == "types"))
async def buy_types(call: CallbackQuery, shop: Shop):
    types = await shop.proxy_types()
    await call.message.edit_text("Выберите тип прокси:", reply_markup=_types_kb(types))
    await call.answer()


@router.callback_query(BuyCB.filter(F.step == "type"))
async def buy_type(call: CallbackQuery, callback_data: BuyCB, shop: Shop):
    locs = await shop.locations(callback_data.t)
    available = []
    for loc in locs:
        prices = [p for d in loc.term_options
                  if (p := await shop.unit_sell_price(loc.proxy_type, loc.node_id, d)) is not None]
        if prices:
            available.append((loc, min(prices)))
    names = locations.display_many([(loc.country_code, loc.title) for loc, _ in available])
    kb = InlineKeyboardBuilder()
    shown = len(available)
    for name, (loc, price) in sorted(zip(names, available), key=lambda x: (x[0].split(" ", 1)[-1], x[1][1])):
        kb.button(text=f"{name} · от {fmt_rub(price)}",
                  callback_data=BuyCB(step="loc", t=loc.proxy_type, n=loc.node_id))
    kb.adjust(1)
    kb.row(btn("« Назад", BuyCB(step="types")))
    type_title = PROXY_TYPE_TITLES.get(callback_data.t, callback_data.t)
    text = (f"<b>{esc(type_title)}</b>\nВыберите локацию:" if shown
            else f"<b>{esc(type_title)}</b>\nСейчас нет доступных локаций.")
    await call.message.edit_text(text, reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(BuyCB.filter(F.step == "loc"))
async def buy_loc(call: CallbackQuery, callback_data: BuyCB, shop: Shop):
    loc = await shop.location(callback_data.t, callback_data.n)
    if not loc:
        await call.answer("Товар недоступен.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for d in loc.term_options:
        price = await shop.unit_sell_price(loc.proxy_type, loc.node_id, d)
        if price is None:
            continue
        kb.button(text=f"{term_name(d)} — {fmt_rub(price)}",
                  callback_data=BuyCB(step="term", t=loc.proxy_type, n=loc.node_id, d=d))
    kb.adjust(1)
    kb.row(btn("« Назад", BuyCB(step="type", t=loc.proxy_type)))
    await call.message.edit_text(
        f"{location_name(loc.country_code, loc.title)}\n"
        f"Тип: {esc(PROXY_TYPE_TITLES.get(loc.proxy_type, loc.proxy_type))}\n\n"
        "Выберите срок (цена за 1 прокси):",
        reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(BuyCB.filter(F.step == "term"))
async def buy_term(call: CallbackQuery, callback_data: BuyCB, shop: Shop, state: FSMContext):
    c = callback_data
    price = await shop.unit_sell_price(c.t, c.n, c.d)
    if price is None:
        await call.answer("Товар недоступен.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for q in QTY_PRESETS:
        kb.button(text=f"{q} шт · {fmt_rub(price * q)}",
                  callback_data=BuyCB(step="qty", t=c.t, n=c.n, d=c.d, q=q))
    kb.adjust(2)
    kb.row(btn("✏️ Другое количество", BuyCB(step="custom", t=c.t, n=c.n, d=c.d)))
    kb.row(btn("« Назад", BuyCB(step="loc", t=c.t, n=c.n)))
    await state.clear()
    await call.message.edit_text(f"Срок: <b>{term_name(c.d)}</b>, {fmt_rub(price)} за штуку.\n"
                                 "Сколько прокси нужно?", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(BuyCB.filter(F.step == "custom"))
async def buy_custom(call: CallbackQuery, callback_data: BuyCB, state: FSMContext):
    await state.set_state(BuyStates.custom_qty)
    await state.update_data(t=callback_data.t, n=callback_data.n, d=callback_data.d)
    await call.message.answer(f"Введите количество числом (1–{MAX_QTY}). /cancel — отмена.")
    await call.answer()


@router.message(BuyStates.custom_qty)
async def buy_custom_qty(message: Message, state: FSMContext, shop: Shop, db: Database):
    text = (message.text or "").strip()
    if not text.isdigit() or not 1 <= int(text) <= MAX_QTY:
        await message.answer(f"Нужно целое число от 1 до {MAX_QTY}.")
        return
    data = await state.get_data()
    await state.clear()
    cb = BuyCB(step="qty", t=data["t"], n=data["n"], d=data["d"], q=int(text))
    text, kb = await _confirm_view(cb, shop, db, message.from_user.id)
    await message.answer(text, reply_markup=kb)


async def _confirm_view(c: BuyCB, shop: Shop, db: Database, user_id: int):
    loc = await shop.location(c.t, c.n)
    unit = await shop.unit_sell_price(c.t, c.n, c.d)
    if not loc or unit is None:
        return "Товар недоступен.", None
    total = unit * c.q
    user = await db.get_user(user_id)
    balance = user["balance"] if user else 0
    kb = InlineKeyboardBuilder()
    if balance >= total:
        kb.button(text=f"✅ Оплатить {fmt_rub(total)}",
                  callback_data=BuyCB(step="pay", t=c.t, n=c.n, d=c.d, q=c.q))
    else:
        kb.button(text="💳 Пополнить баланс",
                  callback_data=TopupCB(act="start", a=math.ceil((total - balance) / 100)))
    kb.button(text="« Назад", callback_data=BuyCB(step="term", t=c.t, n=c.n, d=c.d))
    kb.adjust(1)
    text = (
        "<b>Проверьте заказ</b>\n\n"
        f"Тип: {esc(PROXY_TYPE_TITLES.get(c.t, c.t))}\n"
        f"Локация: {location_name(loc.country_code, loc.title)}\n"
        f"Срок: {term_name(c.d)}\n"
        f"Количество: {c.q} шт\n"
        f"Итого: <b>{fmt_rub(total)}</b>\n\n"
        f"Ваш баланс: {fmt_rub(balance)}"
    )
    if balance < total:
        text += f"\n\n⚠️ Не хватает {fmt_rub(total - balance)}."
    return text, kb.as_markup()


@router.callback_query(BuyCB.filter(F.step == "qty"))
async def buy_qty(call: CallbackQuery, callback_data: BuyCB, shop: Shop, db: Database):
    text, kb = await _confirm_view(callback_data, shop, db, call.from_user.id)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


@router.callback_query(BuyCB.filter(F.step == "pay"))
async def buy_pay(call: CallbackQuery, callback_data: BuyCB, shop: Shop, db: Database,
                  cfg: Config, bot: Bot):
    c = callback_data
    lock = shop.user_lock(call.from_user.id)
    if lock.locked():
        await call.answer("Заказ уже обрабатывается…")
        return
    async with lock:
        await call.answer()
        loc = await shop.location(c.t, c.n)
        if not loc:
            await call.message.edit_text("Товар недоступен.")
            return
        await call.message.edit_text("⏳ Создаём заказ, это займёт несколько секунд…")
        try:
            res = await shop.purchase(call.from_user.id, loc, c.d, c.q)
        except ShopError as exc:
            await call.message.edit_text(f"❌ {esc(exc)}")
            if exc.status == 402:
                await notify_admins(bot, cfg, "🔴 <b>Недостаточно средств на балансе Steel Proxy</b> — "
                                              "покупатель не смог купить прокси. Пополните баланс в Ф-боте.")
            elif exc.status in (403, 401):
                await notify_admins(bot, cfg, f"🔴 API Steel Proxy вернул {exc.status}: "
                                              "проверьте ключ, подписку и статус аккаунта.")
            return

    if res.pending_check:
        await call.message.edit_text(
            f"⚠️ Связь с поставщиком прервалась во время оформления заказа №{res.local_id}.\n"
            "Деньги зарезервированы. Мы проверим заказ и выдадим прокси или вернём средства — "
            f"обычно в течение нескольких минут. Поддержка: {esc(cfg.support_contact)}")
        await notify_admins(bot, cfg, f"⚠️ Заказ №{res.local_id} в статусе unknown (сбой сети). "
                                      f"Проверьте: /check {res.local_id}")
        return

    order = res.order
    await call.message.delete()
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Открыть заказ", callback_data=OrderCB(act="view", id=res.local_id))
    header = (f"✅ <b>Заказ №{order['order_id']} оплачен</b>\n"
              f"{location_name(loc.country_code, loc.title)} · {term_name(c.d)} · {c.q} шт\n"
              f"Списано: {fmt_rub(res.charged)} · баланс: {fmt_rub(res.balance_after)}\n"
              f"Действует до: {fmt_dt(order.get('expires_at'), cfg)}")
    await send_proxies(bot, call.from_user.id, order, header, kb.as_markup())

    u = call.from_user
    profit = res.charged - to_kop(order.get("price", 0))
    await notify_admins(
        bot, cfg,
        f"💸 Продажа №{order['order_id']}: {esc(u.full_name)} "
        f"(@{esc(u.username) if u.username else u.id})\n"
        f"{location_name(loc.country_code, loc.title)} · {term_name(c.d)} · {c.q} шт\n"
        f"Выручка {fmt_rub(res.charged)}, прибыль {fmt_rub(profit)}\n"
        f"Баланс Steel Proxy: {esc(order.get('balance_after'))} {esc(order.get('currency', 'RUB'))}")
    try:
        if to_kop(order.get("balance_after", 0)) < to_kop(cfg.low_balance_rub):
            await notify_admins(bot, cfg, f"🟠 Баланс Steel Proxy ниже порога: "
                                          f"{esc(order.get('balance_after'))} ₽")
    except Exception:
        pass


# ============================ мои заказы ============================

async def _orders_list(user_id: int, page: int, db: Database, cfg: Config):
    total = await db.count_user_orders(user_id)
    if not total:
        return "У вас пока нет заказов. Нажмите «🛒 Купить прокси».", None
    pages = math.ceil(total / ORDERS_PAGE)
    page = max(0, min(page, pages - 1))
    rows = await db.user_orders(user_id, ORDERS_PAGE, page * ORDERS_PAGE)
    kb = InlineKeyboardBuilder()
    for o in rows:
        mark = {"active": "🟢", "unknown": "🟡", "refunded": "↩️"}.get(o["status"], "")
        num = o["sp_order_id"] or f"л{o['id']}"
        kb.button(text=f"{mark} №{num} · {o['country_code'] or ''} · {o['quantity']} шт · "
                       f"до {fmt_dt(o['expires_at'], cfg)[:10]}",
                  callback_data=OrderCB(act="view", id=o["id"]))
    kb.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="‹", callback_data=OrderCB(act="list", p=page - 1))
    if page < pages - 1:
        nav.button(text="›", callback_data=OrderCB(act="list", p=page + 1))
    if list(nav.buttons):
        kb.row(*nav.buttons)
    return f"Ваши заказы (стр. {page + 1}/{pages}):", kb.as_markup()


@router.message(F.text == BTN_ORDERS)
@router.message(Command("orders"))
async def orders_cmd(message: Message, db: Database, cfg: Config):
    text, kb = await _orders_list(message.from_user.id, 0, db, cfg)
    await message.answer(text, reply_markup=kb)


@router.callback_query(OrderCB.filter(F.act == "list"))
async def orders_page(call: CallbackQuery, callback_data: OrderCB, db: Database, cfg: Config):
    text, kb = await _orders_list(call.from_user.id, callback_data.p, db, cfg)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()


async def _own_order(call: CallbackQuery, db: Database, local_id: int):
    o = await db.get_order(local_id)
    if not o or o["user_id"] != call.from_user.id:
        await call.answer("Заказ не найден.", show_alert=True)
        return None
    return o


@router.callback_query(OrderCB.filter(F.act == "view"))
async def order_view(call: CallbackQuery, callback_data: OrderCB, db: Database,
                     shop: Shop, cfg: Config, bot: Bot):
    o = await _own_order(call, db, callback_data.id)
    if not o:
        return
    await call.answer()
    if o["status"] != "active":
        status = {"unknown": "проверяется администратором", "refunded": "средства возвращены"}
        await call.message.answer(f"Заказ л{o['id']}: {status.get(o['status'], o['status'])}.")
        return
    try:
        order = await shop.api.get_order(int(o["sp_order_id"]))
    except SteelProxyError as exc:
        await call.message.answer(exc.customer_message)
        return
    if order.get("expires_at") and order["expires_at"] != o["expires_at"]:
        await db.update_order(o["id"], expires_at=order["expires_at"])
    st = {"active": "активен", "refunded": "возврат", "partially_refunded": "частичный возврат"}
    kb = InlineKeyboardBuilder()
    if order.get("status") == "active":
        kb.button(text="🔄 Продлить", callback_data=OrderCB(act="ext", id=o["id"]))
    kb.button(text="📄 Скачать списком", callback_data=OrderCB(act="file", id=o["id"]))
    kb.adjust(2)
    header = (f"<b>Заказ №{order['order_id']}</b> — {st.get(order.get('status'), order.get('status'))}\n"
              f"{location_name(o['country_code'], o['location_title'])} · "
              f"{esc(PROXY_TYPE_TITLES.get(o['proxy_type'], o['proxy_type']))} · {o['quantity']} шт\n"
              f"Создан: {fmt_dt(order.get('created_at'), cfg)}\n"
              f"Действует до: <b>{fmt_dt(order.get('expires_at'), cfg)}</b>")
    await send_proxies(bot, call.from_user.id, order, header, kb.as_markup())


@router.callback_query(OrderCB.filter(F.act == "file"))
async def order_file(call: CallbackQuery, callback_data: OrderCB, db: Database, shop: Shop):
    from .common import proxies_file
    o = await _own_order(call, db, callback_data.id)
    if not o or not o["sp_order_id"]:
        return
    await call.answer()
    try:
        order = await shop.api.get_order(int(o["sp_order_id"]))
    except SteelProxyError as exc:
        await call.message.answer(exc.customer_message)
        return
    await call.message.answer_document(proxies_file(order))


@router.callback_query(OrderCB.filter(F.act == "ext"))
async def order_extend_menu(call: CallbackQuery, callback_data: OrderCB, db: Database, shop: Shop):
    o = await _own_order(call, db, callback_data.id)
    if not o:
        return
    kb = InlineKeyboardBuilder()
    for m in EXTEND_MONTHS:
        price = await shop.extend_price(o, m)
        if price is None:
            continue
        kb.button(text=f"{term_name(30 * m)} — {fmt_rub(price)}",
                  callback_data=OrderCB(act="extok", id=o["id"], p=m))
    kb.adjust(1)
    if not list(kb.buttons):
        await call.answer("Продление для этой локации сейчас недоступно.", show_alert=True)
        return
    await call.message.answer(f"Продление заказа №{o['sp_order_id']} ({o['quantity']} шт).\n"
                              "На сколько продлить?", reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(OrderCB.filter(F.act == "extok"))
async def order_extend(call: CallbackQuery, callback_data: OrderCB, db: Database, shop: Shop,
                       cfg: Config, bot: Bot):
    o = await _own_order(call, db, callback_data.id)
    if not o:
        return
    lock = shop.user_lock(call.from_user.id)
    if lock.locked():
        await call.answer("Операция уже выполняется…")
        return
    async with lock:
        await call.answer()
        await call.message.edit_text("⏳ Продлеваем…")
        try:
            res, charged, bal = await shop.extend(o, callback_data.p)
        except ShopError as exc:
            text = f"❌ {esc(exc)}"
            if str(exc).startswith("Недостаточно"):
                kb = InlineKeyboardBuilder()
                kb.button(text="💳 Пополнить баланс", callback_data=TopupCB(act="start"))
                await call.message.edit_text(text, reply_markup=kb.as_markup())
            else:
                await call.message.edit_text(text)
            if exc.status == 402:
                await notify_admins(bot, cfg, "🔴 <b>Недостаточно средств на балансе Steel Proxy</b> — "
                                              "продление не прошло.")
            return
        except SteelProxyUnknownResult:
            await call.message.edit_text(
                "⚠️ Связь с поставщиком прервалась. Деньги зарезервированы, администратор "
                f"проверит продление вручную. Поддержка: {esc(cfg.support_contact)}")
            await notify_admins(bot, cfg, f"⚠️ Продление заказа №{o['sp_order_id']} "
                                          f"(пользователь {o['user_id']}, {callback_data.p} мес.) — "
                                          "сбой сети, результат неизвестен. Проверьте в Ф-боте.")
            return
    await call.message.edit_text(
        f"✅ Заказ №{o['sp_order_id']} продлён на {term_name(30 * callback_data.p)}.\n"
        f"Действует до: <b>{fmt_dt(res.get('expires_at'), cfg)}</b>\n"
        f"Списано: {fmt_rub(charged)} · баланс: {fmt_rub(bal)}")
    await notify_admins(bot, cfg, f"🔄 Продление №{o['sp_order_id']} на {callback_data.p} мес.: "
                                  f"{fmt_rub(charged)}. Баланс Steel Proxy: "
                                  f"{esc(res.get('balance_after'))} ₽")


# ============================ баланс и пополнение ============================

@router.message(F.text == BTN_BALANCE)
@router.message(Command("balance"))
async def balance(message: Message, db: Database):
    u = await db.get_user(message.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Пополнить", callback_data=TopupCB(act="start"))
    await message.answer(f"Ваш баланс: <b>{fmt_rub(u['balance'] if u else 0)}</b>\n"
                         f"ID: <code>{message.from_user.id}</code>",
                         reply_markup=kb.as_markup())


TERMS_NOTE = ("<i>Оплачивая, вы принимаете Пользовательское соглашение (/terms); "
              "условия возврата — в его разделе 4.</i>")

SERVICES = {"xrocket": "💎 Криптовалютой (xRocket)", "manual": "🏦 Переводом по реквизитам"}


def topup_services(cfg: Config, xr) -> list[str]:
    """Доступные способы пополнения в том порядке, в каком их показываем."""
    out = []
    if xr is not None:
        out.append("xrocket")
    if cfg.manual_payments or not out:
        out.append("manual")
    return out


@router.callback_query(TopupCB.filter(F.act == "start"))
async def topup_start(call: CallbackQuery, callback_data: TopupCB, cfg: Config, state: FSMContext,
                      xr=None):
    await call.answer()
    await state.clear()
    suggested = max(cfg.min_topup_rub, callback_data.a)
    services = topup_services(cfg, xr)
    if len(services) == 1:   # выбирать не из чего — сразу к сумме
        await _ask_amount(call.message, services[0], suggested, cfg, state)
        return
    kb = InlineKeyboardBuilder()
    for svc in services:
        kb.button(text=SERVICES[svc], callback_data=TopupCB(act="svc", a=suggested, c=svc))
    kb.adjust(1)
    await call.message.answer("Выберите способ пополнения:", reply_markup=kb.as_markup())


@router.callback_query(TopupCB.filter(F.act == "svc"))
async def topup_service(call: CallbackQuery, callback_data: TopupCB, cfg: Config,
                        state: FSMContext, xr=None):
    await call.answer()
    if callback_data.c not in topup_services(cfg, xr):
        await call.message.answer("Этот способ сейчас недоступен.")
        return
    await _ask_amount(call.message, callback_data.c, max(cfg.min_topup_rub, callback_data.a),
                      cfg, state)


async def _ask_amount(target: Message, svc: str, suggested: int, cfg: Config, state: FSMContext):
    await state.set_state(TopupStates.amount)
    await state.update_data(svc=svc)
    kb = InlineKeyboardBuilder()
    for a in sorted({suggested, 300, 500, 1000, 3000}):
        if a >= cfg.min_topup_rub:
            kb.button(text=f"{a} ₽", callback_data=TopupCB(act="amount", a=a, c=svc))
    kb.adjust(3)
    await target.answer(
        f"{SERVICES[svc]}\n\nНа какую сумму пополнить? Выберите или введите число "
        f"(от {cfg.min_topup_rub} ₽).\n/cancel — отмена.", reply_markup=kb.as_markup())


async def _topup_amount_chosen(target: Message, user_id: int, amount: int, svc: str, cfg: Config,
                               state: FSMContext, shop: Shop, xr=None):
    await state.clear()
    if svc == "xrocket" and xr is not None:
        if len(xr.currencies) == 1:   # одна валюта — сразу выставляем счёт
            await _send_xrocket_invoice(target, user_id, amount, xr.currencies[0], cfg, shop, xr)
            return
        kb = InlineKeyboardBuilder()
        for cur in xr.currencies:
            kb.button(text=f"💎 {xrocket_title(cur)}",
                      callback_data=TopupCB(act="xr", a=amount, c=cur))
        kb.adjust(3)
        await target.answer(f"Сумма: <b>{amount} ₽</b>\n\nВыберите криптовалюту. Оплата — "
                            "через @xRocket, деньги зачислятся автоматически.",
                            reply_markup=kb.as_markup())
        return
    await _manual_topup(target, amount, cfg, state)


async def _manual_topup(target: Message, amount: int, cfg: Config, state: FSMContext):
    await state.set_state(TopupStates.receipt)
    await state.update_data(amount=amount)
    await target.answer(
        f"Сумма: <b>{amount} ₽</b>\n\n{esc(cfg.payment_info)}\n\n"
        "После оплаты пришлите сюда <b>скриншот или PDF чека</b> — администратор "
        "проверит и зачислит деньги. /cancel — отмена.\n\n" + TERMS_NOTE)


def xrocket_title(cur: str) -> str:
    from ..xrocket import TITLES
    return TITLES.get(cur, cur)


@router.callback_query(TopupCB.filter(F.act == "xr"))
async def topup_xrocket(call: CallbackQuery, callback_data: TopupCB, cfg: Config, shop: Shop,
                        xr=None):
    await call.answer()
    await _send_xrocket_invoice(call.message, call.from_user.id, callback_data.a,
                                callback_data.c, cfg, shop, xr)


async def _send_xrocket_invoice(target: Message, user_id: int, amount: int, currency: str,
                                cfg: Config, shop: Shop, xr) -> None:
    from ..xrocket import XRocketError
    if xr is None:
        await target.answer("Оплата криптовалютой сейчас недоступна.")
        return
    if not cfg.min_topup_rub <= amount <= 1_000_000:
        await target.answer("Неверная сумма.")
        return
    lock = shop.user_lock(user_id)
    if lock.locked():
        return
    async with lock:
        try:
            inv = await xr.create_invoice(user_id, amount * 100, currency)
        except XRocketError as exc:
            await target.answer(f"❌ Не удалось выставить счёт. {esc(exc.customer_message)}")
            return
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text=f"💳 Оплатить {inv.amount} {xrocket_title(inv.currency)}",
                                url=inv.url))
    kb.row(btn("🔄 Проверить оплату", TopupCB(act="xrcheck")))
    minutes = max(1, int((inv.expires_at - time.time()) // 60))
    await target.answer(
        f"Счёт на <b>{amount} ₽</b> = <b>{esc(inv.amount)} {esc(xrocket_title(inv.currency))}</b> "
        f"по курсу xRocket.\nДействует {minutes} мин.\n\n"
        "Нажмите «Оплатить» — откроется @xRocket. После оплаты деньги зачислятся на баланс "
        "автоматически в течение минуты, мы пришлём уведомление.\n\n" + TERMS_NOTE,
        reply_markup=kb.as_markup())


@router.callback_query(TopupCB.filter(F.act == "xrcheck"))
async def topup_xrocket_check(call: CallbackQuery, xr=None):
    if xr is None:
        await call.answer()
        return
    credited = await xr.check_user(call.from_user.id)
    if credited:
        await call.answer("Оплата получена!")   # уведомление с суммой пришлёт on_paid
    else:
        await call.answer("Оплата пока не поступила. Если вы уже оплатили — подождите минуту.",
                          show_alert=True)


@router.callback_query(TopupCB.filter(F.act == "amount"))
async def topup_amount_btn(call: CallbackQuery, callback_data: TopupCB, cfg: Config,
                           state: FSMContext, shop: Shop, xr=None):
    await call.answer()
    svc = callback_data.c or "manual"
    if svc not in topup_services(cfg, xr):
        await call.message.answer("Этот способ сейчас недоступен.")
        return
    await _topup_amount_chosen(call.message, call.from_user.id, callback_data.a, svc, cfg,
                               state, shop, xr)


@router.message(TopupStates.amount)
async def topup_amount_text(message: Message, cfg: Config, state: FSMContext, shop: Shop,
                            xr=None):
    text = (message.text or "").replace(" ", "").replace("₽", "")
    if not text.isdigit() or int(text) < cfg.min_topup_rub or int(text) > 1_000_000:
        await message.answer(f"Введите целое число рублей от {cfg.min_topup_rub}.")
        return
    svc = (await state.get_data()).get("svc") or topup_services(cfg, xr)[0]
    await _topup_amount_chosen(message, message.from_user.id, int(text), svc, cfg, state,
                               shop, xr)


@router.message(TopupStates.receipt, F.photo | F.document)
async def topup_receipt(message: Message, db: Database, cfg: Config, state: FSMContext, bot: Bot):
    from .admin import topup_decision_kb
    data = await state.get_data()
    await state.clear()
    amount = to_kop(data["amount"])
    topup_id = await db.create_topup(message.from_user.id, amount)
    u = message.from_user
    caption = (f"💳 Заявка на пополнение #{topup_id}\n"
               f"{esc(u.full_name)} (@{esc(u.username) if u.username else '—'}, "
               f"<code>{u.id}</code>)\nСумма: <b>{fmt_rub(amount)}</b>")
    for admin_id in cfg.admin_ids:
        try:
            await message.copy_to(admin_id)
            await bot.send_message(admin_id, caption, reply_markup=topup_decision_kb(topup_id))
        except Exception as exc:
            log.warning("admin %s: %s", admin_id, exc)
    await message.answer(f"Заявка #{topup_id} отправлена. Как только администратор проверит "
                         "оплату, деньги появятся на балансе — мы пришлём уведомление.",
                         reply_markup=MAIN_KB)


@router.message(TopupStates.receipt)
async def topup_receipt_wrong(message: Message):
    await message.answer("Пришлите, пожалуйста, скриншот (фото) или файл чека. /cancel — отмена.")
