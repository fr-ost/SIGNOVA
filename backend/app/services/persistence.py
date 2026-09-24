"""Database writes for Phase 1 data (dialect-aware upserts, batched inserts)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timeutil import utcnow
from app.data.health import ProviderHealth
from app.data.normalization.schemas import Candle
from app.data.validation.orderbook import OrderBookSummary
from app.models import Asset, MarketSnapshot, OrderbookSnapshot, ProviderHealthRecord, SystemEvent
from app.models import Candle as CandleRow

_BATCH = 500


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
