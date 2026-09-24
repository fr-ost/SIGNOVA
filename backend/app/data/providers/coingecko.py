"""Raw CoinGecko client. Keyless public API, or a Demo / Pro key (COINGECKO_API_KEY).

Demo keys (the free plan) use api.coingecko.com with the `x-cg-demo-api-key` header and allow
about 30 calls per minute; Pro keys use pro-api.coingecko.com with `x-cg-pro-api-key`. The key
is sent as a header, never in the URL, so it cannot leak into logs.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.data.health import ProviderHealthRegistry
from app.data.http import ProviderHttpClient, RetryPolicy

PROVIDER = "coingecko"


class CoinGeckoClient:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        health: ProviderHealthRegistry,
        *,
        retry: RetryPolicy | None = None,
        rate_per_second: float | None = None,
        headers: dict[str, str] | None = None,
        **http_kwargs: Any,
    ) -> None:
        health.register(PROVIDER, "listing_fallback")
        self._base = base_url.rstrip("/")
        self._headers = dict(headers or {})
        self.keyed = bool(self._headers)
        if rate_per_second is None:
            rate_per_second = 0.45 if self.keyed else 0.2  # Demo plan: 30/min; keyless: much lower
        self._http = ProviderHttpClient(
            PROVIDER, client, health, rate_per_second=rate_per_second, retry=retry, **http_kwargs
        )

    async def coins_markets(self, per_page: int = 100, category: str | None = None) -> Any:
        params: dict[str, Any] = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": 1,
            "sparkline": "false",
            "price_change_percentage": "1h,24h,7d",
        }
        if category:
            params["category"] = category
        return await self._http.get_json(self._base + "/coins/markets", params=params, headers=self._headers)

    async def global_data(self) -> Any:
        return await self._http.get_json(self._base + "/global", headers=self._headers)
