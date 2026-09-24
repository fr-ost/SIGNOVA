"""Market data tables: assets, candles, snapshots, order books, features, regimes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Float, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK, CreatedAtMixin, JSONType, TZDateTime


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(128))
    listing_source: Mapped[str | None] = mapped_column(String(32))
    listing_source_id: Mapped[str | None] = mapped_column(String(64))
    market_source: Mapped[str | None] = mapped_column(String(32))
    market_symbol: Mapped[str | None] = mapped_column(String(32))
    rank: Mapped[int | None] = mapped_column(Integer)
    market_cap_usd: Mapped[float | None] = mapped_column(Float)
    is_stablecoin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_wrapped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    in_universe: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    unsupported_reason: Mapped[str | None] = mapped_column(String(255))
    tags: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now(), nullable=False)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("source", "symbol", "timeframe", "open_time", name="uq_candles_source_symbol_tf_time"),
        Index("ix_candles_base_tf_time", "base_asset", "timeframe", "open_time"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    base_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    open_time: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    close_time: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    quote_volume: Mapped[float | None] = mapped_column(Float)
    trades: Mapped[int | None] = mapped_column(Integer)
    taker_buy_base: Mapped[float | None] = mapped_column(Float)
    ingested_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now(), nullable=False)


class MarketSnapshot(CreatedAtMixin, Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_market_snapshots_symbol_captured", "symbol", "captured_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    universe_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    listing_source: Mapped[str] = mapped_column(String(32), nullable=False)
    market_source: Mapped[str | None] = mapped_column(String(32))
    price_usd: Mapped[float | None] = mapped_column(Float)
    reference_price_usd: Mapped[float | None] = mapped_column(Float)
    deviation_pct: Mapped[float | None] = mapped_column(Float)
    pct_change_24h: Mapped[float | None] = mapped_column(Float)
    volume_24h_usd: Mapped[float | None] = mapped_column(Float)
    market_cap_usd: Mapped[float | None] = mapped_column(Float)
    data_state: Mapped[str] = mapped_column(String(32), nullable=False)
    data_health_score: Mapped[int] = mapped_column(Integer, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class OrderbookSnapshot(CreatedAtMixin, Base):
    __tablename__ = "orderbook_snapshots"
    __table_args__ = (Index("ix_orderbook_snapshots_symbol_captured", "symbol", "captured_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    base_asset: Mapped[str] = mapped_column(String(32), nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    best_bid: Mapped[float | None] = mapped_column(Float)
    best_ask: Mapped[float | None] = mapped_column(Float)
    spread_bps: Mapped[float | None] = mapped_column(Float)
    band_pct: Mapped[float] = mapped_column(Float, nullable=False)
    bid_depth_quote: Mapped[float | None] = mapped_column(Float)
    ask_depth_quote: Mapped[float | None] = mapped_column(Float)
    imbalance: Mapped[float | None] = mapped_column(Float)
    issues: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)


class TechnicalFeature(CreatedAtMixin, Base):
    __tablename__ = "technical_features"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "timeframe", "candle_open_time", "feature_version", name="uq_technical_features_key"
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    candle_open_time: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)


class MarketRegime(CreatedAtMixin, Base):
    __tablename__ = "market_regimes"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    computed_at: Mapped[datetime] = mapped_column(TZDateTime, index=True, nullable=False)
    regime: Mapped[str] = mapped_column(String(32), nullable=False)
    total_market_cap_usd: Mapped[float | None] = mapped_column(Float)
    btc_dominance_pct: Mapped[float | None] = mapped_column(Float)
    fear_greed_value: Mapped[int | None] = mapped_column(Integer)
    fear_greed_source: Mapped[str | None] = mapped_column(String(32))
    breadth_pct: Mapped[float | None] = mapped_column(Float)
    volatility_pct: Mapped[float | None] = mapped_column(Float)
    engine_version: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class WatchlistItem(CreatedAtMixin, Base):
    """Coins the user added manually; analysed alongside the Top 20."""

    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))
