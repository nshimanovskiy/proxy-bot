"""SQLite-хранилище: пользователи, балансы (в копейках), заказы, пополнения."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,           -- Telegram user id
    username    TEXT,
    first_name  TEXT,
    balance     INTEGER NOT NULL DEFAULT 0,    -- копейки
    banned      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    sp_order_id    INTEGER,                    -- номер заказа в Steel Proxy
    status         TEXT NOT NULL,              -- pending / active / failed / unknown / refunded
    proxy_type     TEXT NOT NULL,
    node_id        INTEGER NOT NULL,
    country_code   TEXT,
    location_title TEXT,
    term_days      INTEGER NOT NULL,
    quantity       INTEGER NOT NULL,
    cost           INTEGER NOT NULL DEFAULT 0, -- закупка, копейки
    price          INTEGER NOT NULL,           -- списано с покупателя, копейки
    created_at     TEXT NOT NULL,
    expires_at     TEXT,
    reminded       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);

CREATE TABLE IF NOT EXISTS transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,               -- копейки, + пополнение / - списание
    kind       TEXT NOT NULL,                  -- topup / order / refund / extend / admin
    ref        TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- какие сообщения у админов относятся к какому обращению (чтобы отвечать реплаем)
CREATE TABLE IF NOT EXISTS ticket_links (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    ticket_id  INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS topups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,               -- копейки
    status     TEXT NOT NULL DEFAULT 'pending',-- pending / approved / declined
    created_at TEXT NOT NULL,
    decided_by INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_kop(rub: Decimal | str | int | float) -> int:
    return int((Decimal(str(rub)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_rub(kop: int) -> str:
    rub = Decimal(kop) / 100
    s = f"{rub:,.2f}".replace(",", " ")
    if s.endswith(".00"):
        s = s[:-3]
    return f"{s} ₽"


class InsufficientFunds(Exception):
    pass


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _one(self, sql: str, *args: Any) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, *args: Any) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, args) as cur:
            return list(await cur.fetchall())

    # ---------- Пользователи ----------
    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        await self.conn.execute(
            """INSERT INTO users (id, username, first_name, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET username=excluded.username,
                                             first_name=excluded.first_name""",
            (user_id, username, first_name, now_iso()),
        )
        await self.conn.commit()

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM users WHERE id=?", user_id)

    async def find_user(self, query: str) -> aiosqlite.Row | None:
        query = query.strip().lstrip("@")
        if query.isdigit():
            return await self.get_user(int(query))
        return await self._one("SELECT * FROM users WHERE lower(username)=lower(?)", query)

    async def set_banned(self, user_id: int, banned: bool) -> None:
        await self.conn.execute("UPDATE users SET banned=? WHERE id=?", (int(banned), user_id))
        await self.conn.commit()

    async def all_user_ids(self) -> list[int]:
        return [r["id"] for r in await self._all("SELECT id FROM users WHERE banned=0")]

    # ---------- Баланс ----------
    async def change_balance(self, user_id: int, amount: int, kind: str,
                             ref: str | None = None, allow_negative: bool = False) -> int:
        """Атомарно меняет баланс. При списании без allow_negative проверяет остаток."""
        if amount < 0 and not allow_negative:
            cur = await self.conn.execute(
                "UPDATE users SET balance = balance + ? WHERE id=? AND balance >= ?",
                (amount, user_id, -amount),
            )
        else:
            cur = await self.conn.execute(
                "UPDATE users SET balance = balance + ? WHERE id=?", (amount, user_id)
            )
        if cur.rowcount != 1:
            await self.conn.rollback()
            raise InsufficientFunds()
        await self.conn.execute(
            "INSERT INTO transactions (user_id, amount, kind, ref, created_at) VALUES (?,?,?,?,?)",
            (user_id, amount, kind, ref, now_iso()),
        )
        await self.conn.commit()
        row = await self._one("SELECT balance FROM users WHERE id=?", user_id)
        return int(row["balance"])

    # ---------- Заказы ----------
    async def create_local_order(self, **f: Any) -> int:
        cur = await self.conn.execute(
            """INSERT INTO orders (user_id, status, proxy_type, node_id, country_code,
                   location_title, term_days, quantity, price, created_at)
               VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f["user_id"], f["proxy_type"], f["node_id"], f["country_code"],
             f["location_title"], f["term_days"], f["quantity"], f["price"], now_iso()),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def update_order(self, order_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.conn.execute(f"UPDATE orders SET {cols} WHERE id=?",
                                (*fields.values(), order_id))
        await self.conn.commit()

    async def get_order(self, order_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM orders WHERE id=?", order_id)

    async def user_orders(self, user_id: int, limit: int, offset: int) -> list[aiosqlite.Row]:
        return await self._all(
            """SELECT * FROM orders WHERE user_id=? AND status IN ('active','unknown','refunded')
               ORDER BY id DESC LIMIT ? OFFSET ?""", user_id, limit, offset)

    async def count_user_orders(self, user_id: int) -> int:
        row = await self._one(
            "SELECT COUNT(*) c FROM orders WHERE user_id=? AND status IN ('active','unknown','refunded')",
            user_id)
        return int(row["c"])

    async def orders_to_remind(self, before_iso: str, now: str) -> list[aiosqlite.Row]:
        return await self._all(
            """SELECT * FROM orders WHERE status='active' AND reminded=0
               AND expires_at IS NOT NULL AND expires_at <= ? AND expires_at > ?""",
            before_iso, now)

    # ---------- Пополнения ----------
    async def create_topup(self, user_id: int, amount: int) -> int:
        cur = await self.conn.execute(
            "INSERT INTO topups (user_id, amount, created_at) VALUES (?,?,?)",
            (user_id, amount, now_iso()))
        await self.conn.commit()
        return int(cur.lastrowid)

    async def decide_topup(self, topup_id: int, status: str, admin_id: int) -> aiosqlite.Row | None:
        """Меняет статус только у pending-заявки (защита от двойного нажатия)."""
        cur = await self.conn.execute(
            "UPDATE topups SET status=?, decided_by=? WHERE id=? AND status='pending'",
            (status, admin_id, topup_id))
        await self.conn.commit()
        if cur.rowcount != 1:
            return None
        return await self._one("SELECT * FROM topups WHERE id=?", topup_id)

    # ---------- Настройки ----------
    async def get_setting(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM settings WHERE key=?", key)
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await self.conn.commit()

    # ---------- Обращения в поддержку ----------
    async def create_ticket(self, user_id: int) -> int:
        now = now_iso()
        cur = await self.conn.execute(
            "INSERT INTO tickets (user_id, created_at, updated_at) VALUES (?,?,?)",
            (user_id, now, now))
        await self.conn.commit()
        return int(cur.lastrowid)

    async def get_ticket(self, ticket_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM tickets WHERE id=?", ticket_id)

    async def touch_ticket(self, ticket_id: int, status: str = "open") -> None:
        await self.conn.execute("UPDATE tickets SET status=?, updated_at=? WHERE id=?",
                                (status, now_iso(), ticket_id))
        await self.conn.commit()

    async def link_ticket_message(self, chat_id: int, message_id: int, ticket_id: int) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO ticket_links (chat_id, message_id, ticket_id) VALUES (?,?,?)",
            (chat_id, message_id, ticket_id))
        await self.conn.commit()

    async def ticket_by_message(self, chat_id: int, message_id: int) -> aiosqlite.Row | None:
        return await self._one(
            """SELECT t.* FROM ticket_links l JOIN tickets t ON t.id = l.ticket_id
               WHERE l.chat_id=? AND l.message_id=?""", chat_id, message_id)

    async def open_tickets(self, limit: int = 20) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT * FROM tickets WHERE status='open' ORDER BY updated_at DESC LIMIT ?", limit)

    # ---------- Удаление персональных данных по запросу ----------
    async def forget_user(self, user_id: int) -> None:
        """Удаляет имя, @username и обращения. Заказы и платежи остаются (без имени) для учёта."""
        await self.conn.execute("UPDATE users SET username=NULL, first_name=NULL WHERE id=?",
                                (user_id,))
        await self.conn.execute(
            "DELETE FROM ticket_links WHERE ticket_id IN (SELECT id FROM tickets WHERE user_id=?)",
            (user_id,))
        await self.conn.execute("DELETE FROM tickets WHERE user_id=?", (user_id,))
        await self.conn.commit()

    # ---------- Статистика ----------
    async def stats(self) -> dict[str, int]:
        users = await self._one("SELECT COUNT(*) c, COALESCE(SUM(balance),0) b FROM users")
        orders = await self._one(
            """SELECT COUNT(*) c, COALESCE(SUM(price),0) revenue, COALESCE(SUM(cost),0) cost
               FROM orders WHERE status='active'""")
        today = await self._one(
            """SELECT COUNT(*) c, COALESCE(SUM(price),0) revenue FROM orders
               WHERE status='active' AND created_at >= date('now')""")
        return {
            "users": users["c"], "balances": users["b"],
            "orders": orders["c"], "revenue": orders["revenue"], "cost": orders["cost"],
            "today_orders": today["c"], "today_revenue": today["revenue"],
        }
