from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name} (см. .env.example)")
    return value


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_key: str
    api_base: str
    admin_ids: frozenset[int]
    markup_percent: Decimal
    db_path: str
    shop_name: str
    support_contact: str
    payment_info: str
    min_topup_rub: int
    low_balance_rub: Decimal
    tz: ZoneInfo
    telegram_proxy: str | None = None
    steelproxy_proxy: str | None = None


PROXY_SCHEMES = ("socks5://", "http://")


def _proxy(name: str) -> str | None:
    """Прокси из .env: socks5://user:pass@host:port или http://user:pass@host:port."""
    value = os.getenv(name, "").strip()
    if not value:
        return None
    if "://" not in value:
        raise RuntimeError(
            f"{name}: укажите тип прокси перед адресом — socks5://login:pass@host:port "
            "или http://login:pass@host:port")
    if value.startswith("socks5h://"):
        value = "socks5://" + value[len("socks5h://"):]
    if not value.startswith(PROXY_SCHEMES):
        raise RuntimeError(f"{name}: схема прокси должна быть одной из {', '.join(PROXY_SCHEMES)}")
    return value


def hide_password(url: str) -> str:
    """socks5://user:pass@host:port → socks5://user:***@host:port (для журнала)."""
    return re.sub(r"(://[^:/@]+:).+@", r"\1***@", url)


def load_config() -> Config:
    admins = frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    return Config(
        bot_token=_required("BOT_TOKEN"),
        api_key=_required("STEELPROXY_API_KEY"),
        api_base=os.getenv("STEELPROXY_API_BASE", "https://api.steelproxy.com/v1").rstrip("/"),
        admin_ids=admins,
        markup_percent=Decimal(os.getenv("MARKUP_PERCENT", "30")),
        db_path=os.getenv("DB_PATH", "data/shop.db"),
        shop_name=os.getenv("SHOP_NAME", "Proxy Shop"),
        support_contact=os.getenv("SUPPORT_CONTACT", "@support"),
        payment_info=os.getenv(
            "PAYMENT_INFO", "Реквизиты для оплаты уточните в поддержке."
        ).replace("\\n", "\n"),
        min_topup_rub=int(os.getenv("MIN_TOPUP_RUB", "100")),
        low_balance_rub=Decimal(os.getenv("LOW_BALANCE_RUB", "1000")),
        tz=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        telegram_proxy=_proxy("TELEGRAM_PROXY"),
        steelproxy_proxy=_proxy("STEELPROXY_PROXY"),
    )
