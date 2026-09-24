"""Wires providers, adapters and services together from Settings.

Tests inject fake adapters and an SQLite session factory through the keyword overrides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.timeutil import utcnow
from app.data.adapters.base import (
    AltcoinSeasonAdapter,
    FearGreedAdapter,
    GlobalMetricsAdapter,
    ListingAdapter,
    ReferenceCandleAdapter,
    SpotMarketAdapter,
)
from app.data.adapters.binance import BinanceSpotAdapter
from app.data.adapters.kraken import KrakenSpotAdapter
from app.data.adapters.listings import (
    AlternativeMeAdapter,
    CoinGeckoAdapter,
    CoinMarketCapAdapter,
    CoinPaprikaAdapter,
)
from app.data.health import ProviderHealthRegistry
from app.data.http import CircuitBreaker, RetryPolicy
from app.data.providers.alternative_me import AlternativeMeClient
from app.data.providers.binance import BinanceRestClient
from app.data.providers.binance_ws import BinanceStreamManager
from app.data.providers.cmc_budget import CmcCreditBudget
from app.data.providers.coingecko import CoinGeckoClient
from app.data.providers.coinmarketcap import CoinMarketCapClient
from app.data.providers.coinpaprika import CoinPaprikaClient
from app.data.providers.kraken import KrakenRestClient
from app.database import create_engine_from_settings, create_session_factory
from app.services.analysis import AnalysisService
from app.services.assets import AssetService
from app.services.context import MarketContextService
from app.services.listing import ListingService
from app.services.market import MarketService
from app.services.regime import MarketRegimeService
from app.services.spot_router import SpotMarketRouter
from app.services.system_state import SystemStateStore
from app.services.universe import UniverseService

log = logging.getLogger(__name__)


class LivePriceBook:
    """Latest miniTicker values from the Binance stream (used when monitoring is active)."""

    def __init__(self) -> None:
        self.prices: dict[str, dict[str, Any]] = {}

    def on_message(self, stream: str, data: dict[str, Any]) -> None:
        if stream.endswith("@miniTicker") and "s" in data and "c" in data:
            self.prices[data["s"]] = {"close": data["c"], "event_time_ms": data.get("E"), "received_at": utcnow()}


@dataclass
class Container:
    settings: Settings
    http: httpx.AsyncClient
    health: ProviderHealthRegistry
    state: SystemStateStore
    engine: AsyncEngine | None
    session_factory: async_sessionmaker[AsyncSession] | None
    router: SpotMarketRouter
    listing: ListingService
    universe: UniverseService
    context: MarketContextService
    market: MarketService
    assets: AssetService
    regime: MarketRegimeService
    analysis: AnalysisService
    stream: BinanceStreamManager | None = None
    live_prices: LivePriceBook = field(default_factory=LivePriceBook)
    cmc: CoinMarketCapClient | None = None
    owns_http: bool = True

    async def warmup(self) -> None:
        """Startup checks that must never block or crash the app (e.g. CMC plan detection)."""
        if self.cmc is not None and self.cmc.has_key:
            try:
                await self.cmc.refresh_plan(force=True)
            except Exception:  # defensive: warmup is best effort
                log.exception("CoinMarketCap plan check failed during startup")
            else:
                log.info("CoinMarketCap access mode", extra={"mode": self.cmc.mode})

    async def aclose(self) -> None:
        if self.stream is not None and self.stream.running:
            await self.stream.stop()
        if self.owns_http:
            await self.http.aclose()
        if self.engine is not None:
            await self.engine.dispose()


def build_container(
    settings: Settings,
    *,
    http_client: httpx.AsyncClient | None = None,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    use_database: bool = True,
    spot_adapters: list[SpotMarketAdapter] | None = None,
    listing_adapters: list[ListingAdapter] | None = None,
    global_adapters: list[GlobalMetricsAdapter] | None = None,
    fear_greed_adapters: list[FearGreedAdapter] | None = None,
    altcoin_season_adapters: list[AltcoinSeasonAdapter] | None = None,
    reference_candle_adapter: ReferenceCandleAdapter | None = None,
    health: ProviderHealthRegistry | None = None,
) -> Container:
    health = health or ProviderHealthRegistry()
    owns_http = http_client is None
    http = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout_seconds),
        headers={"User-Agent": f"crypto-signal-dashboard/{settings.app_version}"},
        follow_redirects=False,
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
    )
    retry = RetryPolicy(
        max_retries=settings.http_max_retries,
        base_delay=settings.http_backoff_base_seconds,
        max_delay=settings.http_backoff_max_seconds,
    )

    def breaker() -> CircuitBreaker:
        return CircuitBreaker(settings.circuit_failure_threshold, settings.circuit_recovery_seconds)

    if spot_adapters is None:
        binance = BinanceRestClient(
            settings.binance_rest_base_urls,
            http,
            health,
            retry=retry,
            weight_limit_1m=settings.binance_weight_limit_1m,
            breaker_factory=breaker,
        )
        kraken = KrakenRestClient(settings.kraken_base_url, http, health, retry=retry, breaker=breaker())
        spot_adapters = [
            BinanceSpotAdapter(
                binance, quote_asset=settings.binance_quote_asset, pairs_ttl_seconds=settings.exchange_pairs_ttl_seconds
            ),
            KrakenSpotAdapter(
                kraken, quote_asset=settings.kraken_quote_asset, pairs_ttl_seconds=settings.exchange_pairs_ttl_seconds
            ),
        ]

    cmc_client: CoinMarketCapClient | None = None
    needs_defaults = (
        listing_adapters is None
        or global_adapters is None
        or fear_greed_adapters is None
        or altcoin_season_adapters is None
    )
    if needs_defaults:
        cmc_client = CoinMarketCapClient(
            settings.cmc_public_base_url,
            settings.cmc_pro_base_url,
            http,
            health,
            api_key=settings.cmc_key,
            budget=CmcCreditBudget(
                safety_factor=settings.cmc_credit_safety_factor,
                reserve_pct=settings.cmc_credit_reserve_pct,
                refresh_seconds=settings.cmc_plan_refresh_seconds,
            ),
            keyless_fallback=settings.cmc_keyless_fallback,
            retry=retry,
            breaker_factory=breaker,
        )
        cmc = CoinMarketCapAdapter(cmc_client)
        gecko = CoinGeckoAdapter(CoinGeckoClient(settings.coingecko_base_url, http, health, retry=retry, breaker=breaker()))
        paprika = CoinPaprikaAdapter(
            CoinPaprikaClient(settings.coinpaprika_base_url, http, health, retry=retry, breaker=breaker())
        )
        alternative = AlternativeMeAdapter(
            AlternativeMeClient(settings.alternative_me_base_url, http, health, retry=retry, breaker=breaker())
        )
        listing_adapters = listing_adapters if listing_adapters is not None else [cmc, gecko, paprika]
        global_adapters = global_adapters if global_adapters is not None else [cmc, gecko]
        fear_greed_adapters = fear_greed_adapters if fear_greed_adapters is not None else [cmc, alternative]
        altcoin_season_adapters = altcoin_season_adapters if altcoin_season_adapters is not None else [cmc]
        if reference_candle_adapter is None:
            reference_candle_adapter = cmc

    if use_database and session_factory is None:
        engine = engine or create_engine_from_settings(settings)
        session_factory = create_session_factory(engine)

    state = SystemStateStore()
    router = SpotMarketRouter(spot_adapters, health)
    listing = ListingService(
        listing_adapters,
        fetch_limit=settings.listing_fetch_limit,
        min_entries=settings.universe_size,
        max_age_seconds=settings.listing_max_age_seconds,
        refresh_seconds=settings.listing_refresh_seconds,
    )
    universe = UniverseService(
        listing,
        router,
        size=settings.universe_size,
        refresh_seconds=settings.universe_refresh_seconds,
        exclude_wrapped=settings.exclude_wrapped_assets,
        symbol_overrides=settings.symbol_overrides,
    )
    context = MarketContextService(
        global_adapters,
        fear_greed_adapters,
        cache_seconds=settings.context_cache_seconds,
        altcoin_season_adapters=altcoin_season_adapters,
    )
    market = MarketService(settings, universe, listing, router, context, health, state, session_factory)
    assets = AssetService(
        settings, universe, listing, router, health, state, session_factory, reference=reference_candle_adapter
    )
    regime = MarketRegimeService(settings, universe, router, context, assets.validate, session_factory)
    analysis = AnalysisService(settings, universe, assets, regime, session_factory)
    live_prices = LivePriceBook()
    stream = BinanceStreamManager(settings.binance_ws_base_urls, live_prices.on_message)
    return Container(
        settings=settings,
        http=http,
        health=health,
        state=state,
        engine=engine,
        session_factory=session_factory,
        router=router,
        listing=listing,
        universe=universe,
        context=context,
        market=market,
        assets=assets,
        regime=regime,
        analysis=analysis,
        stream=stream,
        live_prices=live_prices,
        cmc=cmc_client,
        owns_http=owns_http,
    )
