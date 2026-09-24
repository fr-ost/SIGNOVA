"""Raw Binance public Spot REST client (no credentials, market data only).

Several base URLs are tried in order (api.binance.com, api-gcp.binance.com and the
market-data-only data-api.binance.vision). A base that answers HTTP 451 (restricted
server location) or 401/403 (access denied) is parked for an hour and the next one is used. The used request
weight header is tracked, and requests are paused near the per-minute weight limit.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.data.health import ProviderHealthRegistry
from app.data.http import (
    CircuitBreaker,
    ProviderClientError,
    ProviderError,
    ProviderHttpClient,
    ProviderRateLimited,
    ProviderRestricted,
    ProviderUnavailable,
    RetryPolicy,
)

log = logging.getLogger(__name__)

PROVIDER = "binance"
_RESTRICTED_PARK_SECONDS = 3600.0
_WEIGHT_PAUSE_FRACTION = 0.9


@dataclass
class _BaseEndpoint:
    url: str
    http: ProviderHttpClient
    restricted_until: float = 0.0


class BinanceRestClient:
    def __init__(
        self,
        base_urls: list[str],
        client: httpx.AsyncClient,
        health: ProviderHealthRegistry,
        *,
        retry: RetryPolicy | None = None,
        weight_limit_1m: int = 6000,
        rate_per_second: float = 8.0,
        breaker_factory: Callable[[], CircuitBreaker] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        **http_kwargs: Any,
    ) -> None:
        if not base_urls:
            raise ValueError("at least one Binance base URL is required")
        self._health = health
        self._clock = clock
        self._wall_clock = wall_clock
        self._weight_limit = weight_limit_1m
        health.register(PROVIDER, "spot_market")
        self._bases = [
            _BaseEndpoint(
                url=url.rstrip("/"),
                http=ProviderHttpClient(
                    PROVIDER,
                    client,
                    health,
                    rate_per_second=rate_per_second,
                    retry=retry,
                    breaker=breaker_factory() if breaker_factory else None,
                    response_hook=self._weight_hook,
                    clock=clock,
                    **http_kwargs,
                ),
            )
            for url in base_urls
        ]
        self._active = 0
        health.set_detail(PROVIDER, "active_base_url", self._bases[0].url)

    @property
    def active_base_url(self) -> str:
        return self._bases[self._active].url

    def _weight_hook(self, response: httpx.Response, http: ProviderHttpClient) -> None:
        used = response.headers.get("x-mbx-used-weight-1m")
        if not used:
            return
        try:
            used_weight = int(used)
        except ValueError:
            return
        self._health.set_detail(PROVIDER, "used_weight_1m", used_weight)
        if used_weight >= self._weight_limit * _WEIGHT_PAUSE_FRACTION:
            seconds_to_reset = 60.0 - (self._wall_clock() % 60.0) + 1.0
            http.limiter.pause_until(self._clock() + seconds_to_reset)
            log.warning(
                "binance weight near limit; pausing",
                extra={"used_weight_1m": used_weight, "pause_seconds": round(seconds_to_reset, 1)},
            )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        errors: list[str] = []
        count = len(self._bases)
        for offset in range(count):
            index = (self._active + offset) % count
            base = self._bases[index]
            if base.restricted_until > self._clock():
                errors.append(f"{base.url}: parked after access restriction")
                continue
            try:
                data = await base.http.get_json(base.url + path, params=params)
            except ProviderRestricted as exc:
                base.restricted_until = self._clock() + _RESTRICTED_PARK_SECONDS
                errors.append(f"{base.url}: {exc.message}")
                self._health.set_detail(PROVIDER, "restricted_bases", self._restricted_urls())
                continue
            except (ProviderClientError, ProviderRateLimited):
                raise
            except ProviderError as exc:
                errors.append(f"{base.url}: {exc.message}")
                continue
            if index != self._active:
                log.warning("binance endpoint failover", extra={"from": self.active_base_url, "to": base.url})
                self._active = index
                self._health.set_detail(PROVIDER, "active_base_url", base.url)
            return data
        raise ProviderUnavailable(PROVIDER, "all Binance endpoints failed: " + "; ".join(errors))

    def _restricted_urls(self) -> list[str]:
        now = self._clock()
        return [b.url for b in self._bases if b.restricted_until > now]

    async def exchange_info(self) -> Any:
        return await self._get("/api/v3/exchangeInfo")

    async def klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": max(1, min(limit, 1000))}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        return await self._get("/api/v3/klines", params)

    async def tickers_24h(self, symbols: list[str]) -> Any:
        params = {"symbols": json.dumps(sorted(set(symbols)), separators=(",", ":"))}
        return await self._get("/api/v3/ticker/24hr", params)

    async def depth(self, symbol: str, limit: int = 100) -> Any:
        allowed = (5, 10, 20, 50, 100, 500, 1000, 5000)
        limit = min((value for value in allowed if value >= limit), default=5000)
        return await self._get("/api/v3/depth", {"symbol": symbol, "limit": limit})

    async def server_time(self) -> Any:
        return await self._get("/api/v3/time")
