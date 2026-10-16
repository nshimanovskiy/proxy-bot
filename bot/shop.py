"""Бизнес-логика магазина: каталог с кэшем, наценка, покупка и продление."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from .db import Database, InsufficientFunds, to_kop
from .steelproxy import (MULTI_MONTH_TERMS, SteelProxyClient, SteelProxyError,
                         SteelProxyUnknownResult)

log = logging.getLogger(__name__)

CACHE_TTL = 300  # секунд


def term_label(days: int) -> str:
    if days % 30 == 0:
        months = days // 30
        word = "месяц" if months == 1 else ("месяца" if months in (2, 3, 4) else "месяцев")
        return f"{months} {word}"
    if days % 10 == 1 and days % 100 != 11:
        return f"{days} день"
    if days % 10 in (2, 3, 4) and days % 100 not in (12, 13, 14):
        return f"{days} дня"
    return f"{days} дней"


def flag(country_code: str | None) -> str:
    from .locations import flag as _flag, parse
    return _flag(parse(country_code, None)[0])


@dataclass
class Location:
    node_id: int
    country_code: str
    title: str
    proxy_type: str
    terms_days: list[int]

    @property
    def term_options(self) -> list[int]:
        opts = sorted(set(self.terms_days))
        if 30 in opts:
            opts += [t for t in MULTI_MONTH_TERMS if t not in opts]
        return opts


@dataclass
class PurchaseResult:
    local_id: int
    order: dict | None          # ответ API (с прокси), если заказ точно создан
    charged: int                # списано с покупателя, копейки
    balance_after: int
    pending_check: bool = False # сеть оборвалась — заказ на ручной проверке


class ShopError(Exception):
    """Ошибка, текст которой можно показать покупателю. status — код API, если был."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Shop:
    def __init__(self, db: Database, api: SteelProxyClient, markup_percent: Decimal):
        self.db = db
        self.api = api
        self.markup = Decimal(1) + markup_percent / 100
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = asyncio.Lock()
        self._user_locks: dict[int, asyncio.Lock] = {}
        self.last_api_balance: str | None = None

    def user_lock(self, user_id: int) -> asyncio.Lock:
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    # ---------- Каталог ----------
    async def _cached(self, key: str, loader):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
        value = await loader()
        self._cache[key] = (time.monotonic(), value)
        return value

    def invalidate_cache(self) -> None:
        self._cache.clear()

    async def proxy_types(self) -> list[dict]:
        return await self._cached("types", self.api.proxy_types)

    async def locations(self, proxy_type: str) -> list[Location]:
        raw = await self._cached("locations", self.api.locations)
        return [Location(int(r["node_id"]), r.get("country_code") or "", r.get("title") or "",
                         r["proxy_type"], [int(t) for t in r.get("terms_days") or []])
                for r in raw if r.get("proxy_type") == proxy_type]

    async def location(self, proxy_type: str, node_id: int) -> Location | None:
        for loc in await self.locations(proxy_type):
            if loc.node_id == node_id:
                return loc
        return None

    async def _price_table(self) -> list[dict]:
        return await self._cached("prices", self.api.prices)

    async def unit_cost(self, proxy_type: str, node_id: int, term_days: int) -> int | None:
        """Закупочная цена одного прокси, копейки (с учётом правила 60/90/180 = 30 × N)."""
        table = await self._price_table()

        def find(term: int) -> int | None:
            for p in table:
                if (p.get("proxy_type") == proxy_type and int(p.get("node_id", -1)) == node_id
                        and int(p.get("term_days", -1)) == term):
                    return to_kop(p["price"])
            return None

        exact = find(term_days)
        if exact is not None:
            return exact
        if term_days in MULTI_MONTH_TERMS:
            month = find(30)
            if month is not None:
                return month * (term_days // 30)
        return None

    def sell_price(self, cost_kop: int) -> int:
        """Цена для покупателя: закупка × наценка, округлено вверх до рубля."""
        raw = Decimal(cost_kop) * self.markup
        rub = -(-int(raw.to_integral_value(rounding="ROUND_CEILING")) // 100)
        return rub * 100

    async def unit_sell_price(self, proxy_type: str, node_id: int, term_days: int) -> int | None:
        cost = await self.unit_cost(proxy_type, node_id, term_days)
        return None if cost is None else self.sell_price(cost)

    # ---------- Покупка ----------
    async def purchase(self, user_id: int, loc: Location, term_days: int,
                       quantity: int) -> PurchaseResult:
        unit = await self.unit_sell_price(loc.proxy_type, loc.node_id, term_days)
        if unit is None:
            raise ShopError("Товар недоступен.")
        total = unit * quantity

        local_id = await self.db.create_local_order(
            user_id=user_id, proxy_type=loc.proxy_type, node_id=loc.node_id,
            country_code=loc.country_code, location_title=loc.title,
            term_days=term_days, quantity=quantity, price=total)
        ref = f"tg{user_id}-o{local_id}"

        # 1) Резервируем деньги покупателя.
        try:
            await self.db.change_balance(user_id, -total, "order", ref)
        except InsufficientFunds:
            await self.db.update_order(local_id, status="failed")
            raise ShopError("Недостаточно средств на балансе.") from None

        # 2) Создаём заказ у Steel Proxy.
        try:
            order = await self.api.create_order(loc.proxy_type, loc.node_id, term_days,
                                                quantity, buyer_reference=ref)
        except SteelProxyError as exc:
            log.warning("create_order failed: %s", exc)
            await self.db.update_order(local_id, status="failed")
            await self.db.change_balance(user_id, total, "refund", ref)
            self.invalidate_cache()
            raise ShopError(exc.customer_message, exc.status) from exc
        except SteelProxyUnknownResult:
            # Сверяемся по buyer_reference — заказ мог успеть создаться.
            order = await self._reconcile(ref)
            if order is None:
                await self.db.update_order(local_id, status="unknown")
                bal = (await self.db.get_user(user_id))["balance"]
                return PurchaseResult(local_id, None, total, bal, pending_check=True)

        await self._save_order(local_id, order)
        bal = (await self.db.get_user(user_id))["balance"]
        return PurchaseResult(local_id, order, total, bal)

    async def _reconcile(self, ref: str) -> dict | None:
        for delay in (2, 5, 10):
            await asyncio.sleep(delay)
            try:
                return await self.api.find_order_by_reference(ref)
            except SteelProxyError:
                continue
        return None

    async def _save_order(self, local_id: int, order: dict) -> None:
        self.last_api_balance = order.get("balance_after") or self.last_api_balance
        await self.db.update_order(
            local_id, status="active", sp_order_id=int(order["order_id"]),
            cost=to_kop(order.get("price", 0)), expires_at=order.get("expires_at"))

    async def resolve_unknown(self, local_id: int) -> str:
        """Ручная/повторная сверка заказа со статусом unknown. Возвращает итог."""
        o = await self.db.get_order(local_id)
        if not o or o["status"] != "unknown":
            return "nothing"
        ref = f"tg{o['user_id']}-o{local_id}"
        order = await self.api.find_order_by_reference(ref)
        if order:
            await self._save_order(local_id, order)
            return "active"
        await self.db.update_order(local_id, status="refunded")
        await self.db.change_balance(o["user_id"], o["price"], "refund", ref)
        return "refunded"

    # ---------- Продление ----------
    async def extend_price(self, order_row, months: int) -> int | None:
        unit = await self.unit_sell_price(order_row["proxy_type"], order_row["node_id"], 30)
        return None if unit is None else unit * order_row["quantity"] * months

    async def extend(self, order_row, months: int) -> tuple[dict, int, int]:
        """Продлевает заказ. Возвращает (ответ API, списано, баланс)."""
        if order_row["status"] != "active" or not order_row["sp_order_id"]:
            raise ShopError("Этот заказ нельзя продлить.")
        estimate = await self.extend_price(order_row, months)
        if estimate is None:
            raise ShopError("Продление для этой локации сейчас недоступно.")
        user_id = order_row["user_id"]
        ref = f"tg{user_id}-o{order_row['id']}-ext{months}"
        try:
            await self.db.change_balance(user_id, -estimate, "extend", ref)
        except InsufficientFunds:
            raise ShopError("Недостаточно средств на балансе.") from None

        try:
            res = await self.api.extend_order(int(order_row["sp_order_id"]), months)
        except SteelProxyError as exc:
            await self.db.change_balance(user_id, estimate, "refund", ref)
            raise ShopError(exc.customer_message, exc.status) from exc
        except SteelProxyUnknownResult:
            # Без идентификатора операции безопасно сверить нельзя — оставляем деньги
            # зарезервированными и зовём администратора.
            raise

        actual = self.sell_price(to_kop(res.get("price", 0)))
        charged = estimate
        if actual and actual != estimate:
            # Цена у поставщика изменилась — доводим списание до фактической.
            await self.db.change_balance(user_id, estimate - actual, "extend", ref + "-adj",
                                         allow_negative=True)
            charged = actual
        self.last_api_balance = res.get("balance_after") or self.last_api_balance
        await self.db.update_order(order_row["id"], expires_at=res.get("expires_at"),
                                   cost=order_row["cost"] + to_kop(res.get("price", 0)),
                                   price=order_row["price"] + charged, reminded=0)
        bal = (await self.db.get_user(user_id))["balance"]
        return res, charged, bal
