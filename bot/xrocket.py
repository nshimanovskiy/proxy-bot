"""Пополнение баланса криптовалютой через xRocket Pay — по образцу Тайника (server/billing.js).

Как это работает:
  1. Покупатель выбирает сумму в рублях и валюту → бот считает сумму в крипте по курсу
     xRocket (GET /api/v1/rates, с округлением вверх) и выставляет счёт POST /api/v1/invoices
     со своим clientInvoiceId. Покупатель платит по ссылке в @xRocket.
  2. Об оплате бот узнаёт:
       • сверкой: фоновая проверка неоплаченных счетов каждые 30 секунд и кнопка
         «Проверить оплату» (работает без домена и nginx);
       • вебхуком (необязательно): xRocket присылает подписанное уведомление на
         XROCKET_WEBHOOK_URL, бот проверяет подпись HMAC-SHA256 и зачисляет сразу.
  3. Рубли зачисляются на баланс ровно один раз, сколько бы уведомлений ни пришло;
     сумма и валюта сверяются с выставленным счётом.

В xRocket уходит только сумма, валюта, описание и случайный номер счёта — Telegram ID,
имя и @username покупателя туда не передаются.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

XROCKET_API = "https://pay.api.xrocket.exchange"
XROCKET_TESTNET_API = "https://pay.api.testnet.xrocket.exchange"
INVOICE_TTL = 3600            # счёт действует час (секунды)
REUSE_MIN = 600               # уже выставленный счёт отдаём повторно, если ему жить ещё 10+ минут
SIGNATURE_TOLERANCE = 300     # окно против повтора старых вебхуков (секунды)
RATE_CACHE = 120              # курс кэшируется на 2 минуты
DECIMAL_RE = re.compile(r"^\d{1,12}(\.\d{1,8})?$")
CURRENCY_RE = re.compile(r"^[A-Z0-9]{2,12}$")
# Одна и та же монета в xRocket может называться по-разному
ALIASES = {"TON": ["TONCOIN"], "TONCOIN": ["TON"], "GRAM": ["GRAMCOIN"], "TRX": ["TRON"]}
TITLES = {"TONCOIN": "TON", "GRAMCOIN": "GRAM"}


def parse_currencies(spec: str) -> list[str]:
    out = []
    for c in (spec or "").split(","):
        c = c.strip().upper()
        if not c:
            continue
        if not CURRENCY_RE.match(c):
            raise RuntimeError(f"XROCKET_CURRENCIES: неверный код валюты «{c}»")
        if c not in out:
            out.append(c)
    return out or ["USDT"]


def convert_price(rub: Decimal, rate: Decimal) -> str | None:
    """rub / rate, округлено ВВЕРХ до 4 значащих цифр (от 2 до 8 знаков после точки)."""
    try:
        x = Decimal(rub) / Decimal(rate)
    except (InvalidOperation, ZeroDivisionError):
        return None
    if not x.is_finite() or x <= 0:
        return None
    decimals = max(2, min(8, 3 - math.floor(math.log10(float(x)))))
    q = Decimal(1).scaleb(-decimals)
    up = x.quantize(q, rounding="ROUND_CEILING")
    s = f"{up:f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def same_amount(a, b) -> bool:
    """Одинаковые ли две десятичные суммы ("3" и "3.00" — да)."""
    def norm(x):
        s = str(x if x is not None else "").strip()
        if not DECIMAL_RE.match(s):
            return None
        return Decimal(s).normalize()
    x = norm(a)
    return x is not None and x == norm(b)


def verify_signature(secret: str | None, raw: bytes, signature: str | None,
                     version: str | None, timestamp: str | None,
                     now: float | None = None) -> bool:
    """Подпись вебхука xRocket Pay (v1): hex(HMAC-SHA256(secret, "{timestamp}.{raw body}")),
    timestamp — в миллисекундах."""
    if not secret or not signature or not timestamp or version != "v1":
        return False
    if not re.fullmatch(r"\d{10,16}", str(timestamp)):
        return False
    now_ms = (now if now is not None else time.time()) * 1000
    if abs(now_ms - int(timestamp)) > SIGNATURE_TOLERANCE * 1000:
        return False
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(signature)):
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def sign(secret: str, raw: bytes, timestamp_ms: int) -> str:
    """Подписать тело так же, как xRocket (для тестов)."""
    return hmac.new(secret.encode(), f"{timestamp_ms}.".encode() + raw, hashlib.sha256).hexdigest()


class XRocketError(Exception):
    def __init__(self, code: str, text: str = ""):
        super().__init__(code)
        self.code = code
        self.text = (text or "")[:200]

    @property
    def customer_message(self) -> str:
        if self.text:
            return f"xRocket: {self.text}"
        return {"no_rate": "Не удалось получить курс валюты, попробуйте через минуту.",
                "bad_currency": "Эта валюта сейчас недоступна."}.get(
            self.code, "Платёжный сервис временно недоступен, попробуйте через минуту.")


@dataclass
class Invoice:
    id: str
    url: str
    rub: int          # копейки
    amount: str       # сумма в крипте
    currency: str
    expires_at: float


PaidCallback = Callable[[dict], Awaitable[None]]


class XRocket:
    def __init__(self, db, token: str, webhook_secret: str | None = None,
                 webhook_url: str | None = None, testnet: bool = False,
                 currencies: list[str] | None = None, rub_per_usd: Decimal | None = None,
                 api: str | None = None, on_paid: PaidCallback | None = None):
        self.db = db
        self.token = token
        self.webhook_secret = webhook_secret or None
        self.callback_url = webhook_url if (webhook_url and webhook_secret) else None
        self.testnet = testnet
        self.api = (api or (XROCKET_TESTNET_API if testnet else XROCKET_API)).rstrip("/")
        self.currencies = currencies or ["USDT"]
        self.rub_per_usd = rub_per_usd
        self.on_paid = on_paid
        self._rates: dict[str, tuple[float, Decimal]] = {}
        self._checking: set[str] = set()
        self._session: aiohttp.ClientSession | None = None

    # ---------- HTTP ----------
    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def call(self, method: str, path: str, query: dict | list | None = None,
                   body: dict | None = None):
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        try:
            async with self._session.request(method, self.api + path, params=query,
                                             json=body, headers=headers) as r:
                text = await r.text()
                status = r.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise XRocketError("unavailable", "") from exc
        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
        if status >= 300:
            # Ошибки — RFC 9457: ветвимся по type (/api/problems/<code>), detail — для людей
            code = (data.get("type") or "").rsplit("/", 1)[-1] if isinstance(data, dict) else ""
            text = (data.get("detail") or data.get("title") or "") if isinstance(data, dict) else ""
            raise XRocketError(code or f"http_{status}", text)
        return data

    # ---------- валюты и курс ----------
    async def check_currencies(self) -> None:
        """Каких валют из списка нет в xRocket — убираем (с записью в журнал)."""
        try:
            data = await self.call("GET", "/api/v1/currencies")
        except XRocketError as exc:
            log.warning("xRocket: список валют не получен — %s", exc.code)
            return
        known = {str(c.get("code", "")).upper() for c in (data or []) if isinstance(c, dict)}
        if not known:
            return
        fixed = []
        for c in self.currencies:
            c = c if c in known else next((a for a in ALIASES.get(c, []) if a in known), c)
            if c in known and c not in fixed:
                fixed.append(c)
            elif c not in known:
                log.warning("xRocket: валюты %s нет — она не предлагается", c)
        if fixed:
            self.currencies = fixed

    @staticmethod
    def _pick(data, code: str) -> Decimal | None:
        for x in data or []:
            if isinstance(x, dict) and str(x.get("currency", "")).upper() == code:
                try:
                    r = Decimal(str(x.get("rate")))
                    return r if r > 0 else None
                except InvalidOperation:
                    return None
        return None

    async def rate(self, currency: str) -> Decimal:
        """Сколько рублей стоит 1 единица currency. Кэш на 2 минуты."""
        hit = self._rates.get(currency)
        if hit and time.monotonic() - hit[0] < RATE_CACHE:
            return hit[1]
        rate = None
        try:
            rate = self._pick(await self.call("GET", "/api/v1/rates",
                                              {"base": "RUB", "assets": currency}), currency)
        except XRocketError as exc:
            log.warning("xRocket: курс %s к RUB не получен — %s %s", currency, exc.code, exc.text)
        if rate is None and self.rub_per_usd:
            # Запасной путь: курс к USD из xRocket × RUB_PER_USD из .env
            try:
                usd = self._pick(await self.call("GET", "/api/v1/rates",
                                                 {"base": "USD", "assets": currency}), currency)
                if usd:
                    rate = usd * self.rub_per_usd
            except XRocketError as exc:
                log.warning("xRocket: курс %s к USD не получен — %s", currency, exc.code)
        if rate is None:
            raise XRocketError("no_rate")
        self._rates[currency] = (time.monotonic(), rate)
        return rate

    # ---------- счета ----------
    async def create_invoice(self, user_id: int, rub_kop: int, currency: str) -> Invoice:
        currency = currency.upper()
        if currency not in self.currencies:
            raise XRocketError("bad_currency")
        now = time.time()
        open_ = await self.db.open_xr_invoice(user_id, rub_kop, currency, now + REUSE_MIN)
        if open_:
            return Invoice(open_["id"], open_["url"], open_["rub"], open_["amount"],
                           open_["currency"], open_["expires_at"])

        amount = convert_price(Decimal(rub_kop) / 100, await self.rate(currency))
        if not amount:
            raise XRocketError("no_rate")
        inv_id = "pb" + secrets.token_hex(12)
        await self.db.add_xr_invoice(inv_id, user_id, rub_kop, amount, currency,
                                     now + INVOICE_TTL)
        body = {
            "priceAmount": amount,
            "priceCurrency": currency,
            "numPayments": 1,
            "clientInvoiceId": inv_id,
            "description": f"Пополнение баланса на {rub_kop // 100} ₽",  # без имён и ID
            "expiresIn": INVOICE_TTL * 1000,
        }
        if self.callback_url:
            body["callback"] = {"callbackUrl": self.callback_url}
        inv = None
        try:
            inv = await self.call("POST", "/api/v1/invoices", body=body)
        except XRocketError as exc:
            if exc.code == "client_id_already_taken":
                try:
                    inv = await self.call("GET", "/api/v1/invoice", {"clientInvoiceId": inv_id})
                except XRocketError:
                    inv = None
            if not inv:
                await self.db.close_xr_invoice(inv_id, "failed")
                log.warning("xRocket не выставил счёт (%s %s) — %s %s", amount, currency,
                            exc.code, exc.text)
                raise
        url = ((inv or {}).get("links") or {}).get("telegramBotLink")
        if not isinstance(url, str) or not url.startswith("https://"):
            await self.db.close_xr_invoice(inv_id, "failed")
            raise XRocketError("no_link")
        expires = now + INVOICE_TTL
        await self.db.set_xr_invoice_link(inv_id, str(inv.get("id") or ""), url, expires)
        return Invoice(inv_id, url, rub_kop, amount, currency, expires)

    async def _apply(self, row, inv: dict) -> dict | None:
        """Счёт из ответа xRocket применить к нашей записи. Возвращает зачисление или None."""
        if not isinstance(inv, dict):
            return None
        if row["invoice_id"] and inv.get("id") and str(inv["id"]) != row["invoice_id"]:
            return None  # не тот счёт
        status = inv.get("status")
        if status == "paid":
            # Защита от подмены: сумма и валюта должны совпасть с выставленными
            if inv.get("priceCurrency") != row["currency"] or not same_amount(
                    inv.get("priceAmount"), row["amount"]):
                log.warning("xRocket: сумма или валюта счёта %s не совпадает — не засчитано",
                            row["id"])
                return None
            credited = await self.db.credit_xr_invoice(row["id"])
            if credited and self.on_paid:
                try:
                    await self.on_paid(credited)
                except Exception:
                    log.exception("on_paid")
            return credited
        if status in ("expired", "cancelled"):
            await self.db.close_xr_invoice(row["id"], status)
        return None

    async def check_invoice(self, row) -> dict | None:
        if row["id"] in self._checking:
            return None
        self._checking.add(row["id"])
        try:
            inv = await self.call("GET", "/api/v1/invoice", {"clientInvoiceId": row["id"]})
            return await self._apply(row, inv)
        except XRocketError as exc:
            if exc.code in ("not_found", "invoice_not_found") and time.time() - row["created_at"] > 120:
                await self.db.close_xr_invoice(row["id"], "failed")
            else:
                log.info("xRocket: сверка счёта %s не удалась — %s", row["id"], exc.code)
            return None
        finally:
            self._checking.discard(row["id"])

    async def check_user(self, user_id: int) -> list[dict]:
        """Кнопка «Проверить оплату»: сверить неоплаченные счета пользователя."""
        out = []
        for row in await self.db.pending_xr_invoices(user_id=user_id, limit=5):
            if (res := await self.check_invoice(row)):
                out.append(res)
        return out

    async def reconcile(self, limit: int = 8) -> None:
        """Фоновая сверка (лимит API xRocket — 20 запросов в минуту)."""
        for row in await self.db.pending_xr_invoices(limit=limit):
            await self.check_invoice(row)

    async def reconcile_loop(self, every: int = 30) -> None:
        await self.check_currencies()
        while True:
            await asyncio.sleep(every)
            try:
                await self.db.expire_xr_invoices(time.time() - 600)
                await self.reconcile()
            except Exception:
                log.exception("xRocket reconcile")

    # ---------- вебхук ----------
    async def handle_webhook(self, raw: bytes, headers) -> int:
        """Возвращает HTTP-статус ответа."""
        if not self.webhook_secret:
            return 404
        if not verify_signature(self.webhook_secret, raw, headers.get("signature"),
                                headers.get("signature-version"),
                                headers.get("signature-timestamp")):
            log.warning("xRocket: вебхук с неверной подписью отклонён")
            return 401
        try:
            event = json.loads(raw.decode())
        except ValueError:
            return 200  # подпись верна, но разобрать нельзя — повторять бессмысленно
        inv = (event.get("data") or {}).get("invoice") if event.get("type") == "invoice" else None
        if isinstance(inv, dict) and isinstance(inv.get("clientInvoiceId"), str):
            row = await self.db.get_xr_invoice(inv["clientInvoiceId"])
            if row and row["status"] != "paid":
                # payment_status_changed несёт усечённый invoice — за полным счётом сходим сами
                if event["data"].get("event") == "invoice_status_changed" and inv.get("priceAmount") is not None:
                    await self._apply(row, inv)
                elif inv.get("status") == "paid":
                    await self.check_invoice(row)
        return 200

    def web_app(self, path: str):
        from aiohttp import web

        async def handler(request: web.Request):
            raw = await request.content.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                return web.Response(status=413)
            status = await self.handle_webhook(raw, request.headers)
            return web.Response(status=status, text="" if status == 200 else "error")

        app = web.Application()
        app.router.add_post(path, handler)
        return app
