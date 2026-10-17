"""Сквозной сценарий через настоящий aiogram-диспетчер: меню, покупка, заказы, продление,
пополнение с подтверждением админом. Telegram и API Steel Proxy подменены."""
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import CopyMessage, EditMessageText, SendDocument, SendMessage
from aiogram.types import (CallbackQuery, Chat, InlineKeyboardMarkup, Message, PhotoSize,
                           Update, User)

from bot.db import Database
from bot.handlers.common import BTN_BALANCE, BTN_BUY, BTN_ORDERS
from bot.main import build_dispatcher
from bot.shop import Shop

from .fakes import BOT_ID, FakeAPI, FakeSession, make_config

ADMIN, BUYER = 1, 10


class BotScenarioTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = make_config(ADMIN)
        self.db = Database(":memory:")
        await self.db.connect()
        self.api = FakeAPI()
        self.shop = Shop(self.db, self.api, Decimal(30))
        self.session = FakeSession()
        self.bot = Bot(self.cfg.bot_token, session=self.session,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = build_dispatcher(self.db, self.shop, self.cfg)
        self.uid = 0

    async def asyncTearDown(self):
        # роутеры — модульные объекты; отвязываем, чтобы их можно было подключить снова
        for r in list(self.dp.sub_routers):
            r._parent_router = None
        await self.db.close()

    # ---------- helpers ----------
    def _user(self, uid):
        return User(id=uid, is_bot=False, first_name=f"user{uid}", username=f"user{uid}")

    async def _feed(self, **kw):
        self.uid += 1
        await self.dp.feed_update(self.bot, Update(update_id=self.uid, **kw))
        return self.session.take()

    async def send(self, uid, text=None, photo=False, reply_to=None):
        reply = None
        if reply_to is not None:
            reply = Message(message_id=reply_to, date=datetime.now(timezone.utc),
                            chat=Chat(id=uid, type="private"), text="…",
                            from_user=User(id=BOT_ID, is_bot=True, first_name="bot"))
        m = Message(message_id=self.uid + 1, date=datetime.now(timezone.utc),
                    chat=Chat(id=uid, type="private"), from_user=self._user(uid), text=text,
                    photo=[PhotoSize(file_id="f", file_unique_id="u", width=10, height=10)]
                    if photo else None, reply_to_message=reply)
        return await self._feed(message=m)

    async def press(self, uid, data):
        msg = Message(message_id=5, date=datetime.now(timezone.utc),
                      chat=Chat(id=uid, type="private"), text="старое",
                      from_user=User(id=BOT_ID, is_bot=True, first_name="bot"))
        q = CallbackQuery(id=str(self.uid), from_user=self._user(uid), chat_instance="c",
                          message=msg, data=data)
        return await self._feed(callback_query=q)

    @staticmethod
    def texts(calls):
        return "\n".join(c.text for c in calls
                         if isinstance(c, (SendMessage, EditMessageText)) and c.text)

    @staticmethod
    def buttons(calls):
        out = {}
        for c in calls:
            kb = getattr(c, "reply_markup", None)
            if isinstance(kb, InlineKeyboardMarkup):
                for row in kb.inline_keyboard:
                    for b in row:
                        out[b.text] = b.callback_data
        return out

    def find(self, buttons, part):
        for text, data in buttons.items():
            if part in text:
                return data
        self.fail(f"Нет кнопки «{part}» среди {list(buttons)}")

    # ---------- сценарий ----------
    async def test_full_flow(self):
        calls = await self.send(BUYER, "/start")
        self.assertIn("Добро пожаловать", self.texts(calls))

        calls = await self.send(BUYER, BTN_BUY)
        type_cb = self.find(self.buttons(calls), "Резидентские")

        calls = await self.press(BUYER, type_cb)
        self.assertIn("Выберите локацию", self.texts(calls))
        loc_cb = self.find(self.buttons(calls), "США · от 62 ₽")

        calls = await self.press(BUYER, loc_cb)
        btns = self.buttons(calls)
        self.assertIn("3 месяца — 390 ₽", btns)
        self.assertNotIn("14 дней", " ".join(btns))
        term_cb = self.find(btns, "7 дней — 62 ₽")

        calls = await self.press(BUYER, term_cb)
        qty_cb = self.find(self.buttons(calls), "2 шт · 124 ₽")

        # денег нет — предлагаем пополнить
        calls = await self.press(BUYER, qty_cb)
        self.assertIn("Не хватает 124 ₽", self.texts(calls))
        self.find(self.buttons(calls), "Пополнить")

        # не-админ не может начислить себе деньги
        await self.send(BUYER, f"/give {BUYER} 1000")
        self.assertEqual((await self.db.get_user(BUYER))["balance"], 0)

        calls = await self.send(ADMIN, "/start")
        calls = await self.send(ADMIN, f"/give @user{BUYER} 1000")
        self.assertIn("1 000 ₽", self.texts(calls))

        calls = await self.press(BUYER, qty_cb)
        pay_cb = self.find(self.buttons(calls), "Оплатить 124 ₽")

        calls = await self.press(BUYER, pay_cb)
        text = self.texts(calls)
        self.assertIn("Заказ №1000 оплачен", text)
        self.assertIn("u1:p@dc01.example:3071", text)
        self.assertIn("HTTPS: <code>u1:p@dc01.example:3071</code>", text)
        admin_msgs = [c.text for c in calls if isinstance(c, SendMessage) and c.chat_id == ADMIN]
        self.assertTrue(any("Продажа №1000" in t and "прибыль 29 ₽" in t for t in admin_msgs),
                        admin_msgs)
        self.assertEqual((await self.db.get_user(BUYER))["balance"], 100000 - 12400)

        # большой заказ отдаётся файлом
        qty20 = self.find(self.buttons(await self.press(BUYER, term_cb)), "20 шт")
        calls = await self.press(BUYER, qty20)
        self.assertIn("Не хватает", self.texts(calls))
        await self.send(ADMIN, f"/give {BUYER} 2000")
        calls = await self.press(BUYER, qty20)
        calls = await self.press(BUYER, self.find(self.buttons(calls), "Оплатить 1 240 ₽"))
        self.assertIn("Заказ №1001 оплачен", self.texts(calls))
        self.assertTrue(any(isinstance(c, SendDocument) and c.chat_id == BUYER for c in calls))
        self.assertEqual((await self.db.get_user(BUYER))["balance"], 87600 + 200000 - 124000)

        # мои заказы → карточка → продление
        calls = await self.send(BUYER, BTN_ORDERS)
        view_cb = self.find(self.buttons(calls), "№1000")
        calls = await self.press(BUYER, view_cb)
        self.assertIn("Заказ №1000", self.texts(calls))
        file_cb = self.find(self.buttons(calls), "Скачать")
        calls = await self.press(BUYER, file_cb)
        self.assertTrue(any(isinstance(c, SendDocument) for c in calls))

        calls = await self.press(BUYER, self.find(self.buttons(
            await self.press(BUYER, view_cb)), "Продлить"))
        ext_cb = self.find(self.buttons(calls), "1 месяц — 260 ₽")
        calls = await self.press(BUYER, ext_cb)
        self.assertIn("продлён на 1 месяц", self.texts(calls))
        self.assertEqual((await self.db.get_user(BUYER))["balance"], 163600 - 26000)

        # пополнение: сумма → чек → админ подтверждает
        calls = await self.send(BUYER, BTN_BALANCE)
        calls = await self.press(BUYER, self.find(self.buttons(calls), "Пополнить"))
        calls = await self.send(BUYER, "500")
        self.assertIn("Карта 0000", self.texts(calls))
        calls = await self.send(BUYER, photo=True)
        self.assertTrue(any(isinstance(c, CopyMessage) and c.chat_id == ADMIN for c in calls))
        approve = self.find(self.buttons(calls), "Зачислить")
        await self.press(BUYER, approve)  # чужое нажатие игнорируется
        self.assertEqual((await self.db.get_user(BUYER))["balance"], 137600)
        calls = await self.press(ADMIN, approve)
        self.assertEqual((await self.db.get_user(BUYER))["balance"], 137600 + 50000)
        self.assertTrue(any(isinstance(c, SendMessage) and c.chat_id == BUYER
                            and "зачислено" in c.text for c in calls))
        calls = await self.press(ADMIN, approve)  # повторное нажатие не зачисляет дважды
        self.assertEqual((await self.db.get_user(BUYER))["balance"], 187600)

        # бан
        await self.send(ADMIN, f"/ban {BUYER}")
        calls = await self.send(BUYER, "/start")
        self.assertEqual(calls, [])

        calls = await self.send(20, "/help")   # покупатель BUYER к этому моменту забанен
        self.assertIn("/orders", self.texts(calls))
        self.assertNotIn("/give", self.texts(calls))
        calls = await self.send(ADMIN, "/help")
        self.assertIn("/give", self.texts(calls))

        calls = await self.send(ADMIN, "/stats")
        self.assertIn("Заказов всего: 2", self.texts(calls))


    async def test_docs_prices_and_support_tickets(self):
        from aiogram.types import ReplyKeyboardMarkup
        from bot.handlers.common import BTN_DOCS, BTN_PRICES, BTN_SUPPORT

        calls = await self.send(BUYER, "/start")
        text = self.texts(calls)
        self.assertIn("/terms", text)
        self.assertIn("/privacy", text)
        kb = next(c.reply_markup for c in calls if isinstance(c.reply_markup, ReplyKeyboardMarkup))
        labels = [b.text for row in kb.keyboard for b in row]
        for label in (BTN_PRICES, BTN_SUPPORT, BTN_DOCS):
            self.assertIn(label, labels)

        # документы: соглашение длинное — приходит несколькими сообщениями, все < 4096
        calls = await self.send(BUYER, "/terms")
        msgs = [c.text for c in calls if isinstance(c, SendMessage)]
        self.assertGreaterEqual(len(msgs), 2)
        self.assertTrue(all(len(m) < 4096 for m in msgs))
        self.assertIn("Пользовательское соглашение", msgs[0])
        self.assertIn("@test_shop_bot", "".join(msgs))
        calls = await self.send(BUYER, BTN_DOCS)
        privacy_cb = self.find(self.buttons(calls), "Политика")
        calls = await self.press(BUYER, privacy_cb)
        self.assertIn("Политика конфиденциальности", self.texts(calls))

        calls = await self.send(BUYER, BTN_PRICES)
        self.assertIn("🇷🇺 Россия: 1 месяц — 104 ₽", self.texts(calls))

        # без OPERATOR_NAME публиковать нельзя
        calls = await self.send(ADMIN, "/publish")
        self.assertIn("OPERATOR_NAME", self.texts(calls))

        # обращение в поддержку
        calls = await self.send(BUYER, BTN_SUPPORT)
        self.assertIn("@support", self.texts(calls))
        calls = await self.press(BUYER, self.find(self.buttons(calls), "Написать"))
        calls = await self.send(BUYER, "Не работает прокси в заказе 1000")
        header = next(c for c in calls if isinstance(c, SendMessage) and c.chat_id == ADMIN)
        self.assertIn("Обращение #1", header.text)
        copied = [c for c in calls if isinstance(c, CopyMessage) and c.chat_id == ADMIN]
        self.assertEqual(len(copied), 1)
        self.assertIn("Обращение #1 принято", self.texts(calls))

        # админ отвечает реплаем на копию сообщения пользователя
        link = await self.db._one("SELECT message_id FROM ticket_links ORDER BY message_id DESC")
        calls = await self.send(ADMIN, "Перезапустили, проверьте", reply_to=link["message_id"])
        to_user = [c for c in calls if getattr(c, "chat_id", None) == BUYER]
        self.assertTrue(any(isinstance(c, SendMessage) and "Ответ поддержки" in c.text
                            for c in to_user))
        copy_to_user = next(c for c in to_user if isinstance(c, CopyMessage))
        reply_cb = self.find(self.buttons([copy_to_user]), "Ответить")
        self.assertIn("отправлен", self.texts(calls))

        # пользователь отвечает в то же обращение
        await self.press(BUYER, reply_cb)
        calls = await self.send(BUYER, photo=True)
        self.assertTrue(any(isinstance(c, SendMessage) and c.chat_id == ADMIN
                            and "Обращение #1" in c.text and "новое сообщение" in c.text
                            for c in calls))
        self.assertEqual((await self.db._one("SELECT COUNT(*) c FROM tickets"))["c"], 1)

        # реплай админа на постороннее сообщение не перехватывается как ответ в тикет
        calls = await self.send(ADMIN, "просто текст", reply_to=99999)
        self.assertFalse(any(getattr(c, "chat_id", None) == BUYER for c in calls))

        calls = await self.send(ADMIN, "/tickets")
        self.assertIn("#1", self.texts(calls))

        # удаление персональных данных по запросу
        calls = await self.send(ADMIN, f"/deluser {BUYER}")
        u = await self.db.get_user(BUYER)
        self.assertIsNone(u["username"])
        self.assertEqual((await self.db._one("SELECT COUNT(*) c FROM tickets"))["c"], 0)

    async def test_topup_mentions_terms(self):
        await self.send(BUYER, "/start")
        calls = await self.send(BUYER, BTN_BALANCE)
        await self.press(BUYER, self.find(self.buttons(calls), "Пополнить"))
        calls = await self.send(BUYER, "500")
        self.assertIn("принимаете Пользовательское соглашение", self.texts(calls))


if __name__ == "__main__":
    unittest.main()
