"""Raw alternative.me Fear & Greed client (keyless). Fallback when CMC's index is unavailable.

Note: this is a different index from CoinMarketCap's. The source is always labelled.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.data.health import ProviderHealthRegistry
from app.data.http import ProviderHttpClient, RetryPolicy

PROVIDER = "alternative_me"


class AlternativeMeClient:
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
        health.register(PROVIDER, "sentiment_fallback")
        self._base = base_url.rstrip("/")
        self._http = ProviderHttpClient(
            PROVIDER, client, health, rate_per_second=rate_per_second, retry=retry, **http_kwargs
        )

    async def fear_and_greed(self) -> Any:
        return await self._http.get_json(self._base + "/fng/", params={"limit": 1})
