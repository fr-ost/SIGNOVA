"""Per-asset data collection and the full integrity gate.

For one universe asset: candles on every timeframe, live ticker, order book, validation,
cross-source price check, volatility check, then the fail-closed integrity gate.
Closed candles are stored in the candles table (upsert) for historical use.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.enums import ProviderStatus, Timeframe
from app.data.adapters.base import ReferenceCandleAdapter
from app.data.http import ProviderError, ProviderPlanLimited
from app.core.timeutil import utcnow
from app.data.health import ProviderHealthRegistry
from app.data.normalization.schemas import Candle, Ticker
from app.data.validation.candle_reference import CandleCrossCheck, cross_validate_candles, unverified
from app.data.validation.candles import CandleValidationReport, validate_candles
from app.data.validation.gate import IntegrityGate, IntegrityInputs, IntegrityResult
from app.data.validation.orderbook import OrderBookSummary, summarize_order_book
from app.data.validation.prices import (
    USD_STABLE_QUOTES,
    CrossCheck,
    cross_validate_price,
    quote_to_usd_rate,
)
from app.data.validation.volatility import VolatilityCheck, check_volatility
from app.schemas.api import (
    AssetDetailOut,
    CandleAnomalyOut,
    CandleCrossCheckOut,
    CandleOut,
    CandlesOut,
    CrossCheckOut,
    IntegrityOut,
    ListingInfoOut,
    OrderBookSummaryOut,
    StageOut,
    TickerOut,
    TimeframeValidationOut,
    VolatilityOut,
)
from app.services import persistence
from app.services.cache import AsyncTTLCache
from app.services.listing import ListingService, ListingUnavailable
from app.services.spot_router import MarketRef, NoMarketData, SpotMarketRouter
from app.services.system_state import SystemStateStore
from app.services.universe import UniverseAsset, UniverseService

log = logging.getLogger(__name__)


class AssetNotInUniverse(Exception):
    def __init__(self, symbol: str) -> None:
        super().__init__(f"{symbol} is not in the current Top universe")
        self.symbol = symbol


class AssetUnsupported(Exception):
    def __init__(self, symbol: str, reason: str) -> None:
        super().__init__(f"{symbol}: {reason}")
        self.symbol = symbol
        self.reason = reason


def _markets(asset: UniverseAsset) -> list[dict[str, str]]:
    return [
        {"source": m.adapter, "symbol": m.symbol, "quote_asset": m.quote_asset, "priority": str(i + 1)}
        for i, m in enumerate(asset.markets)
    ]


def _candle_out(c: Candle) -> CandleOut:
    return CandleOut(
        open_time=c.open_time,
        open=c.open,
        high=c.high,
        low=c.low,
        close=c.close,
        volume=c.volume,
        quote_volume=c.quote_volume,
    )


def _timeframe_out(
    tf: Timeframe,
    report: CandleValidationReport | None,
    closed: Sequence[Candle],
    market: MarketRef | None,
    error: str | None = None,
) -> TimeframeValidationOut:
    if report is None:
        return TimeframeValidationOut(
            timeframe=tf.value,
            label=tf.label,
            source=None,
            market_symbol=None,
            ok=False,
            received=0,
            closed=0,
            duplicates=0,
            conflicting_duplicates=0,
            invalid_ohlc=0,
            misaligned=0,
            missing_candles=0,
            missing_in_recent_window=0,
            completeness_pct=0.0,
            last_closed_open_time=None,
            last_closed_age_seconds=None,
            last_close=None,
            is_stale=False,
            insufficient_history=True,
            anomalies=[],
            issues=[],
            critical_issues=[error or "candles unavailable"],
            error=error,
        )
    return TimeframeValidationOut(
        timeframe=tf.value,
        label=tf.label,
        source=report.source,
        market_symbol=market.symbol if market else None,
        ok=report.ok,
        received=report.received,
        closed=report.closed,
        duplicates=report.duplicates,
        conflicting_duplicates=report.conflicting_duplicates,
        invalid_ohlc=report.invalid_ohlc,
        misaligned=report.misaligned,
        missing_candles=report.missing_candles,
        missing_in_recent_window=report.missing_in_recent_window,
        completeness_pct=report.completeness_pct,
        last_closed_open_time=report.last_closed_open_time,
        last_closed_age_seconds=report.last_closed_age_seconds,
        last_close=closed[-1].close if closed else None,
        is_stale=report.is_stale,
        insufficient_history=report.insufficient_history,
        anomalies=[CandleAnomalyOut.model_validate(a) for a in report.anomalies[-20:]],
        issues=report.issues,
        critical_issues=report.critical_issues,
        error=error,
    )


def integrity_out(result: IntegrityResult) -> IntegrityOut:
    return IntegrityOut(
        decision=result.decision,
        passed=result.passed,
        state=result.state,
        data_health_score=result.data_health_score,
        components=result.components,
        stages=[StageOut.model_validate(s) for s in result.stages],
        reasons=result.reasons,
        checked_at=result.checked_at,
    )


class AssetService:
    def __init__(
        self,
        settings: Settings,
        universe: UniverseService,
        listing: ListingService,
        router: SpotMarketRouter,
        health: ProviderHealthRegistry,
        state: SystemStateStore,
        session_factory: async_sessionmaker[AsyncSession] | None,
        reference: ReferenceCandleAdapter | None = None,
    ) -> None:
        self._s = settings
        self._universe = universe
        self._listing = listing
        self._router = router
        self._health = health
        self._state = state
        self._sessions = session_factory
        self._reference = reference
        self._gate = IntegrityGate()
        self._cache = AsyncTTLCache()

    async def _asset(self, symbol: str) -> UniverseAsset:
        universe = await self._universe.get()
        asset = universe.find(symbol)
        if asset is None:
            raise AssetNotInUniverse(symbol.upper())
        return asset

    def _validate(self, tf: Timeframe, candles: list[Candle]) -> tuple[list[Candle], CandleValidationReport]:
        return validate_candles(
            candles,
            tf,
            utcnow(),
            min_required=self._s.candle_min_history,
            stale_tolerance_intervals=self._s.candle_stale_tolerance_intervals,
            stale_grace_seconds=self._s.candle_stale_grace_seconds,
            recent_window=self._s.candle_recent_window,
            max_recent_missing_ratio=self._s.candle_max_recent_missing_ratio,
            outlier_z=self._s.candle_outlier_zscore,
        )

    # ------------------------------------------------------------------ reference candles

    def _reference_timeframes(self, timeframes: list[Timeframe]) -> list[Timeframe]:
        if self._reference is None or not self._s.candle_reference_enabled:
            return []
        return [tf for tf in timeframes if tf in self._reference.supported_timeframes]

    async def _reference_candles(self, asset: UniverseAsset, tf: Timeframe) -> list[Candle]:
        assert self._reference is not None
        daily = tf == Timeframe.D1
        count = self._s.candle_reference_daily_count if daily else self._s.candle_reference_hourly_count
        ttl = self._s.candle_reference_daily_cache_seconds if daily else self._s.candle_reference_hourly_cache_seconds
        reference, ref_id = self._reference, asset.listing.source_id
        return await self._cache.get_or_load(
            ("reference", reference.name, ref_id, tf.value),
            lambda: reference.reference_candles(ref_id, tf, count),
            ttl,
        )

    async def _collect_reference(
        self,
        asset: UniverseAsset,
        tasks: dict[Timeframe, asyncio.Task[list[Candle]]],
        closed_by_tf: dict[Timeframe, list[Candle]],
        market_by_tf: dict[Timeframe, MarketRef],
    ) -> dict[Timeframe, CandleCrossCheck]:
        checks: dict[Timeframe, CandleCrossCheck] = {}
        source = self._reference.name if self._reference else None
        quotes: dict[str, float] = {}
        if tasks:
            try:
                listing = await self._listing.latest()
                quotes = {e.symbol: e.price_usd for e in listing.entries if e.symbol in USD_STABLE_QUOTES}
            except ListingUnavailable:
                quotes = {}
        for tf, task in tasks.items():
            try:
                reference = await task
            except ProviderPlanLimited as exc:
                checks[tf] = unverified(tf, f"not available on the current CoinMarketCap plan ({exc.message})", source)
                continue
            except ProviderError as exc:
                checks[tf] = unverified(tf, f"reference unavailable: {exc.message}", source)
                continue
            if tf not in closed_by_tf:
                checks[tf] = unverified(tf, "exchange candles unavailable", source)
                continue
            rate, _ = quote_to_usd_rate(market_by_tf[tf].quote_asset, quotes)
            checks[tf] = cross_validate_candles(
                closed_by_tf[tf],
                reference,
                tf,
                usd_rate=rate,
                reference_source=source or "reference",
                warn_pct=self._s.candle_reference_warn_pct,
                max_pct=self._s.candle_reference_max_pct,
                min_overlap=self._s.candle_reference_min_overlap,
            )
        return checks

    # ------------------------------------------------------------------ candles endpoint

    async def candles(self, symbol: str, timeframe: Timeframe, limit: int) -> CandlesOut:
        key = ("candles", symbol.upper(), timeframe.value, limit)
        return await self._cache.get_or_load(
            key, lambda: self._load_candles(symbol, timeframe, limit), self._s.asset_detail_cache_seconds
        )

    async def _load_candles(self, symbol: str, timeframe: Timeframe, limit: int) -> CandlesOut:
        asset = await self._asset(symbol)
        if not asset.supported:
            raise AssetUnsupported(asset.symbol, asset.unsupported_reason or "no spot market")
        raw, market = await self._router.candles(asset.markets, timeframe, limit)
        closed, report = self._validate(timeframe, raw)
        forming = next((c for c in reversed(raw) if not c.is_closed), None)
        await self._store_candles(asset.symbol, {timeframe: closed})
        return CandlesOut(
            generated_at=utcnow(),
            symbol=asset.symbol,
            timeframe=timeframe.value,
            label=timeframe.label,
            source=market.adapter,
            market_symbol=market.symbol,
            quote_asset=market.quote_asset,
            candles=[_candle_out(c) for c in closed],
            forming=_candle_out(forming) if forming else None,
            validation=_timeframe_out(timeframe, report, closed, market),
        )

    # ------------------------------------------------------------------ detail endpoint

    async def detail(self, symbol: str) -> AssetDetailOut:
        key = ("detail", symbol.upper())
        return await self._cache.get_or_load(key, lambda: self._load_detail(symbol), self._s.asset_detail_cache_seconds)

    async def _load_detail(self, symbol: str) -> AssetDetailOut:
        asset = await self._asset(symbol)
        timeframes = sorted(set(self._s.required_timeframes) | {Timeframe.M5}, key=lambda t: t.seconds)
        errors: list[str] = []
        listing_out = ListingInfoOut(
            source=asset.listing.source,
            price_usd=asset.listing.price_usd,
            market_cap_usd=asset.listing.market_cap_usd,
            volume_24h_usd=asset.listing.volume_24h_usd,
            pct_change_1h=asset.listing.pct_change_1h,
            pct_change_24h=asset.listing.pct_change_24h,
            pct_change_7d=asset.listing.pct_change_7d,
            last_updated=asset.listing.last_updated,
        )

        ticker: Ticker | None = None
        ticker_market: MarketRef | None = None
        book_summary: OrderBookSummary | None = None
        reports: dict[Timeframe, CandleValidationReport] = {}
        closed_by_tf: dict[Timeframe, list[Candle]] = {}
        market_by_tf: dict[Timeframe, MarketRef] = {}
        tf_outputs: list[TimeframeValidationOut] = []
        candle_checks: dict[Timeframe, CandleCrossCheck] = {}

        reference_tfs = self._reference_timeframes(timeframes) if asset.supported else []
        reference_tasks: dict[Timeframe, asyncio.Task[list[Candle]]] = {}
        if reference_tfs and self._reference is not None and asset.listing.source != self._reference.name:
            for tf in reference_tfs:
                candle_checks[tf] = unverified(
                    tf, f"ranking served by {asset.listing.source}, so the CoinMarketCap id is unknown"
                )
        elif reference_tfs:
            reference_tasks = {tf: asyncio.create_task(self._reference_candles(asset, tf)) for tf in reference_tfs}

        if asset.supported:
            candle_results, ticker_result, book_result = await asyncio.gather(
                asyncio.gather(
                    *(self._router.candles(asset.markets, tf, self._s.candle_fetch_limit) for tf in timeframes),
                    return_exceptions=True,
                ),
                self._router.tickers({asset.symbol: asset.markets}),
                self._router.order_book(asset.markets, self._s.orderbook_depth_levels),
                return_exceptions=True,
            )
            if isinstance(ticker_result, BaseException):
                errors.append(f"ticker: {ticker_result}")
            else:
                found, ticker_errors = ticker_result
                if asset.symbol in found:
                    ticker, ticker_market = found[asset.symbol]
                errors.extend(f"ticker: {e}" for e in ticker_errors.get(asset.symbol, []))
            if isinstance(book_result, NoMarketData):
                errors.append(f"order book: {book_result}")
            elif isinstance(book_result, BaseException):
                errors.append(f"order book: {type(book_result).__name__}: {book_result}")
            else:
                book, _ = book_result
                book_summary = summarize_order_book(book, self._s.orderbook_band_pct)
            candle_list = candle_results if not isinstance(candle_results, BaseException) else [candle_results] * len(timeframes)
            for tf, outcome in zip(timeframes, candle_list, strict=True):
                if isinstance(outcome, BaseException):
                    message = str(outcome)
                    errors.append(f"{tf.label} candles: {message}")
                    tf_outputs.append(_timeframe_out(tf, None, [], None, error=message))
                    continue
                raw, market = outcome
                closed, report = self._validate(tf, raw)
                reports[tf], closed_by_tf[tf], market_by_tf[tf] = report, closed, market
                tf_outputs.append(_timeframe_out(tf, report, closed, market))
        else:
            errors.append(asset.unsupported_reason or "no supported spot market")
            tf_outputs = [_timeframe_out(tf, None, [], None, error="no supported spot market") for tf in timeframes]

        candle_checks.update(await self._collect_reference(asset, reference_tasks, closed_by_tf, market_by_tf))
        cross = await self._cross_check(asset, ticker, ticker_market)
        volatility: VolatilityCheck | None = (
            check_volatility(
                closed_by_tf[Timeframe.M5],
                ratio_threshold=self._s.extreme_volatility_ratio,
                min_move_pct=self._s.extreme_volatility_min_move_pct,
                absolute_move_pct=self._s.extreme_volatility_absolute_move_pct,
            )
            if Timeframe.M5 in closed_by_tf
            else None
        )
        market_source = ticker_market.adapter if ticker_market else (asset.primary_market.adapter if asset.primary_market else None)
        provider_status = self._health.status_of(market_source) if market_source else ProviderStatus.DOWN
        result = self._gate.evaluate(
            IntegrityInputs(
                symbol=asset.symbol,
                now=utcnow(),
                market_source=market_source,
                provider_status=provider_status,
                ticker=ticker,
                ticker_max_age_seconds=self._s.ticker_max_age_seconds,
                candle_reports=reports,
                required_timeframes=self._s.required_timeframes,
                cross_check=cross,
                volatility=volatility,
                require_cross_validation=self._s.require_price_cross_validation,
                signal_paused_reason=self._state.signal_paused_reason,
                candle_cross_checks=candle_checks,
            )
        )
        if not result.passed:
            log.info(
                "integrity gate blocked",
                extra={"symbol": asset.symbol, "state": result.state.value, "reasons": result.reasons[:5]},
            )

        persistence_status = await self._store_candles(asset.symbol, closed_by_tf, book_summary)
        return AssetDetailOut(
            generated_at=utcnow(),
            symbol=asset.symbol,
            name=asset.name,
            universe_rank=asset.universe_rank,
            supported=asset.supported,
            unsupported_reason=asset.unsupported_reason,
            markets=_markets(asset),
            listing=listing_out,
            ticker=TickerOut.model_validate(ticker) if ticker else None,
            order_book=OrderBookSummaryOut.model_validate(book_summary) if book_summary else None,
            timeframes=tf_outputs,
            cross_check=CrossCheckOut.model_validate(cross) if cross else None,
            candle_cross_checks=[
                CandleCrossCheckOut(
                    timeframe=check.timeframe.value,
                    label=check.timeframe.label,
                    status=check.status,
                    reference_source=check.reference_source,
                    compared=check.compared,
                    median_deviation_pct=check.median_deviation_pct,
                    max_deviation_pct=check.max_deviation_pct,
                    outliers=check.outliers,
                    reason=check.reason,
                )
                for check in sorted(candle_checks.values(), key=lambda c: c.timeframe.seconds)
            ],
            volatility=VolatilityOut.model_validate(volatility) if volatility else None,
            integrity=integrity_out(result),
            errors=errors,
            persistence=persistence_status,
        )

    async def _cross_check(
        self, asset: UniverseAsset, ticker: Ticker | None, market: MarketRef | None
    ) -> CrossCheck | None:
        if ticker is None or market is None:
            return None
        try:
            listing = await self._listing.latest()
            entries = {e.symbol: e for e in listing.entries}
        except ListingUnavailable:
            entries = {}
        reference = entries.get(asset.symbol, asset.listing)
        quotes = {s: e.price_usd for s, e in entries.items() if s in USD_STABLE_QUOTES}
        rate, _ = quote_to_usd_rate(market.quote_asset, quotes)
        now = utcnow()
        return cross_validate_price(
            primary_price_usd=ticker.last_price * rate if rate else None,
            primary_source=market.adapter,
            reference_price_usd=reference.price_usd,
            reference_source=reference.source,
            reference_updated_at=reference.last_updated,
            now=now,
            warn_pct=self._s.cross_source_warn_deviation_pct,
            max_pct=self._s.cross_source_max_deviation_pct,
            max_reference_age_seconds=self._s.reference_max_age_seconds,
        )

    async def _store_candles(
        self,
        base_asset: str,
        closed_by_tf: dict[Timeframe, list[Candle]],
        book: OrderBookSummary | None = None,
    ) -> str:
        if self._sessions is None:
            return "disabled"
        try:
            async with self._sessions() as session:
                for candles in closed_by_tf.values():
                    await persistence.upsert_candles(session, base_asset, candles)
                if book is not None:
                    persistence.add_orderbook_snapshot(session, base_asset, book)
                await session.commit()
        except Exception:
            log.exception("candle persistence failed", extra={"symbol": base_asset})
            return "failed"
        return "ok"
