"""Risk limits. Defaults match the risk_settings table defaults; Phase 4 makes them editable."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskParams:
    max_risk_per_signal_pct: float = 1.0  # portfolio % lost if the stop is hit
    max_allocation_pct: float = 10.0  # largest position, % of portfolio
    fee_pct: float = 0.1  # per side
    slippage_pct: float = 0.05  # per side
    min_reward_risk: float = 1.5  # net, at TP2 (main target)
    strong_min_reward_risk: float = 2.0
    min_room_r: float = 0.75  # nearest resistance at least this far above the entry, in R
    strong_min_room_r: float = 1.0  # STRONG BUY: the first target must at least repay the risk
    max_stop_pct: float = 15.0
    min_stop_atr: float = 1.0
    max_stop_atr: float = 3.0
    stop_buffer_atr: float = 0.25
    entry_zone_atr: float = 0.5
    target_buffer_atr: float = 0.1
    max_spread_bps: float = 30.0
    min_depth_usd: float = 25_000.0  # each side, within the order book band
    min_volume_24h_usd: float = 5_000_000.0
    max_extension_atr: float = 2.5
    max_rsi_4h: float = 78.0
    max_rsi_1d: float = 80.0
    max_rsi_15m_strong: float = 85.0  # STRONG BUY: no micro-spike at the entry
    max_change_24h_pct: float = 25.0
    max_atr_percentile_strong: float = 95.0
    min_trigger_confirmations: int = 2  # of 3 on 1H: close above EMA20, MACD rising, RSI rising
    min_book_imbalance_strong: float = -0.25  # (bids - asks) / total within the band; -0.25 = bids 60% of asks
    target_allocations: tuple[float, float, float] = (40.0, 35.0, 25.0)

    @property
    def round_trip_cost_pct(self) -> float:
        return 2.0 * (self.fee_pct + self.slippage_pct)
