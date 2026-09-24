"""Raw CoinPaprika public (keyless) client. Last-resort listing fallback."""

from __future__ import annotations

from typing import Any

import httpx

from app.data.health import ProviderHealthRegistry
from app.data.http import ProviderHttpClient, RetryPolicy

PROVIDER = "coinpaprika"


class CoinPaprikaClient:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        health: ProviderHealthRegistry,
        *,
        retry: RetryPolicy | None = None,
        rate_per_second: float = 0.5,
        **http_kwargs: Any,
    ) -> None:
        health.register(PROVIDER, "listing_fallback")
        self._base = base_url.rstrip("/")
        self._http = ProviderHttpClient(
            PROVIDER, client, health, rate_per_second=rate_per_second, retry=retry, **http_kwargs
        )

    async def tickers(self) -> Any:
        return await self._http.get_json(self._base + "/tickers", params={"quotes": "USD"})
