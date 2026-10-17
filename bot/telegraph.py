"""Публикация документов на telegra.ph — чтобы дать банку ссылки.

Страницы создаются один раз, дальше редактируются по тем же адресам (ссылки не меняются).
Ключ доступа и адреса страниц хранятся в базе (таблица settings).
"""
from __future__ import annotations

import json
import logging

import aiohttp

log = logging.getLogger(__name__)


class TelegraphError(Exception):
    pass


class Telegraph:
    def __init__(self, proxy: str | None = None, api: str = "https://api.telegra.ph"):
        self.api = api.rstrip("/")
        self.proxy = proxy

    def _session(self) -> aiohttp.ClientSession:
        timeout = aiohttp.ClientTimeout(total=30)
        if self.proxy:
            from aiohttp_socks import ProxyConnector
            return aiohttp.ClientSession(connector=ProxyConnector.from_url(self.proxy),
                                         timeout=timeout)
        return aiohttp.ClientSession(timeout=timeout)

    async def call(self, method: str, **params) -> dict:
        data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v))
                for k, v in params.items() if v is not None}
        try:
            async with self._session() as s, s.post(f"{self.api}/{method}", data=data) as r:
                body = await r.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise TelegraphError(f"нет связи с telegra.ph: {exc}") from exc
        if not body.get("ok"):
            raise TelegraphError(body.get("error") or "неизвестная ошибка")
        return body["result"]


async def publish(db, tg: Telegraph, pages: dict[str, tuple[str, list[dict]]],
                  author: str, author_url: str) -> dict[str, str]:
    """pages: {ключ: (заголовок, содержимое)} → {ключ: url}."""
    token = await db.get_setting("telegraph_token")
    if not token:
        acc = await tg.call("createAccount", short_name=author[:32] or "shop",
                            author_name=author[:128], author_url=author_url)
        token = acc["access_token"]
        await db.set_setting("telegraph_token", token)

    urls = {}
    for key, (title, content) in pages.items():
        path = await db.get_setting(f"telegraph_path_{key}")
        params = dict(access_token=token, title=title[:256], author_name=author[:128],
                      author_url=author_url, content=content)
        page = None
        if path:
            try:
                page = await tg.call(f"editPage/{path}", **params)
            except TelegraphError as exc:
                log.warning("editPage %s: %s — создаю страницу заново", path, exc)
        if page is None:
            page = await tg.call("createPage", **params)
            await db.set_setting(f"telegraph_path_{key}", page["path"])
        await db.set_setting(f"telegraph_url_{key}", page["url"])
        urls[key] = page["url"]
    return urls
