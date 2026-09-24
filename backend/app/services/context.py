"""Market-wide context: total market cap, BTC dominance, Fear & Greed, Altcoin Season.

Unavailable data is reported as UNAVAILABLE with the reasons, never as a neutral value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import Availability
from app.core.timeutil import utcnow
from app.data.adapters.base import AltcoinSeasonAdapter, FearGreedAdapter, GlobalMetricsAdapter
from app.data.http import ProviderError
from app.data.normalization.schemas import AltcoinSeason, FearGreed, GlobalMetrics
from app.services.cache import AsyncTTLCache


@dataclass
class MarketContext:
    fetched_at: datetime
    global_metrics: GlobalMetrics | None
    global_status: Availability
    fear_greed: FearGreed | None
    fear_greed_status: Availability
    altcoin_season: AltcoinSeason | None = None
    altcoin_season_status: Availability = Availability.UNAVAILABLE
    errors: list[str] = field(default_factory=list)


class MarketContextService:
    def __init__(
        self,
        global_adapters: list[GlobalMetricsAdapter],
        fear_greed_adapters: list[FearGreedAdapter],
        *,
        cache_seconds: float,
        altcoin_season_adapters: list[AltcoinSeasonAdapter] | None = None,
    ) -> None:
        self._global = global_adapters
        self._fng = fear_greed_adapters
        self._altseason = altcoin_season_adapters or []
        self._ttl = cache_seconds
        self._cache = AsyncTTLCache()

    async def get(self, *, force: bool = False) -> MarketContext:
        return await self._cache.get_or_load("context", self._load, self._ttl, force=force)

    async def _load(self) -> MarketContext:
        errors: list[str] = []
        metrics: GlobalMetrics | None = None
        for adapter in self._global:
            try:
                metrics = await adapter.global_metrics()
                break
            except ProviderError as exc:
                errors.append(f"global metrics via {adapter.name}: {exc.message}")
        fear_greed: FearGreed | None = None
        for adapter in self._fng:
            try:
                fear_greed = await adapter.fear_greed()
                break
            except ProviderError as exc:
                errors.append(f"fear & greed via {adapter.name}: {exc.message}")
        altcoin_season: AltcoinSeason | None = None
        for adapter in self._altseason:
            try:
                altcoin_season = await adapter.altcoin_season()
                break
            except ProviderError as exc:
                errors.append(f"altcoin season via {adapter.name}: {exc.message}")
        return MarketContext(
            fetched_at=utcnow(),
            global_metrics=metrics,
            global_status=Availability.AVAILABLE if metrics else Availability.UNAVAILABLE,
            fear_greed=fear_greed,
            fear_greed_status=Availability.AVAILABLE if fear_greed else Availability.UNAVAILABLE,
            altcoin_season=altcoin_season,
            altcoin_season_status=Availability.AVAILABLE if altcoin_season else Availability.UNAVAILABLE,
            errors=errors,
        )
