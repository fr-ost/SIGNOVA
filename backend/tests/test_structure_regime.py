from dataclasses import replace
from datetime import timedelta

from app.analysis.features import compute_snapshot
from app.analysis.regime import classify_market_regime, classify_timeframe
from app.analysis.structure import analyze_structure, cluster_levels, find_pivots
from app.core.enums import (
    MarketRegimeLabel,
    RegimeLabel,
    SignalLabel,
    StructureTrend,
    Timeframe,
    TrendDirection,
    VolatilityLevel,
)
from app.core.timeutil import utcnow
from app.data.normalization.schemas import FearGreed
from tests.conftest import candles_from_closes, trend_series, zigzag


def test_pivots_need_confirmation_and_ignore_flat_duplicates():
    highs = [10, 11, 12, 13, 12, 11, 10, 11, 12, 12, 11, 10, 9]
    lows = [h - 1 for h in highs]
    candles = [
        replace(c, high=h, low=lo, open=lo, close=h)
        for c, h, lo in zip(candles_from_closes([1.0] * len(highs)), highs, lows, strict=True)
    ]
    pivots = find_pivots(candles, left=3, right=3)
    highs = [p for p in pivots if p.kind == "high"]
    lows = [p for p in pivots if p.kind == "low"]
    assert [p.index for p in highs] == [3, 8]  # the flat top at 8-9 counts once
    assert [p.index for p in lows] == [6]
    # The last three candles can never be pivots (not yet confirmed).
    assert all(p.index <= len(candles) - 4 for p in pivots)


def test_bullish_structure_with_levels_and_upside_break():
    candles = candles_from_closes(zigzag(100, [(8, 10), (4, -4)] * 6))
    s = analyze_structure(Timeframe.H4, candles, atr=None)
    assert s.trend == StructureTrend.BULLISH
    assert s.last_break is not None and s.last_break.direction == "UP"
    assert s.supports and all(level.price < s.close for level in s.supports)
    assert s.resistances and all(level.price > s.close for level in s.resistances)
    assert s.supports[0].price > s.supports[-1].price  # nearest first
    low = s.swing_low_below(s.close)
    assert low is not None and low.price < s.close


def test_bearish_structure_and_change_of_character():
    down = analyze_structure(Timeframe.H4, candles_from_closes(zigzag(100, [(8, -10), (4, 4)] * 6)), atr=None)
    assert down.trend == StructureTrend.BEARISH and down.last_break.direction == "DOWN"
    reversal = analyze_structure(
        Timeframe.H4, candles_from_closes(zigzag(100, [(8, 10), (4, -4)] * 5 + [(6, -12)])), atr=None
    )
    assert reversal.trend == StructureTrend.RANGE
    assert "change of character" in reversal.reason
    assert reversal.last_break.direction == "DOWN"


def test_level_clustering_merges_nearby_pivots():
    candles = candles_from_closes(zigzag(100, [(5, 10), (5, -8), (5, 8.5), (5, -8)]), wick=0.0)
    pivots = [p for p in find_pivots(candles) if p.kind == "high"]
    assert len(pivots) == 2
    merged = cluster_levels(pivots, tolerance=2.0, close=90.0, atr=1.0)
    assert len(merged) == 1 and merged[0].touches == 2
    separate = cluster_levels(pivots, tolerance=0.01, close=90.0, atr=1.0)
    assert len(separate) == 2


def test_snapshot_requires_history_and_reports_trend():
    series = candles_from_closes(trend_series(400))
    assert compute_snapshot(Timeframe.H4, series[:29]) is None
    assert compute_snapshot(Timeframe.H4, series[:150]).ema200 is None
    snap = compute_snapshot(Timeframe.H4, series)
    assert snap.ema20 > snap.ema50 > snap.ema200
    assert snap.open_time == series[-1].open_time
    regime = classify_timeframe(snap)
    assert regime.trend == TrendDirection.UP and regime.label == RegimeLabel.TRENDING_UP
    assert regime.volatility in (VolatilityLevel.LOW, VolatilityLevel.NORMAL, VolatilityLevel.HIGH)
    stored = snap.as_dict()
    assert stored["timeframe"] == "4h" and isinstance(stored["open_time"], str)

    falling = compute_snapshot(Timeframe.H4, candles_from_closes(trend_series(400, drift_pct=-0.3)))
    assert classify_timeframe(falling).trend == TrendDirection.DOWN


def _btc(drift: float):
    return compute_snapshot(Timeframe.D1, candles_from_closes(trend_series(400, drift_pct=drift), Timeframe.D1))


def _fng(value: int) -> FearGreed:
    now = utcnow()
    return FearGreed("fake", value, "x", now - timedelta(hours=1), now)


def test_market_regime_classification_and_caps():
    now = utcnow()
    bull = classify_market_regime(now=now, btc_daily=_btc(0.3), breadth=[True] * 14 + [False] * 5,
                                  fear_greed=_fng(85), global_metrics=None)
    assert bull.regime == MarketRegimeLabel.BULL and bull.max_signal == SignalLabel.STRONG_BUY
    assert "EXTREME_GREED" in bull.flags and bull.breadth_pct > 70

    bear = classify_market_regime(now=now, btc_daily=_btc(-0.3), breadth=[False] * 15 + [True] * 4,
                                  fear_greed=_fng(10), global_metrics=None)
    assert bear.regime == MarketRegimeLabel.BEAR and bear.max_signal == SignalLabel.WATCH
    assert "EXTREME_FEAR" in bear.flags

    weak_breadth = classify_market_regime(now=now, btc_daily=_btc(0.3), breadth=[False] * 12 + [True] * 7,
                                          fear_greed=None, global_metrics=None)
    assert weak_breadth.regime == MarketRegimeLabel.NEUTRAL and weak_breadth.max_signal == SignalLabel.BUY

    unknown = classify_market_regime(now=now, btc_daily=None, breadth=[], fear_greed=None, global_metrics=None)
    assert unknown.regime == MarketRegimeLabel.UNKNOWN and unknown.max_signal == SignalLabel.WATCH
    assert unknown.breadth_pct is None
