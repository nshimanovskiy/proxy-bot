"""xRocket Pay: курс и сумма, счёт, зачисление ровно один раз, вебхук с подписью,
и сценарий в боте — с фейковым сервером xRocket."""
import json
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from aiohttp import web
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User

from bot import xrocket as X
from bot.db import Database
from bot.handlers.common import BTN_BALANCE
from bot.main import build_dispatcher, create_xrocket
from bot.shop import Shop

from .fakes import BOT_ID, FakeAPI, FakeSession, make_config

SECRET = "whsec"


class FakeXRocket:
    """Минимальный xRocket Pay API: currencies, rates, invoices."""

    def __init__(self):
        self.invoices: dict[str, dict] = {}
        self.created: list[dict] = []
        self.rub_rates = {"USDT": "92.5", "TONCOIN": "250"}
        self.usd_rates = {"USDT": "1", "TONCOIN": "2.7", "TRX": "0.25"}
        self.auth = []

    async def start(self):
        app = web.Application()
        app.router.add_get("/api/v1/currencies", self.currencies)
        app.router.add_get("/api/v1/rates", self.rates)
        app.router.add_post("/api/v1/invoices", self.create)
        app.router.add_get("/api/v1/invoice", self.get)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def stop(self):
        await self.runner.cleanup()

    @staticmethod
    def problem(code, status=400, detail=""):
        return web.json_response({"type": f"/api/problems/{code}", "detail": detail}, status=status)

    async def currencies(self, request):
        self.auth.append(request.headers.get("Authorization"))
        return web.json_response([{"code": "USDT"}, {"code": "TONCOIN"}, {"code": "TRX"}])

    async def rates(self, request):
        base, asset = request.query["base"], request.query["assets"]
        table = self.rub_rates if base == "RUB" else self.usd_rates
        if asset not in table:
            return self.problem("rate_not_found", 404)
        return web.json_response([{"currency": asset, "rate": table[asset]}])

    async def create(self, request):
        body = await request.json()
        self.created.append(body)
        if Decimal(body["priceAmount"]) < Decimal("0.5"):
            return self.problem("validation_error", 422, "Минимальная сумма — 0.5")
        inv = {"id": 1000 + len(self.invoices), "clientInvoiceId": body["clientInvoiceId"],
               "priceAmount": body["priceAmount"], "priceCurrency": body["priceCurrency"],
               "status": "active",
               "links": {"telegramBotLink": f"https://t.me/xrocket?start=inv_{len(self.invoices)}"}}
        self.invoices[body["clientInvoiceId"]] = inv
        return web.json_response(inv, status=201)

    async def get(self, request):
        inv = self.invoices.get(request.query["clientInvoiceId"])
        return web.json_response(inv) if inv else self.problem("invoice_not_found", 404)

    def pay(self, client_id, amount=None):
        inv = self.invoices[client_id]
        inv["status"] = "paid"
        if amount:
            inv["priceAmount"] = amount


class HelpersTest(unittest.TestCase):
    def test_convert_price_rounds_up(self):
        self.assertEqual(X.convert_price(Decimal(500), Decimal("92.5")), "5.406")   # 5.4054…
        self.assertEqual(X.convert_price(Decimal(100), Decimal("250")), "0.4")
        self.assertEqual(X.convert_price(Decimal(1000), Decimal("0.0001")), "10000000")
        self.assertIsNone(X.convert_price(Decimal(100), Decimal(0)))

    def test_same_amount(self):
        self.assertTrue(X.same_amount("3", "3.00"))
        self.assertTrue(X.same_amount(5.406, "5.406"))
        self.assertFalse(X.same_amount("3", "3.01"))
        self.assertFalse(X.same_amount("abc", "abc"))

    def test_signature(self):
        raw = b'{"type":"invoice"}'
        ts = str(int(time.time() * 1000))
        sig = X.sign(SECRET, raw, int(ts))
        self.assertTrue(X.verify_signature(SECRET, raw, sig, "v1", ts))
        self.assertFalse(X.verify_signature("other", raw, sig, "v1", ts))
        self.assertFalse(X.verify_signature(SECRET, raw + b" ", sig, "v1", ts))
        self.assertFalse(X.verify_signature(SECRET, raw, sig, "v2", ts))
        old = str(int(time.time() * 1000) - 600_000)
        self.assertFalse(X.verify_signature(SECRET, raw, X.sign(SECRET, raw, int(old)), "v1", old))

    def test_parse_currencies(self):
        self.assertEqual(X.parse_currencies(" usdt, TON ,usdt"), ["USDT", "TON"])
        self.assertEqual(X.parse_currencies(""), ["USDT"])
        with self.assertRaises(RuntimeError):
            X.parse_currencies("US-DT")


class XRocketTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeXRocket()
        self.url = await self.fake.start()
        self.db = Database(":memory:")
        await self.db.connect()
        await self.db.upsert_user(10, "buyer", "Buyer")
        self.paid = []

        async def on_paid(p):
            self.paid.append(p)
        self.xr = X.XRocket(self.db, "tok", webhook_secret=SECRET,
                            webhook_url="https://shop.example/xrocket",
                            currencies=["USDT", "TON", "GRAM"], api=self.url, on_paid=on_paid)

    async def asyncTearDown(self):
        await self.xr.close()
        await self.db.close()
        await self.fake.stop()

    async def balance(self):
        return (await self.db.get_user(10))["balance"]

    async def test_currencies_aliases_and_missing(self):
        await self.xr.check_currencies()
        self.assertEqual(self.xr.currencies, ["USDT", "TONCOIN"])   # TON → TONCOIN, GRAM нет
        self.assertEqual(self.fake.auth[0], "Bearer tok")

    async def test_invoice_paid_by_polling_credits_once(self):
        inv = await self.xr.create_invoice(10, 50000, "USDT")
        body = self.fake.created[0]
        self.assertEqual(body["priceAmount"], "5.406")
        self.assertEqual(body["priceCurrency"], "USDT")
        self.assertEqual(body["callback"], {"callbackUrl": "https://shop.example/xrocket"})
        self.assertNotIn("buyer", json.dumps(body, ensure_ascii=False))
        self.assertNotIn("10", body["description"].replace("500", ""))
        self.assertTrue(inv.url.startswith("https://t.me/xrocket"))

        # повторный запрос той же суммы — тот же счёт
        again = await self.xr.create_invoice(10, 50000, "USDT")
        self.assertEqual(again.id, inv.id)
        self.assertEqual(len(self.fake.created), 1)

        self.assertEqual(await self.xr.check_user(10), [])
        self.fake.pay(inv.id)
        credited = await self.xr.check_user(10)
        self.assertEqual(credited[0]["rub"], 50000)
        self.assertEqual(await self.balance(), 50000)
        await self.xr.reconcile()
        await self.xr.check_user(10)
        self.assertEqual(await self.balance(), 50000)
        self.assertEqual(len(self.paid), 1)
        self.assertEqual((await self.db.xr_stats())["rub"], 50000)

    async def test_amount_mismatch_not_credited(self):
        inv = await self.xr.create_invoice(10, 50000, "USDT")
        self.fake.pay(inv.id, amount="0.01")
        self.assertEqual(await self.xr.check_user(10), [])
        self.assertEqual(await self.balance(), 0)

    async def test_expired_is_closed(self):
        inv = await self.xr.create_invoice(10, 50000, "USDT")
        self.fake.invoices[inv.id]["status"] = "expired"
        await self.xr.reconcile()
        self.assertEqual((await self.db.get_xr_invoice(inv.id))["status"], "expired")

    async def test_webhook(self):
        inv = await self.xr.create_invoice(10, 30000, "USDT")
        event = {"type": "invoice", "data": {"event": "invoice_status_changed", "invoice": {
            **self.fake.invoices[inv.id], "status": "paid"}}}
        raw = json.dumps(event).encode()
        ts = str(int(time.time() * 1000))
        headers = {"signature": X.sign(SECRET, raw, int(ts)), "signature-version": "v1",
                   "signature-timestamp": ts}
        self.assertEqual(await self.xr.handle_webhook(raw, {**headers, "signature": "0" * 64}), 401)
        self.assertEqual(await self.balance(), 0)
        self.assertEqual(await self.xr.handle_webhook(raw, headers), 200)
        self.assertEqual(await self.balance(), 30000)
        self.assertEqual(await self.xr.handle_webhook(raw, headers), 200)   # повтор
        self.assertEqual(await self.balance(), 30000)

    async def test_webhook_http_endpoint(self):
        from aiohttp.test_utils import TestClient, TestServer
        inv = await self.xr.create_invoice(10, 30000, "USDT")
        self.fake.pay(inv.id)
        raw = json.dumps({"type": "invoice", "data": {"event": "payment_status_changed",
                                                      "invoice": {"clientInvoiceId": inv.id,
                                                                  "status": "paid"}}}).encode()
        ts = str(int(time.time() * 1000))
        async with TestClient(TestServer(self.xr.web_app("/xrocket"))) as client:
            r = await client.post("/xrocket", data=raw, headers={
                "signature": X.sign(SECRET, raw, int(ts)), "signature-version": "v1",
                "signature-timestamp": ts})
            self.assertEqual(r.status, 200)
        self.assertEqual(await self.balance(), 30000)

    async def test_rate_fallback_and_errors(self):
        self.fake.rub_rates.pop("USDT")
        with self.assertRaises(X.XRocketError) as ctx:
            await self.xr.create_invoice(10, 50000, "USDT")
        self.assertEqual(ctx.exception.code, "no_rate")
        self.xr.rub_per_usd = Decimal("100")
        await self.xr.create_invoice(10, 50000, "USDT")
        self.assertEqual(self.fake.created[-1]["priceAmount"], "5")
        # пояснение xRocket доходит до покупателя
        self.xr.currencies.append("TONCOIN")
        with self.assertRaises(X.XRocketError) as ctx:
            await self.xr.create_invoice(10, 10000, "TONCOIN")
        self.assertIn("Минимальная сумма", ctx.exception.customer_message)
        with self.assertRaises(X.XRocketError):
            await self.xr.create_invoice(10, 10000, "BTC")


class BotTopupTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeXRocket()
        url = await self.fake.start()
        self.cfg = make_config(1, xrocket_token="tok", xrocket_currencies=("USDT", "TONCOIN"),
                               manual_payments=False)
        self.db = Database(":memory:")
        await self.db.connect()
        self.session = FakeSession()
        self.bot = Bot(self.cfg.bot_token, session=self.session,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.xr = create_xrocket(self.db, self.cfg, self.bot)
        self.xr.api = url
        self.dp = build_dispatcher(self.db, Shop(self.db, FakeAPI(), Decimal(30)), self.cfg, self.xr)
        self.n = 0

    async def asyncTearDown(self):
        for r in list(self.dp.sub_routers):
            r._parent_router = None
        await self.xr.close()
        await self.db.close()
        await self.fake.stop()

    def user(self):
        return User(id=10, is_bot=False, first_name="B", username="buyer")

    async def feed(self, **kw):
        self.n += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.n, **kw))
        return self.session.take()

    async def send(self, text):
        return await self.feed(message=Message(
            message_id=self.n + 1, date=datetime.now(timezone.utc), text=text,
            chat=Chat(id=10, type="private"), from_user=self.user()))

    async def press(self, data):
        msg = Message(message_id=5, date=datetime.now(timezone.utc), text="…",
                      chat=Chat(id=10, type="private"),
                      from_user=User(id=BOT_ID, is_bot=True, first_name="bot"))
        return await self.feed(callback_query=CallbackQuery(
            id=str(self.n), from_user=self.user(), chat_instance="c", message=msg, data=data))

    @staticmethod
    def buttons(calls):
        out = {}
        for c in calls:
            kb = getattr(c, "reply_markup", None)
            if isinstance(kb, InlineKeyboardMarkup):
                for row in kb.inline_keyboard:
                    for b in row:
                        out[b.text] = b.callback_data or b.url
        return out

    def find(self, calls, part):
        for t, d in self.buttons(calls).items():
            if part in t:
                return d
        self.fail(f"нет кнопки «{part}»: {list(self.buttons(calls))}")

    async def test_service_is_chosen_first(self):
        import dataclasses
        for r in list(self.dp.sub_routers):
            r._parent_router = None
        self.cfg = dataclasses.replace(self.cfg, manual_payments=True,
                                       payment_info="Карта 1111", xrocket_currencies=("USDT",))
        self.xr.currencies = ["USDT"]
        self.dp = build_dispatcher(self.db, Shop(self.db, FakeAPI(), Decimal(30)), self.cfg, self.xr)

        await self.send("/start")
        calls = await self.press(self.find(await self.send(BTN_BALANCE), "Пополнить"))
        btns = self.buttons(calls)
        self.assertEqual(list(btns), ["💎 Криптовалютой (xRocket)", "🏦 Переводом по реквизитам"])
        self.assertFalse(any("₽" in b for b in btns))         # сумма — только после выбора

        # реквизиты → сумма → чек
        calls = await self.press(btns["🏦 Переводом по реквизитам"])
        self.assertIn("На какую сумму", "\n".join(c.text for c in calls if isinstance(c, SendMessage)))
        calls = await self.press(self.find(calls, "500 ₽"))
        text = "\n".join(c.text for c in calls if isinstance(c, SendMessage))
        self.assertIn("Карта 1111", text)
        self.assertIn("чека", text)
        self.assertEqual(self.fake.created, [])

        # крипта → сумма текстом → одна валюта, счёт сразу
        await self.send("/cancel")
        calls = await self.press(self.find(await self.send(BTN_BALANCE), "Пополнить"))
        calls = await self.press(self.find(calls, "Криптовалютой"))
        calls = await self.send("700")
        self.assertEqual(len(self.fake.created), 1)
        self.assertEqual(self.fake.created[0]["priceCurrency"], "USDT")
        self.assertTrue(self.find(calls, "Оплатить").startswith("https://t.me/xrocket"))

    async def test_topup_with_crypto(self):
        await self.send("/start")
        calls = await self.send(BTN_BALANCE)
        calls = await self.press(self.find(calls, "Пополнить"))
        calls = await self.send("500")
        btns = self.buttons(calls)
        self.assertIn("💎 USDT", btns)
        self.assertIn("💎 TON", btns)                        # TONCOIN показывается как TON
        self.assertFalse(any("реквизит" in b for b in btns))  # ручной способ выключен

        calls = await self.press(btns["💎 USDT"])
        text = "\n".join(c.text for c in calls if isinstance(c, SendMessage))
        self.assertIn("500 ₽", text)
        self.assertIn("5.406 USDT", text)
        self.assertIn("/terms", text)
        pay_url = self.find(calls, "Оплатить")
        self.assertTrue(pay_url.startswith("https://t.me/xrocket"))
        check_cb = self.find(calls, "Проверить")

        await self.press(check_cb)
        self.assertEqual((await self.db.get_user(10))["balance"], 0)

        inv_id = next(iter(self.fake.invoices))
        self.fake.pay(inv_id)
        calls = await self.press(check_cb)
        self.assertEqual((await self.db.get_user(10))["balance"], 50000)
        to_user = [c.text for c in calls if isinstance(c, SendMessage) and c.chat_id == 10]
        self.assertTrue(any("Баланс пополнен на <b>500 ₽</b>" in t for t in to_user), to_user)
        to_admin = [c.text for c in calls if isinstance(c, SendMessage) and c.chat_id == 1]
        self.assertTrue(any("xRocket" in t and "@buyer" in t for t in to_admin))

        calls = await self.send("/terms")
        self.assertIn("xRocket Pay", "\n".join(c.text for c in calls if isinstance(c, SendMessage)))


if __name__ == "__main__":
    unittest.main()
