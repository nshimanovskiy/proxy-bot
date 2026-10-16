"""Деньги: цены с наценкой, покупка, возвраты, сверка после обрыва связи, продление."""
import asyncio
import unittest
from decimal import Decimal
from unittest import mock

from bot.db import Database
from bot.shop import Shop, ShopError

from .fakes import FakeAPI

_real_sleep = asyncio.sleep


class ShopTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.connect()
        self.api = FakeAPI()
        self.shop = Shop(self.db, self.api, Decimal(30))
        await self.db.upsert_user(10, "buyer", "Buyer")
        self.loc = await self.shop.location("residential", 5)

    async def asyncTearDown(self):
        await self.db.close()

    async def balance(self):
        return (await self.db.get_user(10))["balance"]

    async def test_terms_and_prices(self):
        self.assertEqual(self.loc.term_options, [1, 7, 14, 30, 60, 90, 180])
        # 47.50 × 1.3 = 61.75 → округление вверх до рубля
        self.assertEqual(await self.shop.unit_sell_price("residential", 5, 7), 6200)
        # 90 дней = 3 × месячный тариф (100 ₽) × 1.3
        self.assertEqual(await self.shop.unit_sell_price("residential", 5, 90), 39000)
        # тарифа на 14 дней нет в прайсе
        self.assertIsNone(await self.shop.unit_sell_price("residential", 5, 14))

    async def test_not_enough_money(self):
        with self.assertRaises(ShopError):
            await self.shop.purchase(10, self.loc, 7, 1)
        self.assertEqual(await self.balance(), 0)
        self.assertEqual(self.api.orders, {})

    async def test_purchase_charges_and_saves_cost(self):
        await self.db.change_balance(10, 100000, "admin")
        res = await self.shop.purchase(10, self.loc, 7, 2)
        self.assertEqual(res.charged, 12400)
        self.assertEqual(await self.balance(), 100000 - 12400)
        order = await self.db.get_order(res.local_id)
        self.assertEqual(order["status"], "active")
        self.assertEqual(order["cost"], 9500)
        self.assertEqual(order["sp_order_id"], res.order["order_id"])

    async def test_supplier_errors_refund(self):
        await self.db.change_balance(10, 100000, "admin")
        for code in ("402", "409", "502"):
            self.api.fail = code
            with self.assertRaises(ShopError) as ctx:
                await self.shop.purchase(10, self.loc, 7, 1)
            self.assertEqual(ctx.exception.status, int(code))
        self.assertEqual(await self.balance(), 100000)

    async def test_network_drop_after_create_is_reconciled(self):
        await self.db.change_balance(10, 100000, "admin")
        self.api.fail = "net-after-create"
        with mock.patch("bot.shop.asyncio.sleep", lambda *_: _real_sleep(0)):
            res = await self.shop.purchase(10, self.loc, 7, 1)
        self.assertFalse(res.pending_check)
        self.assertEqual((await self.db.get_order(res.local_id))["status"], "active")
        self.assertEqual(await self.balance(), 100000 - 6200)

    async def test_unknown_order_refund_via_check(self):
        await self.db.change_balance(10, 100000, "admin")
        self.api.fail = "net-after-create"

        async def nothing(ref, pages=3):
            return None
        with mock.patch("bot.shop.asyncio.sleep", lambda *_: _real_sleep(0)), \
             mock.patch.object(self.api, "find_order_by_reference", nothing):
            res = await self.shop.purchase(10, self.loc, 7, 1)
            self.assertTrue(res.pending_check)
            self.assertEqual(await self.balance(), 100000 - 6200)
            self.assertEqual(await self.shop.resolve_unknown(res.local_id), "refunded")
        self.assertEqual(await self.balance(), 100000)
        # повторная сверка ничего не делает
        self.assertEqual(await self.shop.resolve_unknown(res.local_id), "nothing")

    async def test_extend(self):
        await self.db.change_balance(10, 100000, "admin")
        res = await self.shop.purchase(10, self.loc, 7, 2)
        row = await self.db.get_order(res.local_id)
        api_res, charged, bal = await self.shop.extend(row, 3)
        self.assertEqual(charged, 13000 * 2 * 3)
        self.assertEqual(bal, 100000 - 12400 - 78000)
        self.assertEqual((await self.db.get_order(res.local_id))["expires_at"],
                         api_res["expires_at"])

    async def test_extend_rejects_inactive(self):
        await self.db.change_balance(10, 100000, "admin")
        self.api.fail = "502"
        with self.assertRaises(ShopError):
            await self.shop.purchase(10, self.loc, 7, 1)
        failed = await self.db.get_order(1)
        with self.assertRaises(ShopError):
            await self.shop.extend(failed, 1)
        self.assertEqual(await self.balance(), 100000)


if __name__ == "__main__":
    unittest.main()
