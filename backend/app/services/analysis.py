"""Phase 2 analysis service: runs the signal engine on collected data, persists, scans.

Per asset: AssetService.collect (candles, ticker, order book, integrity gate; shared with
the detail endpoint) -> market regime -> SignalEngine -> persistence -> API model.

Persistence keeps history without flooding the database: technical features once per
closed candle (1H, 4H, 1D by default) and a signal row only when the label changes or
a new 4H setup candle has closed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.engine import STRATEGY, AnalysisInputs, AnalysisResult, EngineParams, SignalEngine, ENGINE_VERSION
from app.analysis.features import FEATURE_VERSION
from app.analysis.regime import MarketRegimeResult
from app.analysis.scoring import SignalParams
from app.config import Settings
from app.core.enums import SignalLabel, Timeframe
from app.core.timeutil import utcnow
from app.risk.params import RiskParams
from app.schemas.api import (
    AnalysisOut,
    FactorOut,
    FearGreedOut,
    GlobalMetricsOut,
    IndicatorSetOut,
    LevelOut,
    MarketRegimeOut,
    PipelineStageOut,
    PivotOut,
    RiskCheckOut,
    SignalHistoryOut,
    SignalScanOut,
    SignalSummaryOut,
    StoredSignalOut,
    StoredTargetOut,
    StructureBreakOut,
    StructureOut,
    TargetOut,
    TimeframeRegimeOut,
    TradePlanOut,
)
from app.services import persistence
from app.services.assets import AssetCollection, AssetService
from app.services.cache import AsyncTTLCache
from app.services.regime import MarketRegimeService
from app.services.universe import UniverseAsset, UniverseService

log = logging.getLogger(__name__)


def engine_params(s: Settings) -> EngineParams:
    return EngineParams(
        signal=SignalParams(
            min_score_strong_buy=s.signal_min_score_strong_buy,
            min_score_buy=s.signal_min_score_buy,
            min_score_watch=s.signal_min_score_watch,
        ),
        risk=RiskParams(
            max_risk_per_signal_pct=s.risk_max_per_signal_pct,
            max_allocation_pct=s.risk_max_allocation_pct,
            fee_pct=s.risk_fee_pct,
            slippage_pct=s.risk_slippage_pct,
            min_reward_risk=s.risk_min_reward_risk,
            strong_min_reward_risk=s.risk_strong_min_reward_risk,
            min_room_r=s.risk_min_room_r,
            strong_min_room_r=s.risk_strong_min_room_r,
            max_stop_pct=s.risk_max_stop_pct,
            max_spread_bps=s.risk_max_spread_bps,
            min_depth_usd=s.risk_min_depth_usd,
            min_volume_24h_usd=s.risk_min_volume_24h_usd,
            max_extension_atr=s.risk_max_extension_atr,
            max_change_24h_pct=s.risk_max_change_24h_pct,
        ),
    )


# ----------------------------------------------------------------------------- API mapping


def regime_out(m: MarketRegimeResult) -> MarketRegimeOut:
    return MarketRegimeOut(
        computed_at=m.computed_at,
        regime=m.regime,
        max_signal=m.max_signal,
        btc_trend=m.btc_trend,
        btc_close=m.btc_close,
        btc_ema50=m.btc_ema50,
        btc_ema200=m.btc_ema200,
        btc_vs_ema200_pct=m.btc_vs_ema200_pct,
        btc_roc20=m.btc_roc20,
        btc_atr_pct=m.btc_atr_pct,
        volatility=m.volatility,
        breadth_pct=m.breadth_pct,
        breadth_sample=m.breadth_sample,
        fear_greed=FearGreedOut.model_validate(m.fear_greed) if m.fear_greed else None,
        global_metrics=GlobalMetricsOut.model_validate(m.global_metrics) if m.global_metrics else None,
        flags=list(m.flags),
        reasons=list(m.reasons),
        errors=list(m.errors),
    )


def _plan_out(r: AnalysisResult) -> TradePlanOut | None:
    p = r.plan
    if p is None:
        return None
    data = asdict(p)
    data["targets"] = [TargetOut(**t) for t in data["targets"]]
    return TradePlanOut(**data)


def _structure_out(r: AnalysisResult) -> list[StructureOut]:
    out = []
    for tf in sorted(r.structures, key=lambda t: t.seconds):
        s = r.structures[tf]
        brk = s.last_break
        out.append(
            StructureOut(
                timeframe=tf.value,
                label=tf.label,
                trend=s.trend,
                reason=s.reason,
                last_swing_high=PivotOut(time=s.highs[-1].time, price=s.highs[-1].price) if s.highs else None,
                last_swing_low=PivotOut(time=s.lows[-1].time, price=s.lows[-1].price) if s.lows else None,
                last_break=StructureBreakOut(
                    direction=brk.direction, level=brk.level, time=brk.time, candles_ago=brk.candles_ago
                ) if brk else None,
                supports=[LevelOut.model_validate(level) for level in s.supports],
                resistances=[LevelOut.model_validate(level) for level in s.resistances],
            )
        )
    return out


def analysis_out(r: AnalysisResult, persistence_status: str, sentiment: dict[str, Any] | None = None) -> AnalysisOut:
    ordered = sorted(r.snapshots, key=lambda t: t.seconds)
    return AnalysisOut(
        generated_at=r.generated_at,
        symbol=r.symbol,
        name=r.name,
        universe_rank=r.universe_rank,
        engine_version=r.engine_version,
        feature_version=r.feature_version,
        strategy=r.strategy,
        setup_timeframe=r.setup_timeframe.label,
        signal=r.signal,
        score=r.score,
        score_label=r.score_label,
        summary=r.summary,
        trend=r.trend,
        price=r.price,
        quote_asset=r.quote_asset,
        market_source=r.market_source,
        data_state=r.data_state,
        data_health_score=r.data_health_score,
        integrity_passed=r.integrity_passed,
        pipeline=[PipelineStageOut(**asdict(stage)) for stage in r.pipeline],
        factors=[FactorOut(**asdict(f)) for f in r.factors],
        plan=_plan_out(r),
        risk_checks=[RiskCheckOut(**asdict(c)) for c in r.risk_checks],
        reasons=r.reasons,
        risks=r.risks,
        indicators=[
            IndicatorSetOut(**{**r.snapshots[tf].as_dict(), "timeframe": tf.value, "label": tf.label}) for tf in ordered
        ],
        structure=_structure_out(r),
        regimes=[
            TimeframeRegimeOut(
                timeframe=tf.value,
                label=tf.label,
                trend=g.trend,
                regime=g.label,
                adx=g.adx,
                strength=g.strength,
                volatility=g.volatility,
                atr_pct_percentile=g.atr_pct_percentile,
                reasons=g.reasons,
            )
            for tf, g in sorted(r.regimes.items(), key=lambda item: item[0].seconds)
        ],
        market_regime=regime_out(r.market),
        persistence=persistence_status,
        sentiment=sentiment,
    )


def summary_out(a: AnalysisOut) -> SignalSummaryOut:
    plan = a.plan
    return SignalSummaryOut(
        symbol=a.symbol,
        name=a.name,
        universe_rank=a.universe_rank,
        signal=a.signal,
        score=a.score,
        trend=a.trend,
        price=a.price,
        quote_asset=a.quote_asset,
        market_source=a.market_source,
        data_state=a.data_state,
        entry_low=plan.entry_low if plan else None,
        entry_high=plan.entry_high if plan else None,
        stop_loss=plan.stop_loss if plan else None,
        take_profit_1=plan.targets[0].price if plan else None,
        take_profit_2=plan.targets[1].price if plan else None,
        reward_risk=plan.reward_risk if plan else None,
        suggested_allocation_pct=plan.suggested_allocation_pct if plan and plan.actionable else None,
        summary=a.summary,
        reasons=a.reasons[:3],
    )


def _failed_summary(asset: UniverseAsset, error: str) -> SignalSummaryOut:
    return SignalSummaryOut(
        symbol=asset.symbol,
        name=asset.name,
        universe_rank=asset.universe_rank,
        signal=SignalLabel.NO_TRADE,
        score=0,
        trend="n/a",
        price=None,
        quote_asset=None,
        market_source=None,
        data_state="API_FAILURE",
        entry_low=None,
        entry_high=None,
        stop_loss=None,
        take_profit_1=None,
        take_profit_2=None,
        reward_risk=None,
        suggested_allocation_pct=None,
        summary=f"NO TRADE: analysis failed ({error})",
        reasons=[f"analysis failed: {error}"],
    )


# ----------------------------------------------------------------------------- service


class AnalysisService:
    def __init__(
        self,
        settings: Settings,
        universe: UniverseService,
        assets: AssetService,
        regime: MarketRegimeService,
        session_factory: async_sessionmaker[AsyncSession] | None,
    ) -> None:
        self._s = settings
        self._universe = universe
        self._assets = assets
        self._regime = regime
        self._sessions = session_factory
        self._engine = SignalEngine(engine_params(settings))
        self._cache = AsyncTTLCache()
        self._last_signal: dict[str, tuple[str, str | None]] = {}
        self._feature_timeframes = {Timeframe(value) for value in settings.feature_persist_timeframes}
        # Phase 5 context hooks (set by the container): sentiment per coin, refreshed before scans.
        self.sentiment_for: Callable[[str], Any] | None = None
        self.before_scan: Callable[[], Awaitable[Any]] | None = None

    @property
    def risk_params(self) -> RiskParams:
        return self._engine.params.risk

    def set_risk_params(self, risk: RiskParams) -> None:
        """Apply edited risk settings (Phase 4) to every following analysis."""
        self._engine = SignalEngine(replace(self._engine.params, risk=risk))
        self._cache.invalidate()

    def cached(self, symbol: str) -> AnalysisOut | None:
        """Latest analysis for `symbol` without running anything (may be expired)."""
        entry = self._cache.peek(("analysis", symbol.upper()))
        return entry[0] if entry else None

    async def analyze(self, symbol: str, *, force: bool = False) -> AnalysisOut:
        key = ("analysis", symbol.upper())
        return await self._cache.get_or_load(
            key, lambda: self._analyze(symbol), self._s.analysis_cache_seconds, force=force
        )

    async def _analyze(self, symbol: str) -> AnalysisOut:
        collection = await self._assets.collect(symbol)
        market = await self._regime.current()
        result = self._engine.evaluate(self._inputs(collection, market))
        sentiment = self.sentiment_for(result.symbol) if self.sentiment_for else None
        if sentiment is not None:
            # Context only: notes join the risks, the label and score stay the engine's.
            result.risks.extend(f"sentiment: {note}" for note in sentiment.notes)
        if result.signal in (SignalLabel.BUY, SignalLabel.STRONG_BUY):
            log.info("signal", extra={"symbol": result.symbol, "signal": result.signal.value, "score": result.score})
        return analysis_out(result, await self._persist(result), asdict(sentiment) if sentiment is not None else None)

    @staticmethod
    def _inputs(c: AssetCollection, market: MarketRegimeResult) -> AnalysisInputs:
        asset, ticker = c.asset, c.ticker
        market_ref = c.ticker_market or asset.primary_market
        return AnalysisInputs(
            symbol=asset.symbol,
            name=asset.name,
            universe_rank=asset.universe_rank,
            now=utcnow(),
            supported=asset.supported,
            unsupported_reason=asset.unsupported_reason,
            candles=c.closed,
            price=ticker.last_price if ticker else None,
            quote_asset=market_ref.quote_asset if market_ref else None,
            market_source=market_ref.adapter if market_ref else None,
            quote_usd_rate=c.quote_usd_rate,
            pct_change_24h=ticker.pct_change_24h if ticker else None,
            volume_24h_quote=ticker.volume_quote_24h if ticker else None,
            order_book=c.book,
            integrity=c.integrity,
            market=market,
        )

    async def run_scan(
        self,
        *,
        force: bool = True,
        on_start: Callable[[int], None] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> SignalScanOut:
        """Analyse the whole universe now. Runs only when called (Analyze now / auto schedule)."""
        if self.before_scan is not None:
            try:
                await asyncio.wait_for(self.before_scan(), timeout=60)
            except asyncio.CancelledError:
                raise
            except Exception:  # sentiment is context; a scan never fails because of it
                log.warning("sentiment refresh before scan failed", exc_info=True)
        universe = await self._universe.get(force=force)
        market = await self._regime.current(force=force)
        semaphore = asyncio.Semaphore(max(1, self._s.signal_scan_concurrency))
        if on_start:
            on_start(len(universe.assets))

        async def one(asset: UniverseAsset) -> tuple[SignalSummaryOut, str | None]:
            async with semaphore:
                try:
                    row = summary_out(await self.analyze(asset.symbol, force=force)), None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # one asset must never take the whole scan down
                    log.exception("analysis failed", extra={"symbol": asset.symbol})
                    message = f"{type(exc).__name__}: {exc}"
                    row = _failed_summary(asset, message), f"{asset.symbol}: {message}"
                row[0].watchlist = asset.watchlist
                if on_progress:
                    on_progress(asset.symbol)
                return row

        outcomes = await asyncio.gather(*(one(a) for a in universe.assets))
        rows = sorted(
            (row for row, _ in outcomes), key=lambda r: (-r.signal.rank, -r.score, r.universe_rank)
        )
        counts = Counter(row.signal.value for row in rows)
        return SignalScanOut(
            generated_at=utcnow(),
            engine_version=ENGINE_VERSION,
            strategy=STRATEGY,
            market_regime=regime_out(market),
            counts={label.value: counts.get(label.value, 0) for label in SignalLabel},
            signals=rows,
            errors=[error for _, error in outcomes if error],
        )

    # ------------------------------------------------------------------ persistence

    async def _persist(self, r: AnalysisResult) -> str:
        if self._sessions is None or not self._s.signal_persist_enabled:
            return "disabled"
        setup = r.setup_candle_open_time.isoformat() if r.setup_candle_open_time else None
        key = (r.signal.value, setup)
        try:
            async with self._sessions() as session:
                snapshots = [s for tf, s in r.snapshots.items() if tf in self._feature_timeframes]
                await persistence.insert_features(session, r.symbol, snapshots, FEATURE_VERSION)
                last = self._last_signal.get(r.symbol)
                if last is None:
                    last = await persistence.latest_signal_key(session, r.symbol)
                stored = last != key
                if stored:
                    await persistence.add_signal(
                        session, r, input_features=self._input_features(r, setup), quant_output=self._quant_output(r)
                    )
                await session.commit()
        except Exception:
            log.exception("analysis persistence failed", extra={"symbol": r.symbol})
            return "failed"
        self._last_signal[r.symbol] = key
        return "ok"

    @staticmethod
    def _input_features(r: AnalysisResult, setup: str | None) -> dict[str, Any]:
        return {
            "setup_candle_open_time": setup,
            "price": r.price,
            "quote_asset": r.quote_asset,
            "market_source": r.market_source,
            "snapshots": {tf.value: s.as_dict() for tf, s in r.snapshots.items()},
            "structure": {
                tf.value: {
                    "trend": s.trend.value,
                    "reason": s.reason,
                    "supports": [round(level.price, 10) for level in s.supports],
                    "resistances": [round(level.price, 10) for level in s.resistances],
                    "last_break": asdict(s.last_break) | {"time": s.last_break.time.isoformat()} if s.last_break else None,
                }
                for tf, s in r.structures.items()
            },
            "regimes": {tf.value: {"trend": g.trend.value, "regime": g.label.value, "adx": g.adx} for tf, g in r.regimes.items()},
        }

    @staticmethod
    def _quant_output(r: AnalysisResult) -> dict[str, Any]:
        return {
            "score": r.score,
            "score_label": r.score_label.value,
            "factors": [asdict(f) for f in r.factors],
            "risk_checks": [asdict(c) | {"severity": c.severity.value} for c in r.risk_checks],
            "pipeline": [asdict(stage) for stage in r.pipeline],
            "plan": asdict(r.plan) if r.plan else None,
            "market_regime": r.market.regime.value,
            "market_max_signal": r.market.max_signal.value,
        }

    async def history(self, symbol: str | None, limit: int) -> SignalHistoryOut:
        symbol = symbol.upper() if symbol else None
        if self._sessions is None:
            return SignalHistoryOut(generated_at=utcnow(), symbol=symbol, persistence="disabled", signals=[])
        async with self._sessions() as session:
            rows = await persistence.recent_signals(session, symbol, limit)
        return SignalHistoryOut(
            generated_at=utcnow(),
            symbol=symbol,
            persistence="ok",
            signals=[
                StoredSignalOut(
                    id=s.id,
                    created_at=s.created_at,
                    symbol=s.symbol,
                    timeframe=s.timeframe,
                    strategy=s.strategy,
                    signal=s.signal,
                    signal_score=s.signal_score,
                    data_state=s.data_state,
                    data_health_score=s.data_health_score,
                    entry_low=s.entry_low,
                    entry_high=s.entry_high,
                    stop_loss=s.stop_loss,
                    risk_reward=s.risk_reward,
                    trend=s.trend,
                    market_regime=s.market_regime,
                    summary=s.summary,
                    status=s.status,
                    engine_version=s.engine_version,
                    targets=[StoredTargetOut.model_validate(t) for t in targets],
                )
                for s, targets in rows
            ],
        )
