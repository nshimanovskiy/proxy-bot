"""Клиент API реселлера Steel Proxy (https://api.steelproxy.com/docs)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Тексты для покупателя — как рекомендует документация.
CUSTOMER_MESSAGES = {
    402: "Внутренняя ошибка. Обратитесь к администратору магазина.",
    404: "Товар недоступен.",
    409: "Товара нет в наличии.",
    502: "Попробуйте ещё раз через минуту.",
}
DEFAULT_CUSTOMER_MESSAGE = "Внутренняя ошибка. Обратитесь к администратору магазина."

# Сроки «на несколько месяцев», доступные при наличии тарифа на 30 дней.
MULTI_MONTH_TERMS = (60, 90, 180)
EXTEND_MONTHS = (1, 2, 3, 6)


class SteelProxyError(Exception):
    """Ответ API с кодом не 2xx. Деньги у Steel Proxy в этом случае не списаны."""

    def __init__(self, status: int, message: str, data: Any = None):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message
        self.data = data

    @property
    def customer_message(self) -> str:
        return CUSTOMER_MESSAGES.get(self.status, DEFAULT_CUSTOMER_MESSAGE)


class SteelProxyUnknownResult(Exception):
    """Сеть оборвалась после отправки запроса — неизвестно, создан ли заказ."""


class SteelProxyClient:
    def __init__(self, api_key: str, base_url: str = "https://api.steelproxy.com/v1",
                 transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, safe_to_retry: bool = True,
                       **kwargs: Any) -> Any:
        try:
            resp = await self._client.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Запрос точно не дошёл до сервера.
            raise SteelProxyError(502, f"Нет связи с API: {exc}") from exc
        except httpx.HTTPError as exc:
            if safe_to_retry:
                raise SteelProxyError(502, f"Сбой сети: {exc}") from exc
            raise SteelProxyUnknownResult(str(exc)) from exc

        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {"data": body}

        if resp.status_code >= 300:
            raise SteelProxyError(
                resp.status_code, body.get("message") or resp.reason_phrase, body.get("data")
            )
        return body.get("data")

    # ---------- Каталог ----------
    async def proxy_types(self) -> list[dict]:
        return await self._request("GET", "/proxy-types")

    async def locations(self, proxy_type: str | None = None) -> list[dict]:
        params = {"proxy_type": proxy_type} if proxy_type else None
        return await self._request("GET", "/locations", params=params)

    async def terms(self, proxy_type: str | None = None) -> list[int]:
        params = {"proxy_type": proxy_type} if proxy_type else None
        return await self._request("GET", "/terms", params=params)

    async def prices(self, proxy_type: str | None = None,
                     country_code: str | None = None) -> list[dict]:
        params = {k: v for k, v in
                  {"proxy_type": proxy_type, "country_code": country_code}.items() if v}
        return await self._request("GET", "/prices", params=params or None)

    # ---------- Заказы ----------
    async def create_order(self, proxy_type: str, node_id: int, term_days: int,
                           quantity: int, buyer_reference: str | None = None) -> dict:
        payload: dict[str, Any] = {
            "proxy_type": proxy_type,
            "node_id": node_id,
            "term_days": term_days,
            "quantity": quantity,
        }
        if buyer_reference:
            payload["buyer_reference"] = buyer_reference
        return await self._request("POST", "/orders", json=payload, safe_to_retry=False)

    async def get_order(self, order_id: int) -> dict:
        return await self._request("GET", f"/orders/{order_id}")

    async def list_orders(self, page: int = 1, page_size: int = 20) -> dict:
        return await self._request("GET", "/orders",
                                   params={"page": page, "page_size": page_size})

    async def extend_order(self, order_id: int, months: int) -> dict:
        return await self._request("POST", f"/orders/{order_id}/extend",
                                   json={"months": months}, safe_to_retry=False)

    async def find_order_by_reference(self, reference: str, pages: int = 3) -> dict | None:
        """Ищет заказ по buyer_reference среди последних заказов (для сверки после сбоя сети)."""
        for page in range(1, pages + 1):
            data = await self.list_orders(page=page, page_size=100)
            for item in data.get("items", []):
                if item.get("buyer_reference") == reference:
                    return await self.get_order(int(item["order_id"]))
            if page * 100 >= int(data.get("total", 0)):
                break
        return None
