"""Wires providers, adapters and services together from Settings.

Tests inject fake adapters and an SQLite session factory through the keyword overrides.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.enums import ProcessingState, SignalLabel
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
from app.services.assistant import chat_context, universe_names
from app.services.chat import ChatService
from app.services.control import AnalysisController
from app.services.news import NewsService
from app.services.onchain import OnChainService
from app.services.events import EventsService
from app.services.killswitch import KillSwitch
from app.services.ai_review import AIReviewService
from app.services.ai_news import AINewsReader
from app.services.derivatives import DerivativesService
from app.services.evidence import EvidenceService
from app.services.futures import FuturesService
from app.services.learning import LearningService
from app.services.lab import LabService
from app.services.settings_store import SettingsStore
from app.analysis.engine import STRATEGY
from app.services.outcomes import OutcomeTracker
from app.services.scalp import ScalpService
from app.services.selection import ScanSelectionService
from app.services.sentiment import SentimentService
from app.services.portfolio import PortfolioService
from app.services.watchlist import WatchlistService
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
    watchlist: WatchlistService
    controller: AnalysisController
    news: NewsService
    chat: ChatService
    portfolio: PortfolioService
    onchain: OnChainService
    sentiment: SentimentService
    selection: ScanSelectionService
    events: EventsService
    scalp: ScalpService
    tracker: OutcomeTracker
    kill: KillSwitch
    lab: LabService
    store: SettingsStore
    ai_review: AIReviewService
    derivatives: DerivativesService
    evidence: EvidenceService
    learning: LearningService
    futures: FuturesService
    stream: BinanceStreamManager | None = None
    live_prices: LivePriceBook = field(default_factory=LivePriceBook)
    cmc: CoinMarketCapClient | None = None
    owns_http: bool = True

    async def warmup(self) -> None:
        """Startup checks that must never block or crash the app (e.g. CMC plan detection)."""
        await self.kill.load()  # first: an engaged emergency stop keeps the schedule off
        await self.watchlist.load()
        await self.selection.load()
        await self.scalp.load()
        await self.lab.load()
        await self.ai_review.load()
        await self.evidence.load()
        await self.learning.load()
        await self.futures.load()
        await self.portfolio.load()
        self.controller.start_background()
        if self.cmc is not None and self.cmc.has_key and not self.kill.active:
            try:
                await self.cmc.refresh_plan(force=True)
            except Exception:  # defensive: warmup is best effort
                log.exception("CoinMarketCap plan check failed during startup")
            else:
                log.info("CoinMarketCap access mode", extra={"mode": self.cmc.mode})

    async def emergency_stop(self, reason: str | None = None) -> None:
        """Halt everything: scans, schedule, lab, live prices; refuse outside calls until resumed."""
        await self.kill.engage(reason)  # first, so nothing new can start while we stop the rest
        await self.ai_review.stop()
        await self.lab.stop()
        await self.scalp.stop()
        await self.futures.stop()
        await self.controller.stop()
        self.state.processing_state = ProcessingState.EMERGENCY_STOP

    async def resume(self) -> None:
        await self.kill.release()

    async def aclose(self) -> None:
        await self.ai_review.stop()
        await self.lab.stop()
        await self.scalp.stop()
        await self.futures.stop()
        await self.controller.aclose()
        if self.stream is not None and self.stream.running:
            await self.stream.stop()
        if self.owns_http:
            await self.http.aclose()
        if self.engine is not None:
            await self.engine.dispose()


def wire_ai_review(review: AIReviewService, controller: AnalysisController, analysis: AnalysisService,
                   scalp: ScalpService) -> None:
    """Show verdicts on the signals they reviewed, and review new buys automatically when enabled."""
    from app.services.ai_review import capped_label

    def apply(result: dict[str, Any]) -> None:
        symbol, horizon, capped = result["symbol"], result.get("horizon") or "", review.caps(result)
        if result["kind"] == "swing":
            cached = analysis.cached(symbol)
            if cached is not None:
                cached.ai_review = result
                if capped and cached.signal.value in ("BUY", "STRONG BUY"):
                    cached.signal = SignalLabel.WATCH
                    cached.reasons = [f"AI reviewer rejected it: {result['summary']}", *cached.reasons]
            scan = controller.last_scan
            for row in scan.signals if scan else []:
                if row.symbol == symbol:
                    row.ai_review = result
                    if capped and row.signal.value in ("BUY", "STRONG BUY"):
                        row.signal = SignalLabel.WATCH
                        row.reasons = [f"AI reviewer rejected it: {result['summary']}", *row.reasons]
        else:
            for sig in (scalp.results.get(horizon) or {}).get("signals", []):
                if sig["symbol"] == symbol:
                    sig["ai_review"] = result
                    shown = capped_label(sig["signal"], result, review)
                    if shown != sig["signal"]:
                        sig["signal"] = shown
                        sig["reasons"] = [f"AI reviewer rejected it: {result['summary']}", *sig["reasons"]]

    review.on_review = apply

    def after_swing(scan: Any) -> None:
        buys = [row for row in scan.signals if row.signal.value in ("BUY", "STRONG BUY")]
        items = []
        for row in buys:
            cached = analysis.cached(row.symbol)
            items.append(("swing", row.symbol, "", cached.model_dump(mode="json") if cached else row.model_dump(mode="json")))
        review.schedule_auto(items)

    def after_scalp(horizon: str, result: dict[str, Any]) -> None:
        review.schedule_auto([("scalp", s["symbol"], horizon, s) for s in result["signals"]
                              if s["signal"] in ("BUY", "STRONG BUY")])

    controller.after_scan = after_swing
    scalp.after_scan = after_scalp


def wire_futures_review(review: AIReviewService, futures: FuturesService) -> None:
    """Show AI verdicts on futures signals too (filter mode: a reject turns the trade into WATCH)."""
    previous = review.on_review

    def apply(result: dict[str, Any]) -> None:
        if result.get("kind") != "futures":
            if previous is not None:
                previous(result)
            return
        for sig in (futures.results.get(result.get("horizon") or "") or {}).get("signals", []):
            if sig["symbol"] == result["symbol"]:
                sig["ai_review"] = result
                if review.caps(result) and sig["signal"] in ("BUY", "STRONG BUY"):
                    sig["signal"], sig["label"] = "WATCH", "WATCH"
                    sig["reasons"] = [f"AI reviewer rejected it: {result['summary']}", *sig["reasons"]]

    review.on_review = apply


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
        gecko = CoinGeckoAdapter(
            CoinGeckoClient(settings.coingecko_url, http, health, retry=retry, breaker=breaker(), headers=settings.coingecko_headers)
        )
        paprika = CoinPaprikaAdapter(
            CoinPaprikaClient(
                settings.coinpaprika_base_url, http, health, retry=retry, breaker=breaker(),
                timeout_seconds=settings.coinpaprika_timeout_seconds,
            )
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
    watchlist = WatchlistService(session_factory)
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
        extra_symbols=watchlist.symbols,
        quotes=CoinMarketCapAdapter(cmc_client).quotes if cmc_client is not None else None,
    )
    watchlist._on_change = universe.invalidate
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
    selection = ScanSelectionService(session_factory)
    analysis.scan_filter = selection.filter
    controller = AnalysisController(
        analysis, universe, state, stream, live_prices, auto_minutes=settings.auto_analyze_minutes, selection=selection
    )

    news = NewsService(
        http,
        feeds=settings.news_feeds,
        cryptocompare_url=settings.cryptocompare_news_url or None,
        cache_seconds=settings.news_cache_seconds,
        asset_names=lambda: universe_names(universe),
        session_factory=session_factory,
        trending_url=f"{settings.coingecko_url.rstrip('/')}/search/trending",
        trending_headers=settings.coingecko_headers,
    )
    portfolio = PortfolioService(session_factory, router, universe, analysis, watchlist, live_prices=controller)

    def listing_prices() -> dict[str, float]:
        last = universe.last
        return {a.symbol: a.listing.price_usd for a in last.assets} if last else {}

    def sentiment_symbols() -> list[str]:
        last = universe.last
        return [a.symbol for a in last.assets if a.supported] if last else ["BTC", "ETH"]

    onchain = OnChainService(settings, http, health, listing_prices, session_factory)
    sentiment = SentimentService(settings, http, health, news, onchain, sentiment_symbols, session_factory)
    def event_coins() -> list[tuple[str, str, float | None]]:
        last = universe.last
        if last is None:
            return []
        return [(a.symbol, a.name, a.listing.price_usd) for a in selection.filter(last.assets)]

    events = EventsService(settings, http, health, event_coins)

    async def before_scan() -> None:
        last = universe.last
        chosen = [a.symbol for a in selection.filter(last.assets)] if last is not None else []
        jobs = [sentiment.digest(force=True), evidence.prepare(chosen)]
        if settings.mobula_key:
            jobs.append(events.unlocks())  # cached for EVENTS_CACHE_SECONDS; no refetch per scan
        for result in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(result, Exception):
                log.warning("pre-scan context refresh failed", extra={"error": str(result)})

    store = SettingsStore(session_factory)
    tracker = OutcomeTracker(
        session_factory,
        cost_pct=lambda strategy: analysis.risk_params.round_trip_cost_pct if strategy == STRATEGY
        else 2.0 * (futures.fee_pct + futures.slippage_pct) if strategy.startswith("fut_")
        else 2.0 * (analysis.risk_params.fee_pct + settings.scalp_slippage_pct),
    )
    analysis.on_candles = tracker.update
    scalp = ScalpService(
        settings, universe, assets, router,
        risk_params=lambda: analysis.risk_params,
        selection_filter=selection.filter,
        notes_for=lambda symbol: [f"sentiment: {n}" for n in (getattr(sentiment.for_asset(symbol), "notes", None) or [])]
        + events.notes_for(symbol),
        equity=portfolio.equity_at_cost,
        session_factory=session_factory,
        on_candles=tracker.update,
        blocked=lambda: state.emergency_stop,
        store=store,
    )
    kill = KillSwitch(state, session_factory)
    lab = LabService(universe, scalp, store, selection_filter=selection.filter, blocked=lambda: state.emergency_stop,
                     concurrency=settings.signal_scan_concurrency)
    analysis.sentiment_for = sentiment.for_asset
    analysis.event_notes = events.notes_for
    analysis.before_scan = before_scan
    chat = ChatService(
        settings,
        http,
        lambda symbol: chat_context(analysis, controller, news, portfolio, watchlist, symbol, sentiment, onchain, events),
    )
    ai_review = AIReviewService(chat, store, session_factory, blocked=lambda: state.emergency_stop)
    wire_ai_review(ai_review, controller, analysis, scalp)
    derivatives = DerivativesService(settings, http, health)
    learning = LearningService(session_factory, store)
    evidence = EvidenceService(
        settings, derivatives, store, news=news,
        ai_news=AINewsReader(settings, chat, store, blocked=lambda: state.emergency_stop),
        onchain=onchain, events=events, context=context, assets=assets, session_factory=session_factory,
        blocked=lambda: state.emergency_stop, universe_symbols=sentiment_symbols,
    )
    evidence.learner = learning
    analysis.evidence = evidence
    scalp.evidence = evidence
    scalp.market_regime = regime.current
    futures = FuturesService(
        settings, universe, assets, scalp, store, risk_params=lambda: analysis.risk_params,
        equity=portfolio.equity_at_cost, derivatives=derivatives, evidence=evidence, selection_filter=selection.filter,
        session_factory=session_factory, on_candles=tracker.update, blocked=lambda: state.emergency_stop,
    )
    wire_futures_review(ai_review, futures)
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
        watchlist=watchlist,
        controller=controller,
        news=news,
        chat=chat,
        portfolio=portfolio,
        onchain=onchain,
        sentiment=sentiment,
        selection=selection,
        events=events,
        scalp=scalp,
        tracker=tracker,
        kill=kill,
        lab=lab,
        store=store,
        ai_review=ai_review,
        derivatives=derivatives,
        evidence=evidence,
        learning=learning,
        futures=futures,
        stream=stream,
        live_prices=live_prices,
        cmc=cmc_client,
        owns_http=owns_http,
    )
