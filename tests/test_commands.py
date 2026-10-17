"""Список команд совпадает с тем, что бот реально обрабатывает."""
import unittest

from aiogram.filters import Command

from bot.commands import ADMIN_COMMANDS, USER_COMMANDS, admin_text, help_text, menu
from bot.handlers import admin, info, user


def registered(router) -> set[str]:
    out = set()
    for handler in router.message.handlers:
        for f in handler.filters or []:
            if isinstance(f.callback, Command):
                out |= {c if isinstance(c, str) else c.pattern for c in f.callback.commands}
    return out


class CommandsListTest(unittest.TestCase):
    def test_lists_match_handlers(self):
        user_cmds = {c for c, _, _ in USER_COMMANDS}
        admin_cmds = {c for c, _, _ in ADMIN_COMMANDS}
        self.assertFalse(user_cmds & admin_cmds)
        self.assertEqual(registered(user.router) | registered(info.router) | {"start"}, user_cmds)
        self.assertEqual(registered(admin.router), admin_cmds)

    def test_texts(self):
        self.assertIn("/prices — Цены и тарифы", help_text(False))
        self.assertNotIn("/give", help_text(False))
        self.assertIn("/give &lt;id|@username&gt; &lt;сумма&gt;", help_text(True))
        self.assertIn("реплай", admin_text())

    def test_menu_is_valid_for_telegram(self):
        for cmd in menu(USER_COMMANDS + ADMIN_COMMANDS):
            self.assertRegex(cmd.command, r"^[a-z0-9_]{1,32}$")
            self.assertTrue(0 < len(cmd.description) <= 256)


if __name__ == "__main__":
    unittest.main()
