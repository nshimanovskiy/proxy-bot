"""Поддельный API Steel Proxy (ответы — как в документации) и Telegram-сессия для тестов."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiogram.client.session.base import BaseSession
from aiogram.methods import (AnswerCallbackQuery, CopyMessage, DeleteMessage,
                             EditMessageReplyMarkup, EditMessageText, GetMe, SendDocument,
                             SendMessage, SetMyCommands, TelegramMethod)
from aiogram.types import Chat, Message, MessageId, User

from bot.config import Config
from bot.steelproxy import SteelProxyError, SteelProxyUnknownResult

BOT_ID = 777


def make_config(admin_id: int = 1, **extra) -> Config:
    return Config(**extra,
        bot_token=f"{BOT_ID}:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", api_key="sp_test",
        api_base="https://example.invalid/v1", admin_ids=frozenset({admin_id}),
        markup_percent=Decimal(30), db_path=":memory:", shop_name="Test Shop",
        support_contact="@support", payment_info="Карта 0000", min_topup_rub=100,
        low_balance_rub=Decimal(1000), tz=ZoneInfo("Europe/Moscow"))


class FakeAPI:
    def __init__(self):
        self.fail: str | None = None
        self.orders: dict[int, dict] = {}
        self.next_id = 1000

    async def proxy_types(self):
        return [{"code": "server", "title": "Серверные"},
                {"code": "residential", "title": "Резидентские"}]

    async def locations(self, proxy_type=None):
        return [{"node_id": 3, "country_code": "RU", "title": "Россия",
                 "proxy_type": "server", "terms_days": [30]},
                {"node_id": 5, "country_code": "US", "title": "США",
                 "proxy_type": "residential", "terms_days": [1, 7, 14, 30]}]

    async def prices(self, proxy_type=None, country_code=None):
        return [{"proxy_type": "residential", "country_code": "US", "node_id": 5,
                 "term_days": 7, "price": "47.50", "currency": "RUB"},
                {"proxy_type": "residential", "country_code": "US", "node_id": 5,
                 "term_days": 30, "price": "100.00", "currency": "RUB"},
                {"proxy_type": "server", "country_code": "RU", "node_id": 3,
                 "term_days": 30, "price": "80.00", "currency": "RUB"}]

    async def create_order(self, proxy_type, node_id, term_days, quantity, buyer_reference=None):
        if self.fail in ("402", "409", "502"):
            raise SteelProxyError(int(self.fail), "fail")
        oid = self.next_id
        self.next_id += 1
        unit = Decimal("47.50") if term_days == 7 else Decimal("100.00") * (term_days // 30)
        order = {
            "order_id": oid, "status": "active", "created_at": "2026-10-16T10:00:00Z",
            "expires_at": "2026-10-23T10:00:00Z", "proxy_type": proxy_type, "location": "US",
            "term_days": term_days, "quantity": quantity, "price": f"{unit * quantity:.2f}",
            "currency": "RUB", "balance_after": "4905.00", "buyer_reference": buyer_reference,
            "proxies": [{"username": f"u{i}", "password": "p", "host": "dc01.example",
                         "http_port": "3071", "socks5_port": "3072",
                         "connection_string_http": f"u{i}:p@dc01.example:3071",
                         "connection_string_socks5": f"u{i}:p@dc01.example:3072"}
                        for i in range(quantity)],
        }
        self.orders[oid] = order
        if self.fail == "net-after-create":
            raise SteelProxyUnknownResult("connection reset")
        return order

    async def get_order(self, order_id):
        if order_id not in self.orders:
            raise SteelProxyError(404, "not found")
        return self.orders[order_id]

    async def list_orders(self, page=1, page_size=20):
        items = sorted(self.orders.values(), key=lambda o: -o["order_id"])
        return {"total": len(items), "page": page, "page_size": page_size,
                "items": items[(page - 1) * page_size: page * page_size]}

    async def find_order_by_reference(self, reference, pages=3):
        return next((o for o in self.orders.values()
                     if o["buyer_reference"] == reference), None)

    async def extend_order(self, order_id, months):
        o = await self.get_order(order_id)
        price = Decimal("100.00") * o["quantity"] * months
        o["expires_at"] = "2026-12-01T10:00:00Z"
        return {"order_id": order_id, "months": months, "price": f"{price:.2f}",
                "currency": "RUB", "previous_expires_at": "2026-10-23T10:00:00Z",
                "expires_at": o["expires_at"], "balance_after": "4000.00"}

    async def close(self):
        pass


class FakeSession(BaseSession):
    """Вместо Telegram: запоминает все вызовы Bot API и возвращает правдоподобные ответы."""

    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self._mid = 100

    def _message(self, chat_id: int, text: str | None = None) -> Message:
        self._mid += 1
        return Message(message_id=self._mid, date=datetime.now(timezone.utc),
                       chat=Chat(id=chat_id, type="private"), text=text,
                       from_user=User(id=BOT_ID, is_bot=True, first_name="bot"))

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendDocument)):
            return self._message(method.chat_id, getattr(method, "text", None))
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            return True
        if isinstance(method, CopyMessage):
            self._mid += 1
            return MessageId(message_id=self._mid)
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="shop", username="test_shop_bot")
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage, SetMyCommands)):
            return True
        raise AssertionError(f"Неожиданный вызов Bot API: {type(method).__name__}")

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError
        yield b""

    def take(self) -> list[TelegramMethod]:
        calls, self.calls = self.calls, []
        return calls
