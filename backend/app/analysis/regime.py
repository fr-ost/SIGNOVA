"""Regime classification.

Timeframe regime: trend direction from EMA alignment and slope, trend strength from ADX,
volatility from the current ATR% percentile within the recent history.

Market regime: Bitcoin's daily trend plus market breadth (share of the universe trading
above its daily EMA50), with Fear & Greed and global metrics attached as context. The
regime caps what the signal engine may emit: a bear market allows no spot buy signals,
a neutral market allows BUY but not STRONG BUY, and an unknown regime is treated as bear.
Bitcoin's 4H trend is reported separately: altcoins rarely rise while Bitcoin falls on the
4H chart, so the risk engine caps altcoin signals when it is down.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.analysis.features import IndicatorSnapshot
from app.core.enums import MarketRegimeLabel, RegimeLabel, SignalLabel, Timeframe, TrendDirection, VolatilityLevel
from app.data.normalization.schemas import FearGreed, GlobalMetrics

ADX_TRENDING = 20.0
ADX_STRONG = 25.0
HIGH_VOL_PERCENTILE = 80.0
LOW_VOL_PERCENTILE = 20.0


@dataclass(frozen=True)
class TimeframeRegime:
    timeframe: Timeframe
    trend: TrendDirection
    label: RegimeLabel
    adx: float | None
    strength: str  # STRONG | MODERATE | WEAK | UNKNOWN
    volatility: VolatilityLevel
    atr_pct_percentile: float | None
    reasons: list[str]


def _volatility(percentile: float | None) -> VolatilityLevel:
    if percentile is None:
        return VolatilityLevel.UNKNOWN
    if percentile >= HIGH_VOL_PERCENTILE:
        return VolatilityLevel.HIGH
    if percentile <= LOW_VOL_PERCENTILE:
        return VolatilityLevel.LOW
    return VolatilityLevel.NORMAL


def trend_direction(s: IndicatorSnapshot) -> tuple[TrendDirection, list[str]]:
    """UP or DOWN when at least three of four EMA conditions agree, else SIDEWAYS."""
    ema20, ema50, ema200, slope = s.ema20, s.ema50, s.ema200, s.ema50_slope_pct
    up = [
        ema50 is not None and s.close > ema50,
        ema20 is not None and ema50 is not None and ema20 > ema50,
        slope is not None and slope > 0,
        ema200 is not None and s.close > ema200,
    ]
    down = [
        ema50 is not None and s.close < ema50,
        ema20 is not None and ema50 is not None and ema20 < ema50,
        slope is not None and slope < 0,
        ema200 is not None and s.close < ema200,
    ]
    reasons = []
    if ema50 is not None:
        reasons.append(f"close {'above' if s.close > ema50 else 'below'} EMA50")
    if ema200 is not None:
        reasons.append(f"close {'above' if s.close > ema200 else 'below'} EMA200")
    if sum(up) >= 3 and sum(up) > sum(down):
        return TrendDirection.UP, reasons
    if sum(down) >= 3 and sum(down) > sum(up):
        return TrendDirection.DOWN, reasons
    return TrendDirection.SIDEWAYS, reasons


def classify_timeframe(s: IndicatorSnapshot) -> TimeframeRegime:
    trend, reasons = trend_direction(s)
    adx = s.adx14
    if adx is None:
        strength = "UNKNOWN"
    elif adx >= ADX_STRONG:
        strength = "STRONG"
    elif adx >= ADX_TRENDING:
        strength = "MODERATE"
    else:
        strength = "WEAK"
    if adx is not None and adx < ADX_TRENDING:
        label = RegimeLabel.RANGING
    elif trend == TrendDirection.UP:
        label = RegimeLabel.TRENDING_UP
    elif trend == TrendDirection.DOWN:
        label = RegimeLabel.TRENDING_DOWN
    else:
        label = RegimeLabel.TRANSITION
    if adx is not None:
        reasons.append(f"ADX {adx:.0f} ({strength.lower()})")
    return TimeframeRegime(
        timeframe=s.timeframe,
        trend=trend,
        label=label,
        adx=adx,
        strength=strength,
        volatility=_volatility(s.atr_pct_percentile),
        atr_pct_percentile=s.atr_pct_percentile,
        reasons=reasons,
    )


@dataclass
class MarketRegimeResult:
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
    fear_greed: FearGreed | None
    global_metrics: GlobalMetrics | None
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    btc_trend_4h: TrendDirection | None = None
    btc_rsi_4h: float | None = None


_MAX_SIGNAL = {
    MarketRegimeLabel.BULL: SignalLabel.STRONG_BUY,
    MarketRegimeLabel.NEUTRAL: SignalLabel.BUY,
    MarketRegimeLabel.BEAR: SignalLabel.WATCH,
    MarketRegimeLabel.UNKNOWN: SignalLabel.WATCH,
}


def classify_market_regime(
    *,
    now: datetime,
    btc_daily: IndicatorSnapshot | None,
    breadth: Sequence[bool],
    fear_greed: FearGreed | None,
    global_metrics: GlobalMetrics | None,
    min_breadth_sample: int = 8,
    bull_breadth_pct: float = 55.0,
    bear_breadth_pct: float = 45.0,
    errors: Sequence[str] = (),
    btc_4h: IndicatorSnapshot | None = None,
) -> MarketRegimeResult:
    reasons: list[str] = []
    flags: list[str] = []
    breadth_pct = 100.0 * sum(breadth) / len(breadth) if len(breadth) >= min_breadth_sample else None
    if breadth_pct is not None:
        reasons.append(f"{breadth_pct:.0f}% of {len(breadth)} assets trade above their daily EMA50")
    else:
        reasons.append(f"breadth unavailable ({len(breadth)} of {min_breadth_sample} required daily samples)")

    btc_trend: TrendDirection | None = None
    volatility = VolatilityLevel.UNKNOWN
    if btc_daily is None or btc_daily.ema200 is None:
        regime = MarketRegimeLabel.UNKNOWN
        reasons.insert(0, "Bitcoin daily trend unavailable; treated as a bear market (no buy signals)")
    else:
        daily = classify_timeframe(btc_daily)
        btc_trend, volatility = daily.trend, daily.volatility
        reasons.insert(0, f"Bitcoin daily trend {btc_trend.value.lower()}: {', '.join(daily.reasons)}")
        if btc_trend == TrendDirection.UP and (breadth_pct is None or breadth_pct >= bull_breadth_pct):
            regime = MarketRegimeLabel.BULL
        elif btc_trend == TrendDirection.DOWN and (breadth_pct is None or breadth_pct <= bear_breadth_pct):
            regime = MarketRegimeLabel.BEAR
        else:
            regime = MarketRegimeLabel.NEUTRAL
        if volatility == VolatilityLevel.HIGH:
            flags.append("HIGH_VOLATILITY")

    if fear_greed is not None:
        if fear_greed.value <= 20:
            flags.append("EXTREME_FEAR")
        elif fear_greed.value >= 80:
            flags.append("EXTREME_GREED")

    btc_trend_4h = trend_direction(btc_4h)[0] if btc_4h is not None and btc_4h.ema50 is not None else None
    if btc_trend_4h is not None:
        reasons.append(f"Bitcoin 4H trend {btc_trend_4h.value.lower()}")
        if btc_trend_4h == TrendDirection.DOWN:
            flags.append("BTC_4H_DOWN")

    max_signal = _MAX_SIGNAL[regime]
    if max_signal != SignalLabel.STRONG_BUY:
        reasons.append(f"{regime.value.lower()} market: signals capped at {max_signal.value}")

    ema200 = btc_daily.ema200 if btc_daily else None
    return MarketRegimeResult(
        computed_at=now,
        regime=regime,
        max_signal=max_signal,
        btc_trend=btc_trend,
        btc_close=btc_daily.close if btc_daily else None,
        btc_ema50=btc_daily.ema50 if btc_daily else None,
        btc_ema200=ema200,
        btc_vs_ema200_pct=(btc_daily.close / ema200 - 1.0) * 100.0 if btc_daily and ema200 else None,
        btc_roc20=btc_daily.roc20 if btc_daily else None,
        btc_atr_pct=btc_daily.atr_pct if btc_daily else None,
        volatility=volatility,
        breadth_pct=breadth_pct,
        breadth_sample=len(breadth),
        fear_greed=fear_greed,
        global_metrics=global_metrics,
        flags=flags,
        reasons=reasons,
        errors=list(errors),
        btc_trend_4h=btc_trend_4h,
        btc_rsi_4h=btc_4h.rsi14 if btc_4h is not None else None,
    )
