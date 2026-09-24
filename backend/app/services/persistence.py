"""Database reads and writes (dialect-aware upserts, batched inserts)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import utcnow
from app.data.health import ProviderHealth
from app.data.normalization.schemas import Candle
from app.data.validation.orderbook import OrderBookSummary
from app.models import (
    Asset,
    MarketRegime,
    MarketSnapshot,
    OrderbookSnapshot,
    ProviderHealthRecord,
    Signal,
    SignalTarget,
    SystemEvent,
    TechnicalFeature,
)
from app.models import Candle as CandleRow

if TYPE_CHECKING:
    from app.analysis.engine import AnalysisResult
    from app.analysis.features import IndicatorSnapshot
    from app.analysis.regime import MarketRegimeResult

_BATCH = 500


def json_safe(value: Any) -> Any:
    """Replace NaN and infinity (rejected by PostgreSQL JSONB) with None, recursively."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    return value


def _insert(session: AsyncSession, table: Any) -> Any:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql.insert(table)
    if dialect == "sqlite":
        return sqlite.insert(table)
    raise RuntimeError(f"unsupported database dialect: {dialect}")


async def upsert_universe_assets(session: AsyncSession, universe: Any) -> None:
    """Mark current universe members, keep history of previous members (in_universe=false)."""
    now = utcnow()
    await session.execute(update(Asset).values(in_universe=False))
    for asset in universe.assets:
        primary = asset.primary_market
        values = {
            "symbol": asset.symbol,
            "name": asset.name[:128],
            "slug": asset.slug,
            "listing_source": asset.listing.source,
            "listing_source_id": asset.listing.source_id,
            "market_source": primary.adapter if primary else None,
            "market_symbol": primary.symbol if primary else None,
            "rank": asset.universe_rank,
            "market_cap_usd": asset.listing.market_cap_usd,
            "is_stablecoin": False,
            "is_wrapped": False,
            "in_universe": True,
            "supported": asset.supported,
            "unsupported_reason": asset.unsupported_reason,
            "tags": list(asset.listing.tags),
            "updated_at": now,
        }
        stmt = _insert(session, Asset).values(**values)
        update_cols = {k: stmt.excluded[k] for k in values if k != "symbol"}
        await session.execute(stmt.on_conflict_do_update(index_elements=["symbol"], set_=update_cols))


async def upsert_candles(session: AsyncSession, base_asset: str, candles: Sequence[Candle]) -> int:
    rows = [
        {
            "source": c.source,
            "symbol": c.symbol,
            "base_asset": base_asset,
            "timeframe": c.timeframe.value,
            "open_time": c.open_time,
            "close_time": c.close_time,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "quote_volume": c.quote_volume,
            "trades": c.trades,
            "taker_buy_base": c.taker_buy_base,
        }
        for c in candles
        if c.is_closed
    ]
    for start in range(0, len(rows), _BATCH):
        chunk = rows[start : start + _BATCH]
        stmt = _insert(session, CandleRow).values(chunk)
        stmt = stmt.on_conflict_do_update(
            index_elements=["source", "symbol", "timeframe", "open_time"],
            set_={
                col: stmt.excluded[col]
                for col in ("close_time", "open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_base")
            },
        )
        await session.execute(stmt)
    return len(rows)


def add_market_snapshot_rows(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    session.add_all(MarketSnapshot(**row) for row in rows)


def add_orderbook_snapshot(session: AsyncSession, base_asset: str, summary: OrderBookSummary) -> None:
    session.add(
        OrderbookSnapshot(
            captured_at=utcnow(),
            source=summary.source,
            symbol=summary.symbol,
            base_asset=base_asset,
            valid=summary.valid,
            best_bid=summary.best_bid,
            best_ask=summary.best_ask,
            spread_bps=summary.spread_bps,
            band_pct=summary.band_pct,
            bid_depth_quote=summary.bid_depth_quote,
            ask_depth_quote=summary.ask_depth_quote,
            imbalance=summary.imbalance,
            issues=list(summary.issues),
        )
    )


def add_provider_health(session: AsyncSession, items: Sequence[ProviderHealth]) -> None:
    now = utcnow()
    session.add_all(
        ProviderHealthRecord(
            recorded_at=now,
            provider=item.provider,
            role=item.role,
            status=item.status.value,
            circuit_state=item.circuit_state,
            consecutive_failures=item.consecutive_failures,
            total_requests=item.total_requests,
            total_failures=item.total_failures,
            rate_limit_hits=item.rate_limit_hits,
            avg_latency_ms=item.avg_latency_ms,
            last_error=item.last_error,
            details=dict(item.details),
        )
        for item in items
    )


def add_system_event(
    session: AsyncSession, level: str, event_type: str, message: str, details: dict[str, Any] | None = None
) -> None:
    session.add(SystemEvent(level=level, event_type=event_type, message=message, details=details or {}))


# ----------------------------------------------------------------------------- Phase 2


def add_market_regime(session: AsyncSession, result: MarketRegimeResult, engine_version: str) -> None:
    g, fg = result.global_metrics, result.fear_greed
    session.add(
        MarketRegime(
            computed_at=result.computed_at,
            regime=result.regime.value,
            total_market_cap_usd=g.total_market_cap_usd if g else None,
            btc_dominance_pct=g.btc_dominance_pct if g else None,
            fear_greed_value=fg.value if fg else None,
            fear_greed_source=fg.source if fg else None,
            breadth_pct=result.breadth_pct,
            volatility_pct=result.btc_atr_pct,
            engine_version=engine_version,
            details=json_safe({
                "max_signal": result.max_signal.value,
                "btc_trend": result.btc_trend.value if result.btc_trend else None,
                "btc_close": result.btc_close,
                "btc_ema50": result.btc_ema50,
                "btc_ema200": result.btc_ema200,
                "btc_roc20": result.btc_roc20,
                "volatility": result.volatility.value,
                "breadth_sample": result.breadth_sample,
                "flags": list(result.flags),
                "reasons": list(result.reasons),
                "errors": list(result.errors)[:20],
            }),
        )
    )


async def insert_features(
    session: AsyncSession, symbol: str, snapshots: Sequence[IndicatorSnapshot], feature_version: str
) -> None:
    """One row per (symbol, timeframe, closed candle, feature version); existing rows are kept."""
    rows = [
        {
            "symbol": symbol,
            "timeframe": snap.timeframe.value,
            "candle_open_time": snap.open_time,
            "feature_version": feature_version,
            "features": json_safe(snap.as_dict()),
        }
        for snap in snapshots
    ]
    if not rows:
        return
    stmt = _insert(session, TechnicalFeature).values(rows)
    await session.execute(
        stmt.on_conflict_do_nothing(index_elements=["symbol", "timeframe", "candle_open_time", "feature_version"])
    )


async def latest_signal_key(session: AsyncSession, symbol: str) -> tuple[str, str | None] | None:
    """(label, setup candle open time) of the newest stored signal for `symbol`."""
    row = (
        await session.execute(
            select(Signal.signal, Signal.input_features).where(Signal.symbol == symbol).order_by(Signal.id.desc()).limit(1)
        )
    ).first()
    if row is None:
        return None
    features = row.input_features or {}
    return row.signal, features.get("setup_candle_open_time")


async def add_signal(
    session: AsyncSession,
    result: AnalysisResult,
    *,
    input_features: dict[str, Any],
    quant_output: dict[str, Any],
) -> Signal:
    """Insert a signal and its targets (TP1-TP3 and the stop as SL)."""
    plan = result.plan
    signal = Signal(
        symbol=result.symbol,
        timeframe=result.setup_timeframe.value,
        strategy=result.strategy,
        signal=result.signal.value,
        signal_score=result.score,
        data_health_score=result.data_health_score,
        data_state=result.data_state.value,
        entry_low=plan.entry_low if plan else None,
        entry_high=plan.entry_high if plan else None,
        stop_loss=plan.stop_loss if plan else None,
        risk_reward=round(plan.reward_risk, 4) if plan else None,
        trend=result.trend[:16],
        market_regime=result.market.regime.value,
        reasons=list(result.reasons),
        risks=list(result.risks),
        invalidation=plan.invalidation if plan else None,
        summary=result.summary,
        status="OPEN" if plan is not None and plan.actionable else "INFO",
        is_backtest=False,
        ai_confirmed=False,
        feature_version=result.feature_version,
        engine_version=result.engine_version,
        input_features=json_safe(input_features),
        quant_output=json_safe(quant_output),
    )
    session.add(signal)
    if plan is not None:
        await session.flush()  # assigns signal.id for the targets
        session.add_all(
            SignalTarget(
                signal_id=signal.id, kind="TP", level_index=index, price=target.price, allocation_pct=target.allocation_pct
            )
            for index, target in enumerate(plan.targets, start=1)
        )
        session.add(SignalTarget(signal_id=signal.id, kind="SL", level_index=1, price=plan.stop_loss, allocation_pct=100.0))
    return signal


async def recent_signals(session: AsyncSession, symbol: str | None, limit: int) -> list[tuple[Signal, list[SignalTarget]]]:
    stmt = select(Signal).order_by(Signal.id.desc()).limit(limit)
    if symbol:
        stmt = stmt.where(Signal.symbol == symbol)
    signals = list((await session.execute(stmt)).scalars())
    if not signals:
        return []
    targets = (
        await session.execute(
            select(SignalTarget).where(SignalTarget.signal_id.in_([s.id for s in signals])).order_by(
                SignalTarget.kind.desc(), SignalTarget.level_index
            )
        )
    ).scalars()
    by_signal: dict[int, list[SignalTarget]] = {}
    for target in targets:
        by_signal.setdefault(target.signal_id, []).append(target)
    return [(s, by_signal.get(s.id, [])) for s in signals]
