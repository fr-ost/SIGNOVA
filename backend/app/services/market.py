"""Market overview snapshot for the Top-20 universe.

Live prices come from the spot exchange; the listing aggregator is only a reference
for cross-validation and market cap. A missing live price is shown as missing (with
the reason), never replaced by the reference price.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.enums import CrossCheckStatus, DataState, ProviderStatus, worst_state
from app.core.timeutil import utcnow
from app.data.health import ProviderHealthRegistry
from app.data.validation.health_score import consistency_component, freshness_component, provider_component
from app.data.validation.prices import USD_STABLE_QUOTES, cross_validate_price, quote_to_usd_rate
from app.schemas.api import (
    AltcoinSeasonOut,
    AssetMarketOut,
    CrossCheckOut,
    ExcludedAssetOut,
    FearGreedOut,
    GlobalMetricsOut,
    MarketContextOut,
    MarketSnapshotOut,
    UniverseMetaOut,
)
from app.services import persistence
from app.services.cache import AsyncTTLCache
from app.services.context import MarketContext, MarketContextService
from app.services.listing import ListingService, ListingUnavailable
from app.services.spot_router import SpotMarketRouter
from app.services.system_state import SystemStateStore
from app.services.universe import Universe, UniverseService

log = logging.getLogger(__name__)

_QUICK_WEIGHTS = {"freshness": 0.30, "consistency": 0.25, "provider": 0.15}


def quick_health_score(freshness: float, consistency: float, provider: float) -> int:
    """Ticker-level score (no candle completeness); re-normalised to 0-100."""
    total = (
        _QUICK_WEIGHTS["freshness"] * freshness
        + _QUICK_WEIGHTS["consistency"] * consistency
        + _QUICK_WEIGHTS["provider"] * provider
    )
    return int(round(100 * total / sum(_QUICK_WEIGHTS.values())))


def context_out(ctx: MarketContext) -> MarketContextOut:
    return MarketContextOut(
        fetched_at=ctx.fetched_at,
        global_status=ctx.global_status,
        global_metrics=GlobalMetricsOut.model_validate(ctx.global_metrics) if ctx.global_metrics else None,
        fear_greed_status=ctx.fear_greed_status,
        fear_greed=FearGreedOut.model_validate(ctx.fear_greed) if ctx.fear_greed else None,
        altcoin_season_status=ctx.altcoin_season_status,
        altcoin_season=AltcoinSeasonOut.model_validate(ctx.altcoin_season) if ctx.altcoin_season else None,
        errors=ctx.errors,
    )


def universe_meta(universe: Universe) -> UniverseMetaOut:
    return UniverseMetaOut(
        size=len(universe.assets),
        listing_source=universe.listing_source,
        listing_access=universe.listing_access,
        fallback_used=universe.fallback_used,
        built_at=universe.built_at,
        listing_newest_update_age_seconds=universe.listing_newest_update_age_seconds,
        stale=universe.stale,
        excluded=[ExcludedAssetOut.model_validate(e) for e in universe.excluded],
        errors=universe.errors,
    )


class MarketService:
    def __init__(
        self,
        settings: Settings,
        universe: UniverseService,
        listing: ListingService,
        router: SpotMarketRouter,
        context: MarketContextService,
        health: ProviderHealthRegistry,
        state: SystemStateStore,
        session_factory: async_sessionmaker[AsyncSession] | None,
    ) -> None:
        self._s = settings
        self._universe = universe
        self._listing = listing
        self._router = router
        self._context = context
        self._health = health
        self._state = state
        self._sessions = session_factory
        self._cache = AsyncTTLCache()

    async def snapshot(self, *, force: bool = False) -> MarketSnapshotOut:
        return await self._cache.get_or_load("market", self._build, self._s.market_cache_seconds, force=force)

    async def _build(self) -> MarketSnapshotOut:
        universe = await self._universe.get()
        try:
            listing = await self._listing.latest()
            reference = {e.symbol: e for e in listing.entries}
            reference_source = listing.source
        except ListingUnavailable:
            reference, reference_source = {}, universe.listing_source
        quote_prices = {s: e.price_usd for s, e in reference.items() if s in USD_STABLE_QUOTES}
        quote_prices = quote_prices or dict(universe.quote_usd_prices)

        found, errors = await self._router.tickers({a.symbol: a.markets for a in universe.assets})
        context = await self._context.get()
        now = utcnow()
        primary_adapter = self._router.adapter_names[0]

        rows: list[AssetMarketOut] = []
        for asset in universe.assets:
            ref_entry = reference.get(asset.symbol, asset.listing)
            reasons: list[str] = []
            if asset.symbol in found:
                ticker, market = found[asset.symbol]
                rate, rate_note = quote_to_usd_rate(market.quote_asset, quote_prices)
                price_usd = ticker.last_price * rate if rate else None
                if rate is None:
                    reasons.append(rate_note)
                cross = cross_validate_price(
                    primary_price_usd=price_usd,
                    primary_source=market.adapter,
                    reference_price_usd=ref_entry.price_usd,
                    reference_source=ref_entry.source,
                    reference_updated_at=ref_entry.last_updated,
                    now=now,
                    warn_pct=self._s.cross_source_warn_deviation_pct,
                    max_pct=self._s.cross_source_max_deviation_pct,
                    max_reference_age_seconds=self._s.reference_max_age_seconds,
                )
                age = (now - ticker.observed_at).total_seconds()
                provider_status = self._health.status_of(market.adapter)
                states = [DataState.HEALTHY]
                if age > self._s.ticker_max_age_seconds:
                    states.append(DataState.STALE_DATA)
                    reasons.append(f"ticker is {age:.0f}s old")
                if cross.status == CrossCheckStatus.CONFLICT:
                    states.append(DataState.DATA_CONFLICT)
                    reasons.append(cross.reason)
                elif cross.status == CrossCheckStatus.UNVERIFIED:
                    states.append(DataState.DEGRADED)
                    reasons.append(f"price not cross-verified: {cross.reason}")
                elif cross.status == CrossCheckStatus.WARNING:
                    reasons.append(cross.reason)
                if market.adapter != primary_adapter:
                    states.append(DataState.DEGRADED)
                    reasons.append(f"served by fallback source {market.adapter}")
                if provider_status in (ProviderStatus.DEGRADED, ProviderStatus.RATE_LIMITED):
                    states.append(DataState.DEGRADED)
                    reasons.append(f"{market.adapter} is {provider_status}")
                exchange_change = ticker.pct_change_24h
                exchange_volume = (
                    ticker.volume_quote_24h * rate if ticker.volume_quote_24h is not None and rate else None
                )
                rows.append(
                    AssetMarketOut(
                        universe_rank=asset.universe_rank,
                        symbol=asset.symbol,
                        name=asset.name,
                        supported=True,
                        unsupported_reason=None,
                        market_source=market.adapter,
                        market_symbol=market.symbol,
                        quote_asset=market.quote_asset,
                        price=ticker.last_price,
                        price_usd=price_usd,
                        reference_price_usd=ref_entry.price_usd,
                        reference_source=ref_entry.source,
                        pct_change_24h=exchange_change if exchange_change is not None else ref_entry.pct_change_24h,
                        pct_change_24h_source=market.adapter if exchange_change is not None else ref_entry.source,
                        volume_24h_usd=exchange_volume if exchange_volume is not None else ref_entry.volume_24h_usd,
                        volume_24h_source=market.adapter if exchange_volume is not None else ref_entry.source,
                        market_cap_usd=ref_entry.market_cap_usd,
                        ticker_age_seconds=round(age, 1),
                        cross_check=CrossCheckOut.model_validate(cross),
                        data_state=worst_state(states),
                        data_health_score=quick_health_score(
                            freshness_component(age, self._s.ticker_max_age_seconds),
                            consistency_component(cross.status),
                            provider_component(provider_status),
                        ),
                        health_scope="ticker",
                        reasons=reasons,
                    )
                )
            else:
                reasons.extend(errors.get(asset.symbol, []) or [asset.unsupported_reason or "no live price"])
                primary = asset.primary_market
                rows.append(
                    AssetMarketOut(
                        universe_rank=asset.universe_rank,
                        symbol=asset.symbol,
                        name=asset.name,
                        supported=asset.supported,
                        unsupported_reason=asset.unsupported_reason,
                        market_source=primary.adapter if primary else None,
                        market_symbol=primary.symbol if primary else None,
                        quote_asset=primary.quote_asset if primary else None,
                        price=None,
                        price_usd=None,
                        reference_price_usd=ref_entry.price_usd,
                        reference_source=ref_entry.source,
                        pct_change_24h=ref_entry.pct_change_24h,
                        pct_change_24h_source=ref_entry.source,
                        volume_24h_usd=ref_entry.volume_24h_usd,
                        volume_24h_source=ref_entry.source,
                        market_cap_usd=ref_entry.market_cap_usd,
                        ticker_age_seconds=None,
                        cross_check=None,
                        data_state=DataState.API_FAILURE,
                        data_health_score=0,
                        health_scope="ticker",
                        reasons=reasons,
                    )
                )

        overall, overall_reasons = self._aggregate(rows, universe, reference_source)
        counts = Counter(row.data_state.value for row in rows)
        result = MarketSnapshotOut(
            generated_at=now,
            data_state=overall,
            data_state_reasons=overall_reasons,
            state_counts=dict(counts),
            universe=universe_meta(universe),
            context=context_out(context),
            assets=rows,
            persistence="skipped",
        )
        result.persistence = await self._persist(result, universe, overall)
        return result

    def _aggregate(
        self, rows: list[AssetMarketOut], universe: Universe, reference_source: str
    ) -> tuple[DataState, list[str]]:
        reasons: list[str] = []
        supported = [r for r in rows if r.supported]
        live = [r for r in supported if r.price is not None]
        if not live:
            return DataState.API_FAILURE, ["no live market data for any universe asset"]
        unhealthy = [r for r in supported if r.data_state not in (DataState.HEALTHY,)]
        if universe.stale:
            reasons.append("universe ranking could not be refreshed; showing last known ranking")
        if universe.fallback_used:
            reasons.append(f"ranking served by fallback listing source {universe.listing_source}")
        if reference_source != universe.listing_source:
            reasons.append(f"reference prices from {reference_source}")
        worst = worst_state(r.data_state for r in supported)
        share_worst = sum(1 for r in supported if r.data_state == worst) / len(supported)
        if unhealthy:
            reasons.append(f"{len(unhealthy)} of {len(supported)} supported assets are not HEALTHY")
        if worst in (DataState.STALE_DATA, DataState.API_FAILURE, DataState.DATA_CONFLICT) and share_worst >= 0.5:
            return worst, reasons
        if unhealthy or universe.stale or universe.fallback_used:
            return DataState.DEGRADED, reasons
        return DataState.HEALTHY, reasons

    async def _persist(self, snap: MarketSnapshotOut, universe: Universe, overall: DataState) -> str:
        changed = self._state.update_data_state(overall, snap.data_state_reasons)
        if self._sessions is None:
            return "disabled"
        batch_id = str(uuid.uuid4())
        rows: list[dict[str, Any]] = [
            {
                "batch_id": batch_id,
                "captured_at": snap.generated_at,
                "symbol": a.symbol,
                "universe_rank": a.universe_rank,
                "listing_source": universe.listing_source,
                "market_source": a.market_source,
                "price_usd": a.price_usd,
                "reference_price_usd": a.reference_price_usd,
                "deviation_pct": a.cross_check.deviation_pct if a.cross_check else None,
                "pct_change_24h": a.pct_change_24h,
                "volume_24h_usd": a.volume_24h_usd,
                "market_cap_usd": a.market_cap_usd,
                "data_state": a.data_state.value,
                "data_health_score": a.data_health_score,
                "details": {"reasons": a.reasons, "health_scope": a.health_scope},
            }
            for a in snap.assets
        ]
        try:
            async with self._sessions() as session:
                await persistence.upsert_universe_assets(session, universe)
                persistence.add_market_snapshot_rows(session, rows)
                dirty = self._health.pop_dirty()
                if dirty:
                    persistence.add_provider_health(session, dirty)
                if changed:
                    persistence.add_system_event(
                        session,
                        "WARNING" if overall != DataState.HEALTHY else "INFO",
                        "data_state_changed",
                        f"aggregate data state is now {overall.value}",
                        {"reasons": snap.data_state_reasons},
                    )
                await session.commit()
        except Exception:
            log.exception("market snapshot persistence failed")
            return "failed"
        return "ok"
