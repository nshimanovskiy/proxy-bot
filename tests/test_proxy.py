"""Работа через прокси: бот ходит в Telegram и API Steel Proxy через SOCKS5/HTTP-прокси.
Поднимаем локальные «Telegram», «Steel Proxy» и мини-прокси с логином и паролем
и проверяем, что запросы действительно идут через прокси."""
import asyncio
import os
import unittest
from unittest import mock

from aiohttp import web
from aiogram.client.telegram import TelegramAPIServer

from bot.config import hide_password, load_config
from bot.main import create_bot
from bot.steelproxy import SteelProxyClient

TOKEN = "777:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
LOGIN, PASSWORD = "user", "secret"


async def _pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


class MiniProxy:
    """SOCKS5 (логин/пароль) и HTTP CONNECT/forward-прокси в одном. Считает соединения."""

    def __init__(self):
        self.connections: list[tuple[str, str, int]] = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, r, w):
        first = await r.readexactly(1)
        if first == b"\x05":
            await self._socks5(r, w)
        else:
            await self._http(first, r, w)

    async def _socks5(self, r, w):
        n = (await r.readexactly(1))[0]
        methods = await r.readexactly(n)
        if 2 not in methods:
            w.write(b"\x05\xff")
            w.close()
            return
        w.write(b"\x05\x02")
        await r.readexactly(1)
        user = (await r.readexactly((await r.readexactly(1))[0])).decode()
        pwd = (await r.readexactly((await r.readexactly(1))[0])).decode()
        if (user, pwd) != (LOGIN, PASSWORD):
            w.write(b"\x01\x01")
            w.close()
            return
        w.write(b"\x01\x00")
        _, cmd, _, atyp = await r.readexactly(4)
        if atyp == 1:
            host = ".".join(map(str, await r.readexactly(4)))
        elif atyp == 3:
            host = (await r.readexactly((await r.readexactly(1))[0])).decode()
        else:
            raise AssertionError("IPv6 не нужен в тесте")
        port = int.from_bytes(await r.readexactly(2), "big")
        self.connections.append(("socks5", host, port))
        tr, tw = await asyncio.open_connection(host, port)
        w.write(b"\x05\x00\x00\x01" + bytes(4) + b"\x00\x00")
        await asyncio.gather(_pipe(r, tw), _pipe(tr, w))

    async def _http(self, first, r, w):
        head = first + await r.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        method, target, _ = lines[0].split(" ")
        headers = {k.lower(): v for k, v in (l.split(": ", 1) for l in lines[1:] if ": " in l)}
        import base64
        expected = "Basic " + base64.b64encode(f"{LOGIN}:{PASSWORD}".encode()).decode()
        if headers.get("proxy-authorization") != expected:
            w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
            w.close()
            return
        if method == "CONNECT":
            host, port = target.rsplit(":", 1)
            self.connections.append(("http", host, int(port)))
            tr, tw = await asyncio.open_connection(host, int(port))
            w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:  # прокси для обычного http:// — пересылаем запрос как есть
            from urllib.parse import urlsplit
            u = urlsplit(target)
            self.connections.append(("http", u.hostname, u.port))
            tr, tw = await asyncio.open_connection(u.hostname, u.port)
            rest = [l for l in lines[1:] if not l.lower().startswith("proxy-")]
            path = u.path + (f"?{u.query}" if u.query else "")
            tw.write(f"{method} {path} HTTP/1.1\r\n".encode() + "\r\n".join(rest).encode())
        await asyncio.gather(_pipe(r, tw), _pipe(tr, w))


class ProxyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()

        async def get_me(request):
            return web.json_response({"ok": True, "result": {
                "id": 777, "is_bot": True, "first_name": "shop", "username": "shop_bot"}})

        async def proxy_types(request):
            assert request.headers["X-API-Key"] == "sp_test"
            return web.json_response({"status": 200, "message": "",
                                      "data": [{"code": "server", "title": "Серверные"}]})

        app.router.add_post(f"/bot{TOKEN}/getMe", get_me)
        app.router.add_get("/v1/proxy-types", proxy_types)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.target = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.proxy = MiniProxy()
        self.proxy_port = await self.proxy.start()

    async def asyncTearDown(self):
        await self.proxy.stop()
        await self.runner.cleanup()

    def env(self, **extra):
        base = {"BOT_TOKEN": TOKEN, "STEELPROXY_API_KEY": "sp_test", "ADMIN_IDS": "1"}
        return mock.patch.dict(os.environ, {**base, **extra}, clear=True)

    async def _telegram_through(self, scheme):
        url = f"{scheme}://{LOGIN}:{PASSWORD}@127.0.0.1:{self.proxy_port}"
        with self.env(TELEGRAM_PROXY=url):
            cfg = load_config()
        bot = create_bot(cfg)
        bot.session.api = TelegramAPIServer.from_base(self.target)
        try:
            me = await bot.get_me()
        finally:
            await bot.session.close()
        self.assertEqual(me.username, "shop_bot")
        self.assertIn((scheme, "127.0.0.1", int(self.target.rsplit(":", 1)[1])),
                      self.proxy.connections)

    async def test_telegram_via_socks5(self):
        await self._telegram_through("socks5")

    async def test_telegram_via_http(self):
        await self._telegram_through("http")

    async def test_telegram_without_proxy(self):
        with self.env():
            cfg = load_config()
        self.assertIsNone(cfg.telegram_proxy)
        bot = create_bot(cfg)
        bot.session.api = TelegramAPIServer.from_base(self.target)
        try:
            await bot.get_me()
        finally:
            await bot.session.close()
        self.assertEqual(self.proxy.connections, [])

    async def test_steelproxy_api_via_proxy(self):
        for scheme in ("socks5", "http"):
            url = f"{scheme}://{LOGIN}:{PASSWORD}@127.0.0.1:{self.proxy_port}"
            with self.env(STEELPROXY_PROXY=url):
                cfg = load_config()
            api = SteelProxyClient(cfg.api_key, self.target + "/v1", proxy=cfg.steelproxy_proxy)
            try:
                types = await api.proxy_types()
            finally:
                await api.close()
            self.assertEqual(types[0]["code"], "server")
            self.assertEqual(self.proxy.connections[-1][0], scheme)

    def test_config_validation(self):
        with self.env(TELEGRAM_PROXY="socks5h://u:p@h:1"):
            self.assertEqual(load_config().telegram_proxy, "socks5://u:p@h:1")
        for bad in ("u:p@h:1", "socks4://h:1", "ftp://h:1"):
            with self.env(TELEGRAM_PROXY=bad), self.assertRaises(RuntimeError):
                load_config()
        self.assertEqual(hide_password("socks5://user:s3cr@t@h:1"), "socks5://user:***@h:1")
        self.assertEqual(hide_password("http://h:1"), "http://h:1")


if __name__ == "__main__":
    unittest.main()
