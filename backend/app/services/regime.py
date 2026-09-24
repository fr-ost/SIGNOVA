"""Market regime service: Bitcoin's daily trend, universe breadth and market context.

Breadth is the share of supported universe assets whose daily close is above their daily
EMA50. Daily candles are validated like every other candle series; an asset whose daily
history fails validation is left out of the breadth sample rather than guessed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.engine import ENGINE_VERSION
from app.analysis.features import IndicatorSnapshot, compute_snapshot
from app.analysis.regime import MarketRegimeResult, classify_market_regime
from app.config import Settings
from app.core.enums import MarketRegimeLabel, Timeframe
from app.core.timeutil import utcnow
from app.data.normalization.schemas import Candle
from app.data.validation.candles import CandleValidationReport
from app.services import persistence
from app.services.cache import AsyncTTLCache
from app.services.context import MarketContextService
from app.services.spot_router import NoMarketData, SpotMarketRouter
from app.services.universe import UniverseAsset, UniverseService

log = logging.getLogger(__name__)

BREADTH_CANDLES = 250  # EMA50 plus warm-up; Bitcoin gets the full long history for EMA200

Validator = Callable[[Timeframe, list[Candle]], tuple[list[Candle], CandleValidationReport]]


class MarketRegimeService:
    def __init__(
        self,
        settings: Settings,
        universe: UniverseService,
        router: SpotMarketRouter,
        context: MarketContextService,
        validate: Validator,
        session_factory: async_sessionmaker[AsyncSession] | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._s = settings
        self._universe = universe
        self._router = router
        self._context = context
        self._validate = validate
        self._sessions = session_factory
        self._clock = clock
        self._cache = AsyncTTLCache()
        self._last_persisted: tuple[MarketRegimeLabel, float] | None = None

    async def current(self, *, force: bool = False) -> MarketRegimeResult:
        return await self._cache.get_or_load("regime", self._compute, self._s.regime_cache_seconds, force=force)

    async def _daily(self, asset: UniverseAsset, semaphore: asyncio.Semaphore) -> tuple[str, IndicatorSnapshot | None, str | None]:
        limit = self._s.fetch_limit(Timeframe.D1) if asset.symbol == "BTC" else BREADTH_CANDLES
        async with semaphore:
            try:
                raw, _ = await self._router.candles(asset.markets, Timeframe.D1, limit)
            except NoMarketData as exc:
                return asset.symbol, None, str(exc)
        closed, report = self._validate(Timeframe.D1, raw)
        if not report.ok:
            return asset.symbol, None, f"{asset.symbol} 1D: {report.critical_issues[0]}"
        return asset.symbol, compute_snapshot(Timeframe.D1, closed), None

    async def _compute(self) -> MarketRegimeResult:
        universe = await self._universe.get()
        supported = [a for a in universe.assets if a.supported]
        semaphore = asyncio.Semaphore(max(1, self._s.signal_scan_concurrency))
        results = await asyncio.gather(*(self._daily(a, semaphore) for a in supported))
        snapshots = {symbol: snap for symbol, snap, _ in results if snap is not None}
        errors: list[str] = [error for _, _, error in results if error]
        if "BTC" not in {a.symbol for a in universe.assets}:
            errors.append("BTC is not in the current universe")
        breadth = [s.close > s.ema50 for s in snapshots.values() if s.ema50 is not None]
        context = await self._context.get()
        result = classify_market_regime(
            now=utcnow(),
            btc_daily=snapshots.get("BTC"),
            breadth=breadth,
            fear_greed=context.fear_greed,
            global_metrics=context.global_metrics,
            min_breadth_sample=self._s.regime_min_breadth_sample,
            errors=errors + list(context.errors),
        )
        await self._persist(result)
        return result

    def _due(self, regime: MarketRegimeLabel) -> bool:
        if self._last_persisted is None:
            return True
        label, at = self._last_persisted
        return label != regime or self._clock() - at >= self._s.regime_persist_min_interval_seconds

    async def _persist(self, result: MarketRegimeResult) -> None:
        if self._sessions is None or not self._s.signal_persist_enabled or not self._due(result.regime):
            return
        try:
            async with self._sessions() as session:
                persistence.add_market_regime(session, result, ENGINE_VERSION)
                await session.commit()
        except Exception:
            log.exception("market regime persistence failed")
            return
        self._last_persisted = (result.regime, self._clock())
