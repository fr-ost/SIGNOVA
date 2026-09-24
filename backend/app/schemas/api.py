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
    MarketRegimeLabel,
    ProcessingState,
    ProviderStatus,
    RegimeLabel,
    RiskSeverity,
    SignalLabel,
    StructureTrend,
    TrendDirection,
    VolatilityLevel,
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


# ---------------------------------------------------------------------- Phase 2: analysis and signals

DISCLAIMER = (
    "Deterministic quantitative analysis of public market data. Not financial advice and no promise "
    "of accuracy or profit. Spot only: you decide and place every trade yourself."
)


class PipelineStageOut(_Model):
    stage: str
    passed: bool
    outcome: str = Field(description="PASS, CAP (at most WATCH), DOWNGRADE (at most BUY) or BLOCK (NO TRADE)")
    reasons: list[str]


class FactorOut(_Model):
    key: str
    name: str
    score: float
    max_score: float
    positives: list[str]
    negatives: list[str]


class RiskCheckOut(_Model):
    key: str
    name: str
    passed: bool
    severity: RiskSeverity = Field(description="effect when failed: block, cap or downgrade")
    detail: str


class TargetOut(_Model):
    price: float
    r_multiple: float
    basis: str
    allocation_pct: float
    projected: bool


class TradePlanOut(_Model):
    quote_asset: str
    price: float
    entry_low: float
    entry_high: float
    entry_reference: float
    stop_loss: float
    stop_basis: str
    stop_distance_pct: float
    risk_per_unit: float
    targets: list[TargetOut]
    reward_risk: float = Field(description="net of fees and slippage, at TP2 (main target)")
    reward_risk_tp1: float
    gross_reward_risk: float
    cost_pct: float = Field(description="round-trip fees and slippage, percent of the position")
    nearest_resistance: float | None
    room_to_resistance_r: float | None
    suggested_allocation_pct: float = Field(description="portfolio share risking the per-signal limit at the stop")
    risk_at_allocation_pct: float
    better_entry_below: float | None
    invalidation: str
    actionable: bool = Field(description="true only for BUY and STRONG BUY; a WATCH plan is not a buy signal")
    notes: list[str]


class IndicatorSetOut(_Model):
    timeframe: str
    label: str
    candles: int
    open_time: datetime
    close: float
    ema20: float | None
    ema50: float | None
    ema200: float | None
    ema20_slope_pct: float | None
    ema50_slope_pct: float | None
    rsi14: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    atr14: float | None
    atr_pct: float | None
    atr_pct_percentile: float | None
    bb_upper: float | None
    bb_middle: float | None
    bb_lower: float | None
    bb_pct_b: float | None
    bb_width_pct: float | None
    adx14: float | None
    plus_di: float | None
    minus_di: float | None
    obv_slope: float | None
    volume_ratio: float | None
    up_down_volume_ratio: float | None
    roc10: float | None
    roc20: float | None
    realized_vol_pct: float | None


class PivotOut(_Model):
    time: datetime
    price: float


class StructureBreakOut(_Model):
    direction: str
    level: float
    time: datetime
    candles_ago: int


class LevelOut(_Model):
    price: float
    touches: int
    last_touch: datetime
    distance_pct: float
    distance_atr: float | None


class StructureOut(_Model):
    timeframe: str
    label: str
    trend: StructureTrend
    reason: str
    last_swing_high: PivotOut | None
    last_swing_low: PivotOut | None
    last_break: StructureBreakOut | None
    supports: list[LevelOut]
    resistances: list[LevelOut]


class TimeframeRegimeOut(_Model):
    timeframe: str
    label: str
    trend: TrendDirection
    regime: RegimeLabel
    adx: float | None
    strength: str
    volatility: VolatilityLevel
    atr_pct_percentile: float | None
    reasons: list[str]


class MarketRegimeOut(_Model):
    computed_at: datetime
    regime: MarketRegimeLabel
    max_signal: SignalLabel
    btc_trend: TrendDirection | None
    btc_close: float | None
    btc_ema50: float | None
    btc_ema200: float | None
    btc_vs_ema200_pct: float | None
    btc_roc20: float | None
    btc_atr_pct: float | None
    volatility: VolatilityLevel
    breadth_pct: float | None
    breadth_sample: int
    fear_greed: FearGreedOut | None
    global_metrics: GlobalMetricsOut | None
    flags: list[str]
    reasons: list[str]
    errors: list[str]
    btc_trend_4h: TrendDirection | None = None
    btc_rsi_4h: float | None = None


class AnalysisOut(_Model):
    generated_at: datetime
    symbol: str
    name: str
    universe_rank: int
    engine_version: str
    feature_version: str
    strategy: str
    setup_timeframe: str
    signal: SignalLabel
    score: int = Field(description="0-100 ranking of the setup; not a probability")
    score_label: SignalLabel = Field(description="label from the score alone, before risk and data checks")
    summary: str
    trend: str
    price: float | None
    quote_asset: str | None
    market_source: str | None
    data_state: DataState
    data_health_score: int
    integrity_passed: bool
    pipeline: list[PipelineStageOut]
    factors: list[FactorOut]
    plan: TradePlanOut | None
    risk_checks: list[RiskCheckOut]
    reasons: list[str]
    risks: list[str]
    indicators: list[IndicatorSetOut]
    structure: list[StructureOut]
    regimes: list[TimeframeRegimeOut]
    market_regime: MarketRegimeOut
    persistence: str
    sentiment: dict[str, Any] | None = Field(None, description="Phase 5 context: news tone, funding, exchange flows")
    disclaimer: str = DISCLAIMER


class SignalSummaryOut(_Model):
    symbol: str
    name: str
    universe_rank: int
    signal: SignalLabel
    score: int
    trend: str
    price: float | None
    quote_asset: str | None
    market_source: str | None
    data_state: DataState
    entry_low: float | None
    entry_high: float | None
    stop_loss: float | None
    take_profit_1: float | None
    take_profit_2: float | None
    reward_risk: float | None
    suggested_allocation_pct: float | None
    summary: str
    reasons: list[str]
    watchlist: bool = False


class SignalScanOut(_Model):
    generated_at: datetime
    engine_version: str
    strategy: str
    market_regime: MarketRegimeOut
    counts: dict[str, int]
    signals: list[SignalSummaryOut]
    errors: list[str]
    disclaimer: str = DISCLAIMER


class StoredTargetOut(_Model):
    kind: str
    level_index: int
    price: float
    allocation_pct: float | None


class StoredSignalOut(_Model):
    id: int
    created_at: datetime
    symbol: str
    timeframe: str
    strategy: str
    signal: str
    signal_score: int
    data_state: str
    data_health_score: int
    entry_low: float | None
    entry_high: float | None
    stop_loss: float | None
    risk_reward: float | None
    trend: str | None
    market_regime: str | None
    summary: str | None
    status: str
    engine_version: str | None
    targets: list[StoredTargetOut]


class SignalHistoryOut(_Model):
    generated_at: datetime
    symbol: str | None
    persistence: str
    signals: list[StoredSignalOut]
