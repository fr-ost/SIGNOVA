"""Raw Kraken public REST client, used as the fallback spot market source.

Kraken wraps every response in {"error": [...], "result": {...}}. Errors are raised as
ProviderError subclasses so the router can fail over cleanly.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.data.health import ProviderHealthRegistry
from app.data.http import (
    ProviderBadResponse,
    ProviderClientError,
    ProviderError,
    ProviderHttpClient,
    ProviderRateLimited,
    RetryPolicy,
)

PROVIDER = "kraken"


class KrakenRestClient:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        health: ProviderHealthRegistry,
        *,
        retry: RetryPolicy | None = None,
        rate_per_second: float = 1.0,
        **http_kwargs: Any,
    ) -> None:
        health.register(PROVIDER, "spot_market_fallback")
        self._base = base_url.rstrip("/")
        self._http = ProviderHttpClient(
            PROVIDER, client, health, rate_per_second=rate_per_second, retry=retry, **http_kwargs
        )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = await self._http.get_json(self._base + path, params=params)
        if not isinstance(payload, dict) or "result" not in payload:
            raise ProviderBadResponse(PROVIDER, "unexpected response envelope")
        errors = payload.get("error") or []
        if errors:
            text = "; ".join(str(e) for e in errors)
            if "Rate limit" in text or "Too many requests" in text:
                raise ProviderRateLimited(PROVIDER, text, retry_after=None)
            if text.startswith("EQuery") or text.startswith("EGeneral:Invalid"):
                raise ProviderClientError(PROVIDER, text)
            raise ProviderError(PROVIDER, text)
        result = payload["result"]
        if not isinstance(result, dict):
            raise ProviderBadResponse(PROVIDER, "result is not an object")
        return result

    async def asset_pairs(self) -> dict[str, Any]:
        return await self._get("/0/public/AssetPairs")

    async def ticker(self, pairs: list[str]) -> dict[str, Any]:
        return await self._get("/0/public/Ticker", {"pair": ",".join(sorted(set(pairs)))})

    async def ohlc(self, pair: str, interval_minutes: int) -> dict[str, Any]:
        return await self._get("/0/public/OHLC", {"pair": pair, "interval": interval_minutes})

    async def depth(self, pair: str, count: int = 100) -> dict[str, Any]:
        return await self._get("/0/public/Depth", {"pair": pair, "count": count})
