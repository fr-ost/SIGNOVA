"""API response models. Every number carries its source; unavailable data is explicit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import (
    Availability,
    CrossCheckStatus,
    DataState,
    GateStage,
    ProcessingState,
    ProviderStatus,
)


class _Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------- health


class ComponentHealth(_Model):
    ok: bool
    latency_ms: float | None = None
    error: str | None = None


class OpenAIConfigStatus(_Model):
    api_key_present: bool
    signal_model: str | None
    analysis_model: str | None
    fallback_model: str | None
    availability_check: str


class HealthOut(_Model):
    status: str
    version: str
    environment: str
    time: datetime
    database: ComponentHealth
    processing_state: ProcessingState
    data_state: DataState | None
    openai: OpenAIConfigStatus


class SystemStateOut(_Model):
    processing_state: ProcessingState
    data_state: DataState | None
    data_state_reasons: list[str]
    signals_paused: bool
    signal_paused_reason: str | None
    updated_at: datetime


# ---------------------------------------------------------------------- providers


class ProviderHealthOut(_Model):
    provider: str
    role: str
    status: ProviderStatus
    circuit_state: str
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None
    consecutive_failures: int
    total_requests: int
    total_failures: int
    rate_limit_hits: int
    last_latency_ms: float | None
    avg_latency_ms: float | None
    status_changed_at: datetime | None
    details: dict[str, Any]


class ProviderHealthListOut(_Model):
    generated_at: datetime
    providers: list[ProviderHealthOut]
    live_stream: dict[str, Any] | None = None


# ---------------------------------------------------------------------- market


class CrossCheckOut(_Model):
    status: CrossCheckStatus
    primary_price_usd: float | None
    reference_price_usd: float | None
    deviation_pct: float | None
    primary_source: str | None
    reference_source: str | None
    reference_age_seconds: float | None
    reason: str


class AssetMarketOut(_Model):
    universe_rank: int
    symbol: str
    name: str
    supported: bool
    unsupported_reason: str | None
    market_source: str | None
    market_symbol: str | None
    quote_asset: str | None
    price: float | None = Field(description="Live exchange price in the quote asset")
    price_usd: float | None
    reference_price_usd: float | None
    reference_source: str
    pct_change_24h: float | None
    pct_change_24h_source: str | None
    volume_24h_usd: float | None
    volume_24h_source: str | None
    market_cap_usd: float
    ticker_age_seconds: float | None
    cross_check: CrossCheckOut | None
    data_state: DataState
    data_health_score: int
    health_scope: str
    reasons: list[str]


class ExcludedAssetOut(_Model):
    symbol: str
    name: str
    market_cap_usd: float
    reason: str


class UniverseMetaOut(_Model):
    size: int
    listing_source: str
    listing_access: str | None = Field(None, description="pro or keyless when served by CoinMarketCap")
    fallback_used: bool
    built_at: datetime
    listing_newest_update_age_seconds: float | None
    stale: bool
    excluded: list[ExcludedAssetOut]
    errors: list[str]


class GlobalMetricsOut(_Model):
    source: str
    total_market_cap_usd: float
    total_volume_24h_usd: float | None
    btc_dominance_pct: float | None
    eth_dominance_pct: float | None
    market_cap_change_24h_pct: float | None
    updated_at: datetime | None


class FearGreedOut(_Model):
    source: str
    value: int
    classification: str
    updated_at: datetime | None


class AltcoinSeasonOut(_Model):
    source: str
    value: int
    snapshot_time: datetime | None
    yearly_high: int | None
    yearly_low: int | None


class MarketContextOut(_Model):
    fetched_at: datetime
    global_status: Availability
    global_metrics: GlobalMetricsOut | None
    fear_greed_status: Availability
    fear_greed: FearGreedOut | None
    altcoin_season_status: Availability
    altcoin_season: AltcoinSeasonOut | None
    errors: list[str]


class MarketSnapshotOut(_Model):
    generated_at: datetime
    data_state: DataState
    data_state_reasons: list[str]
    state_counts: dict[str, int]
    universe: UniverseMetaOut
    context: MarketContextOut
    assets: list[AssetMarketOut]
    persistence: str


class AssetListItemOut(_Model):
    universe_rank: int
    symbol: str
    name: str
    slug: str | None
    supported: bool
    unsupported_reason: str | None
    markets: list[dict[str, str]]
    market_cap_usd: float
    listing_source: str


class AssetListOut(_Model):
    generated_at: datetime
    listing_source: str
    stale: bool
    assets: list[AssetListItemOut]


# ---------------------------------------------------------------------- asset detail


class TickerOut(_Model):
    source: str
    symbol: str
    quote_asset: str
    last_price: float
    bid: float | None
    ask: float | None
    high_24h: float | None
    low_24h: float | None
    pct_change_24h: float | None
    volume_base_24h: float | None
    volume_quote_24h: float | None
    event_time: datetime | None
    received_at: datetime


class OrderBookSummaryOut(_Model):
    source: str
    symbol: str
    valid: bool
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    spread_bps: float | None
    band_pct: float
    bid_depth_quote: float | None
    ask_depth_quote: float | None
    imbalance: float | None
    bid_levels: int
    ask_levels: int
    issues: list[str]


class CandleAnomalyOut(_Model):
    open_time: datetime
    kind: str
    detail: str


class TimeframeValidationOut(_Model):
    timeframe: str
    label: str
    source: str | None
    market_symbol: str | None
    ok: bool
    received: int
    closed: int
    duplicates: int
    conflicting_duplicates: int
    invalid_ohlc: int
    misaligned: int
    missing_candles: int
    missing_in_recent_window: int
    completeness_pct: float
    last_closed_open_time: datetime | None
    last_closed_age_seconds: float | None
    last_close: float | None
    is_stale: bool
    insufficient_history: bool
    anomalies: list[CandleAnomalyOut]
    issues: list[str]
    critical_issues: list[str]
    error: str | None = None


class CandleCrossCheckOut(_Model):
    timeframe: str
    label: str
    status: CrossCheckStatus
    reference_source: str | None
    compared: int
    median_deviation_pct: float | None
    max_deviation_pct: float | None
    outliers: int
    reason: str


class VolatilityOut(_Model):
    available: bool
    extreme: bool
    ratio: float | None
    recent_vol_pct: float | None
    baseline_vol_pct: float | None
    move_pct: float | None
    reason: str


class StageOut(_Model):
    stage: GateStage
    passed: bool
    state: DataState
    reasons: list[str]


class IntegrityOut(_Model):
    decision: str
    passed: bool
    state: DataState
    data_health_score: int
    components: dict[str, float]
    stages: list[StageOut]
    reasons: list[str]
    checked_at: datetime


class ListingInfoOut(_Model):
    source: str
    price_usd: float
    market_cap_usd: float
    volume_24h_usd: float | None
    pct_change_1h: float | None
    pct_change_24h: float | None
    pct_change_7d: float | None
    last_updated: datetime | None


class AssetDetailOut(_Model):
    generated_at: datetime
    symbol: str
    name: str
    universe_rank: int
    supported: bool
    unsupported_reason: str | None
    markets: list[dict[str, str]]
    listing: ListingInfoOut
    ticker: TickerOut | None
    order_book: OrderBookSummaryOut | None
    timeframes: list[TimeframeValidationOut]
    cross_check: CrossCheckOut | None
    candle_cross_checks: list[CandleCrossCheckOut]
    volatility: VolatilityOut | None
    integrity: IntegrityOut
    errors: list[str]
    persistence: str


class CandleOut(_Model):
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float | None


class CandlesOut(_Model):
    generated_at: datetime
    symbol: str
    timeframe: str
    label: str
    source: str
    market_symbol: str
    quote_asset: str
    candles: list[CandleOut]
    forming: CandleOut | None
    validation: TimeframeValidationOut
