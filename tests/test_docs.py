"""Документы для банка: тексты, разбиение на сообщения, цены, публикация на telegra.ph."""
import html
import json
import unittest
from decimal import Decimal

from aiohttp import web

from bot import docs, telegraph
from bot.db import Database
from bot.shop import Shop

from .fakes import FakeAPI

CTX = docs.DocContext(shop_name="Test Shop", bot_username="test_shop_bot",
                      operator_name="ИП Иванов Иван Иванович", operator_inn="660000000000",
                      support_contact="@help_me", support_email="help@example.org",
                      min_topup_rub=100)


class DocsTextTest(unittest.TestCase):
    def test_operator_and_contacts_in_both_documents(self):
        for title, blocks in (docs.terms(CTX), docs.privacy(CTX)):
            text = "\n".join(t for _, t in blocks)
            self.assertIn("ИП Иванов Иван Иванович, ИНН 660000000000", text, title)
            self.assertIn("@help_me", text)
            self.assertIn("help@example.org", text)
            self.assertIn("@test_shop_bot", text)
            self.assertIn(f"Редакция от {docs.DOCS_REVISION}", text)
            self.assertNotIn("[укажите", text)

    def test_missing_data_is_reported(self):
        empty = docs.DocContext("S", "b", "", "", "", "", 100)
        self.assertEqual(len(empty.missing()), 2)
        self.assertIn("[укажите OPERATOR_NAME", "\n".join(t for _, t in docs.terms(empty)[1]))
        self.assertEqual(CTX.missing(), [])
        # группа/ссылка вместо @username не считается контактом
        group = docs.DocContext("S", "b", "ИП", "", "https://t.me/+abc", "", 100)
        self.assertEqual(len(group.missing()), 1)

    def test_chunks_fit_telegram_and_keep_everything(self):
        title, blocks = docs.terms(CTX)
        chunks = docs.to_html_chunks(title, blocks, footer="низ", limit=1500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1500 for c in chunks))
        joined = "\n".join(chunks)
        for _, text in blocks:
            self.assertIn(html.escape(text), joined)
        self.assertTrue(joined.endswith("низ"))

    def test_html_is_escaped(self):
        chunks = docs.to_html_chunks("A<b>", [("p", "x < y & z")])
        self.assertIn("A&lt;b&gt;", chunks[0])
        self.assertIn("x &lt; y &amp; z", chunks[0])


class PricesTest(unittest.IsolatedAsyncioTestCase):
    async def test_prices_from_catalog(self):
        db = Database(":memory:")
        await db.connect()
        shop = Shop(db, FakeAPI(), Decimal(30))
        title, blocks = await docs.prices(shop, CTX)
        text = "\n".join(t for _, t in blocks)
        self.assertEqual(title, "Цены и тарифы")
        self.assertIn("Серверные прокси", text)
        self.assertIn("🇷🇺 Россия: 1 месяц — 104 ₽; 2 месяца — 208 ₽", text)
        self.assertIn("🇺🇸 США: 7 дней — 62 ₽; 1 месяц — 130 ₽", text)
        self.assertIn("минимальная сумма — 100 ₽", text)
        self.assertNotIn("14 дней", text)   # нет цены — нет и строки
        await db.close()


class TelegraphTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.pages = {}
        self.fail_edit = False
        app = web.Application()

        async def handler(request):
            method = request.match_info["method"]
            form = dict(await request.post())
            self.calls.append((method, form))
            if method == "createAccount":
                return web.json_response({"ok": True, "result": {"access_token": "tok"}})
            assert form["access_token"] == "tok"
            json.loads(form["content"])
            if method == "createPage":
                path = f"Page-{len(self.pages) + 1}"
                self.pages[path] = form["title"]
                return web.json_response({"ok": True, "result": {
                    "path": path, "url": f"https://telegra.ph/{path}"}})
            if self.fail_edit:
                return web.json_response({"ok": False, "error": "PAGE_NOT_FOUND"})
            path = method.split("/", 1)[1]
            return web.json_response({"ok": True, "result": {
                "path": path, "url": f"https://telegra.ph/{path}"}})

        app.router.add_post("/{method:.*}", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.tg = telegraph.Telegraph(api=f"http://127.0.0.1:{port}")
        self.db = Database(":memory:")
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        await self.runner.cleanup()

    async def test_create_then_edit_same_urls(self):
        pages = {k: (t, docs.to_telegraph(b)) for k, (t, b) in
                 {"terms": docs.terms(CTX), "privacy": docs.privacy(CTX)}.items()}
        urls1 = await telegraph.publish(self.db, self.tg, pages, "Test Shop", "https://t.me/x")
        self.assertEqual([m for m, _ in self.calls], ["createAccount", "createPage", "createPage"])
        self.calls.clear()
        urls2 = await telegraph.publish(self.db, self.tg, pages, "Test Shop", "https://t.me/x")
        self.assertEqual([m for m, _ in self.calls], ["editPage/Page-1", "editPage/Page-2"])
        self.assertEqual(urls1, urls2)
        self.assertEqual(await self.db.get_setting("telegraph_url_terms"), urls1["terms"])

    async def test_recreates_page_if_edit_fails(self):
        pages = {"prices": ("Цены", [{"tag": "p", "children": ["x"]}])}
        await telegraph.publish(self.db, self.tg, pages, "S", "https://t.me/x")
        self.fail_edit = True
        self.calls.clear()
        urls = await telegraph.publish(self.db, self.tg, pages, "S", "https://t.me/x")
        self.assertEqual([m for m, _ in self.calls], ["editPage/Page-1", "createPage"])
        self.assertEqual(urls["prices"], "https://telegra.ph/Page-2")

    async def test_unreachable(self):
        tg = telegraph.Telegraph(api="http://127.0.0.1:1")
        with self.assertRaises(telegraph.TelegraphError):
            await tg.call("createAccount", short_name="x")


if __name__ == "__main__":
    unittest.main()
