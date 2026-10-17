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

-- Счета xRocket Pay. id — наш clientInvoiceId; rub — сколько копеек зачислить на баланс.
-- Записи остаются навсегда: это бухгалтерия.
CREATE TABLE IF NOT EXISTS xr_invoices (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    rub        INTEGER NOT NULL,
    amount     TEXT NOT NULL,              -- сумма в крипте (строка, как в API xRocket)
    currency   TEXT NOT NULL,
    invoice_id TEXT,
    url        TEXT,
    status     TEXT NOT NULL DEFAULT 'pending', -- pending / paid / expired / cancelled / failed
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    checked_at REAL NOT NULL DEFAULT 0,
    paid_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_xr_status ON xr_invoices(status, checked_at);

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
        # lower() в SQLite понимает только латиницу — для поиска по русским именам свой
        await self.conn.create_function("pylower", 1, lambda v: v.lower() if isinstance(v, str) else v)
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

    _USERS_WHERE = """WHERE (:q = '' OR pylower(u.username) LIKE :like
                        OR pylower(u.first_name) LIKE :like OR CAST(u.id AS TEXT) LIKE :like)"""

    async def list_users(self, query: str = "", limit: int = 10,
                         offset: int = 0) -> list[aiosqlite.Row]:
        """Пользователи с числом заказов и суммой покупок, новые сверху."""
        q = query.strip().lstrip("@").lower()
        async with self.conn.execute(
                f"""SELECT u.*, COUNT(o.id) AS orders, COALESCE(SUM(o.price), 0) AS spent
                    FROM users u LEFT JOIN orders o ON o.user_id = u.id AND o.status = 'active'
                    {self._USERS_WHERE}
                    GROUP BY u.id ORDER BY u.created_at DESC, u.id DESC
                    LIMIT :limit OFFSET :offset""",
                {"q": q, "like": f"%{q}%", "limit": limit, "offset": offset}) as cur:
            return list(await cur.fetchall())

    async def count_users(self, query: str = "") -> int:
        q = query.strip().lstrip("@").lower()
        async with self.conn.execute(f"SELECT COUNT(*) c FROM users u {self._USERS_WHERE}",
                                     {"q": q, "like": f"%{q}%"}) as cur:
            return int((await cur.fetchone())["c"])

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

    # ---------- Счета xRocket Pay ----------
    async def add_xr_invoice(self, inv_id: str, user_id: int, rub: int, amount: str,
                             currency: str, expires_at: float) -> None:
        import time
        await self.conn.execute(
            """INSERT INTO xr_invoices (id, user_id, rub, amount, currency, created_at, expires_at)
               VALUES (?,?,?,?,?,?,?)""",
            (inv_id, user_id, rub, amount, currency, time.time(), expires_at))
        await self.conn.commit()

    async def set_xr_invoice_link(self, inv_id: str, invoice_id: str, url: str,
                                  expires_at: float) -> None:
        await self.conn.execute(
            "UPDATE xr_invoices SET invoice_id=?, url=?, expires_at=? WHERE id=?",
            (invoice_id or None, url, expires_at, inv_id))
        await self.conn.commit()

    async def get_xr_invoice(self, inv_id: str) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM xr_invoices WHERE id=?", inv_id)

    async def open_xr_invoice(self, user_id: int, rub: int, currency: str,
                              alive_until: float) -> aiosqlite.Row | None:
        """Неоплаченный счёт на ту же сумму и валюту, которому жить ещё долго — отдадим его."""
        return await self._one(
            """SELECT * FROM xr_invoices WHERE user_id=? AND rub=? AND currency=?
               AND status='pending' AND url IS NOT NULL AND expires_at > ?
               ORDER BY created_at DESC LIMIT 1""", user_id, rub, currency, alive_until)

    async def close_xr_invoice(self, inv_id: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE xr_invoices SET status=? WHERE id=? AND status='pending'", (status, inv_id))
        await self.conn.commit()

    async def pending_xr_invoices(self, user_id: int | None = None,
                                  limit: int = 8) -> list[aiosqlite.Row]:
        """Неоплаченные счета — сначала те, что дольше всех не проверялись."""
        import time
        now = time.time()
        if user_id is None:
            rows = await self._all(
                """SELECT * FROM xr_invoices WHERE status='pending' AND url IS NOT NULL
                   ORDER BY checked_at ASC LIMIT ?""", limit)
        else:
            rows = await self._all(
                """SELECT * FROM xr_invoices WHERE status='pending' AND url IS NOT NULL
                   AND user_id=? ORDER BY created_at DESC LIMIT ?""", user_id, limit)
        if rows:
            await self.conn.executemany("UPDATE xr_invoices SET checked_at=? WHERE id=?",
                                        [(now, r["id"]) for r in rows])
            await self.conn.commit()
        return rows

    async def expire_xr_invoices(self, before: float) -> None:
        """Подстраховка: давно истёкшие счета, которые не удалось сверить, закрываем."""
        await self.conn.execute(
            "UPDATE xr_invoices SET status='expired' WHERE status='pending' AND expires_at < ?",
            (before,))
        await self.conn.commit()

    async def credit_xr_invoice(self, inv_id: str) -> dict | None:
        """Отмечает счёт оплаченным и зачисляет рубли — одной транзакцией и ровно один раз."""
        import time
        try:
            cur = await self.conn.execute(
                "UPDATE xr_invoices SET status='paid', paid_at=? WHERE id=? AND status != 'paid'",
                (time.time(), inv_id))
            if cur.rowcount != 1:
                await self.conn.rollback()
                return None
            row = await self._one("SELECT * FROM xr_invoices WHERE id=?", inv_id)
            await self.conn.execute("UPDATE users SET balance = balance + ? WHERE id=?",
                                    (row["rub"], row["user_id"]))
            await self.conn.execute(
                "INSERT INTO transactions (user_id, amount, kind, ref, created_at) VALUES (?,?,?,?,?)",
                (row["user_id"], row["rub"], "topup", f"xrocket:{inv_id}", now_iso()))
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        bal = await self._one("SELECT balance FROM users WHERE id=?", row["user_id"])
        return {"id": inv_id, "user_id": row["user_id"], "rub": row["rub"],
                "amount": row["amount"], "currency": row["currency"],
                "balance": int(bal["balance"]) if bal else 0}

    async def xr_stats(self) -> dict[str, int]:
        row = await self._one(
            "SELECT COUNT(*) c, COALESCE(SUM(rub),0) s FROM xr_invoices WHERE status='paid'")
        return {"count": row["c"], "rub": row["s"]}

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
