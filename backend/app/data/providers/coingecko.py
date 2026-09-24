"""Raw CoinGecko public (keyless) client. Low rate limits, so calls are spaced out."""

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
        rate_per_second: float = 0.2,
        **http_kwargs: Any,
    ) -> None:
        health.register(PROVIDER, "listing_fallback")
        self._base = base_url.rstrip("/")
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
        return await self._http.get_json(self._base + "/coins/markets", params=params)

    async def global_data(self) -> Any:
        return await self._http.get_json(self._base + "/global")
