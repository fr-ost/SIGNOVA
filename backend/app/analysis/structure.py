"""Market structure from confirmed swing points.

A swing high is a candle whose high is above the `left` candles before it and not below
the `right` candles after it (lows mirror this). A pivot is only known once `right`
candles have closed after it, so the newest candles never form pivots; nothing is
repainted. From the pivots we derive:

* trend: higher highs and higher lows (BULLISH), lower highs and lower lows (BEARISH),
  mixed (RANGE), or too few pivots (UNCLEAR);
* the most recent break of structure: a close beyond the latest confirmed swing level;
* support and resistance: nearby pivots clustered within half an ATR.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import StructureTrend, Timeframe
from app.data.normalization.schemas import Candle

DEFAULT_LEFT = 3
DEFAULT_RIGHT = 3
DEFAULT_LOOKBACK = 300


@dataclass(frozen=True, slots=True)
class Pivot:
    index: int
    time: datetime
    price: float
    kind: str  # "high" | "low"


@dataclass(frozen=True, slots=True)
class StructureBreak:
    direction: str  # "UP" | "DOWN"
    level: float
    time: datetime  # open time of the candle that closed beyond the level
    candles_ago: int


@dataclass(frozen=True, slots=True)
class Level:
    price: float
    touches: int
    last_touch: datetime
    distance_pct: float
    distance_atr: float | None


@dataclass
class StructureAnalysis:
    timeframe: Timeframe
    trend: StructureTrend
    reason: str
    close: float
    highs: list[Pivot] = field(default_factory=list)  # confirmed swing highs, oldest first
    lows: list[Pivot] = field(default_factory=list)
    last_break: StructureBreak | None = None
    supports: list[Level] = field(default_factory=list)  # below the close, nearest first
    resistances: list[Level] = field(default_factory=list)  # above the close, nearest first

    def swing_low_below(self, price: float) -> Pivot | None:
        """Most recent confirmed swing low under `price`."""
        return next((p for p in reversed(self.lows) if p.price < price), None)


def find_pivots(candles: Sequence[Candle], left: int = DEFAULT_LEFT, right: int = DEFAULT_RIGHT) -> list[Pivot]:
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    pivots: list[Pivot] = []
    for i in range(left, len(candles) - right):
        h, lo = highs[i], lows[i]
        if all(h > highs[j] for j in range(i - left, i)) and all(h >= highs[j] for j in range(i + 1, i + right + 1)):
            pivots.append(Pivot(i, candles[i].open_time, h, "high"))
        if all(lo < lows[j] for j in range(i - left, i)) and all(lo <= lows[j] for j in range(i + 1, i + right + 1)):
            pivots.append(Pivot(i, candles[i].open_time, lo, "low"))
    return pivots


def last_break(candles: Sequence[Candle], pivots: Sequence[Pivot], right: int = DEFAULT_RIGHT) -> StructureBreak | None:
    """Most recent close beyond the latest confirmed swing high or low.

    A swing level becomes active once it is confirmed and is consumed by the first
    close beyond it, so one level produces at most one break.
    """
    ordered = sorted(pivots, key=lambda p: p.index + right)
    active_high: Pivot | None = None
    active_low: Pivot | None = None
    pointer = 0
    found: tuple[str, float, int] | None = None
    for j, candle in enumerate(candles):
        while pointer < len(ordered) and ordered[pointer].index + right < j:
            pivot = ordered[pointer]
            if pivot.kind == "high":
                active_high = pivot
            else:
                active_low = pivot
            pointer += 1
        if active_high is not None and candle.close > active_high.price:
            found = ("UP", active_high.price, j)
            active_high = None
        if active_low is not None and candle.close < active_low.price:
            found = ("DOWN", active_low.price, j)
            active_low = None
    if found is None:
        return None
    direction, level, index = found
    return StructureBreak(direction, level, candles[index].open_time, len(candles) - 1 - index)


def classify_trend(
    highs: Sequence[Pivot], lows: Sequence[Pivot], latest_break: StructureBreak | None
) -> tuple[StructureTrend, str]:
    if len(highs) < 2 or len(lows) < 2:
        return StructureTrend.UNCLEAR, "not enough confirmed swing points"
    higher_high = highs[-1].price > highs[-2].price
    higher_low = lows[-1].price > lows[-2].price
    if higher_high and higher_low:
        trend, reason = StructureTrend.BULLISH, "higher highs and higher lows"
    elif not higher_high and not higher_low:
        trend, reason = StructureTrend.BEARISH, "lower highs and lower lows"
    elif higher_high:
        return StructureTrend.RANGE, "higher high but lower low (expanding range)"
    else:
        return StructureTrend.RANGE, "lower high but higher low (contracting range)"
    # A close through the last swing point against the trend is a change of character.
    if latest_break is not None:
        if trend == StructureTrend.BULLISH and latest_break.direction == "DOWN" and latest_break.time > lows[-1].time:
            return StructureTrend.RANGE, "closed below the last higher low (possible change of character)"
        if trend == StructureTrend.BEARISH and latest_break.direction == "UP" and latest_break.time > highs[-1].time:
            return StructureTrend.RANGE, "closed above the last lower high (possible change of character)"
    return trend, reason


def cluster_levels(
    pivots: Sequence[Pivot], tolerance: float, close: float, atr: float | None
) -> list[Level]:
    """Group pivot prices within `tolerance` of a cluster's mean into levels."""
    clusters: list[list[Pivot]] = []
    for pivot in sorted(pivots, key=lambda p: p.price):
        if clusters and abs(pivot.price - statistics.fmean(p.price for p in clusters[-1])) <= tolerance:
            clusters[-1].append(pivot)
        else:
            clusters.append([pivot])
    levels = []
    for members in clusters:
        price = statistics.fmean(p.price for p in members)
        levels.append(
            Level(
                price=price,
                touches=len(members),
                last_touch=max(p.time for p in members),
                distance_pct=(price / close - 1.0) * 100.0,
                distance_atr=(price - close) / atr if atr else None,
            )
        )
    return levels


def analyze_structure(
    timeframe: Timeframe,
    candles: Sequence[Candle],
    atr: float | None,
    *,
    left: int = DEFAULT_LEFT,
    right: int = DEFAULT_RIGHT,
    lookback: int = DEFAULT_LOOKBACK,
    max_levels: int = 3,
) -> StructureAnalysis | None:
    window = list(candles[-lookback:])
    if len(window) < left + right + 1:
        return None
    close = window[-1].close
    pivots = find_pivots(window, left, right)
    highs = [p for p in pivots if p.kind == "high"]
    lows = [p for p in pivots if p.kind == "low"]
    brk = last_break(window, pivots, right)
    trend, reason = classify_trend(highs, lows, brk)
    tolerance = 0.5 * atr if atr else close * 0.005
    levels = cluster_levels(pivots, tolerance, close, atr)
    supports = sorted((lv for lv in levels if lv.price < close), key=lambda lv: close - lv.price)[:max_levels]
    resistances = sorted((lv for lv in levels if lv.price > close), key=lambda lv: lv.price - close)[:max_levels]
    return StructureAnalysis(
        timeframe=timeframe,
        trend=trend,
        reason=reason,
        close=close,
        highs=highs,
        lows=lows,
        last_break=brk,
        supports=supports,
        resistances=resistances,
    )
