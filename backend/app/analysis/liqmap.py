"""Estimated liquidation map: where leveraged futures positions would be forced out.

Exchanges do not publish where traders' liquidation prices are, so this is an estimate, built
the way public "liquidation heatmaps" are:

1. Every hour in the window where open interest rose, new positions were opened near that
   hour's average price. They are split into longs and shorts by the long/short account ratio
   of that hour (50/50 when unknown) and spread over typical leverage (5x to 100x).
2. A long opened at P with leverage L is liquidated near P x (1 - 1/L + maintenance margin);
   a short near P x (1 + 1/L - maintenance margin).
3. When open interest fell, positions were closed: every open estimate shrinks in proportion.
4. When a later candle traded through a level, those positions are gone (liquidated).

What remains is grouped into price bands. Long liquidations sit below the price (forced
selling if the price falls there); short liquidations sit above it (forced buying if the price
rises there, which can fuel a move). Real leverage and entry prices are private, so treat the
levels as zones, not exact prices.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.data.derivatives import Point
from app.data.normalization.schemas import Candle

LEVERAGE: tuple[tuple[float, float], ...] = ((5, 0.10), (10, 0.30), (20, 0.25), (25, 0.15), (50, 0.12), (100, 0.08))
MAINTENANCE = 0.005
MIN_POINTS = 24


@dataclass
class LiqBand:
    low: float
    high: float
    long_usd: float  # long positions liquidated if the price falls into this band
    short_usd: float  # short positions liquidated if the price rises into this band

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


@dataclass
class LiqMap:
    price: float
    bands: list[LiqBand]
    hours: int
    band_pct: float
    range_pct: float
    long_total_usd: float
    short_total_usd: float
    method: str = "estimated from open-interest changes, the long/short ratio and typical leverage"
    notes: list[str] = field(default_factory=list)

    def clusters(self, side: str, limit: int = 3) -> list[LiqBand]:
        """Largest bands of one side: "long" (below the price) or "short" (above it)."""
        key = (lambda b: b.long_usd) if side == "long" else (lambda b: b.short_usd)
        return sorted((b for b in self.bands if key(b) > 0), key=key, reverse=True)[:limit]

    def usd_between(self, low: float, high: float, side: str) -> float:
        total = 0.0
        for b in self.bands:
            overlap = min(b.high, high) - max(b.low, low)
            if overlap <= 0:
                continue
            share = overlap / (b.high - b.low) if b.high > b.low else 1.0
            total += share * (b.long_usd if side == "long" else b.short_usd)
        return total

    def as_dict(self) -> dict[str, Any]:
        return {
            "price": self.price, "hours": self.hours, "band_pct": self.band_pct, "range_pct": self.range_pct,
            "long_total_usd": self.long_total_usd, "short_total_usd": self.short_total_usd, "method": self.method,
            "notes": self.notes,
            "bands": [{"low": b.low, "high": b.high, "long_usd": b.long_usd, "short_usd": b.short_usd} for b in self.bands],
        }


def _at(points: Sequence[Point], times: list[Any], t: Any, default: float) -> float:
    k = bisect.bisect_right(times, t) - 1
    return points[k].value if k >= 0 else default


def build(
    oi: Sequence[Point],
    candles: Sequence[Candle],
    price: float,
    *,
    unit: str = "coin",
    long_share: Sequence[Point] = (),
    band_pct: float = 0.5,
    range_pct: float = 12.0,
) -> LiqMap | None:
    """The map from hourly open interest (in coins, or USD with unit="usd") and hourly candles."""
    if price <= 0 or len(oi) < MIN_POINTS or not candles:
        return None
    candles = sorted(candles, key=lambda c: c.open_time)
    closes = [c.close_time for c in candles]
    ls_times = [p.time for p in long_share]
    # positions: [liquidation price, usd, side]
    longs: list[list[float]] = []
    shorts: list[list[float]] = []
    c_ptr = bisect.bisect_right(closes, oi[0].time)  # first candle after the first open-interest reading
    prev_coins: float | None = None
    last_price: float | None = None

    def apply_candle(c: Candle) -> None:
        for pos in longs:
            if pos[1] > 0 and c.low <= pos[0]:
                pos[1] = 0.0
        for pos in shorts:
            if pos[1] > 0 and c.high >= pos[0]:
                pos[1] = 0.0

    def typical_before(t: Any) -> float | None:
        k = bisect.bisect_right(closes, t) - 1
        if k < 0:
            return None
        c = candles[k]
        return (c.high + c.low + c.close) / 3.0

    for point in oi:
        while c_ptr < len(candles) and closes[c_ptr] <= point.time:
            apply_candle(candles[c_ptr])
            c_ptr += 1
        p = typical_before(point.time)
        if p is None or p <= 0:
            continue
        last_price = p
        coins = point.value / p if unit == "usd" else point.value
        if prev_coins is None:
            prev_coins = coins
            continue
        change = coins - prev_coins
        prev_coins = coins
        if change > 0:
            usd = change * p
            share = _at(long_share, ls_times, point.time, 0.5) if long_share else 0.5
            share = min(0.95, max(0.05, share))
            for lev, weight in LEVERAGE:
                longs.append([p * (1.0 - 1.0 / lev + MAINTENANCE), usd * share * weight])
                shorts.append([p * (1.0 + 1.0 / lev - MAINTENANCE), usd * (1.0 - share) * weight])
        elif change < 0:
            live = sum(pos[1] for pos in longs) + sum(pos[1] for pos in shorts)
            if live > 0:
                keep = max(0.0, 1.0 - (-change * p) / live)
                for pos in longs:
                    pos[1] *= keep
                for pos in shorts:
                    pos[1] *= keep
        # drop the dust so the lists stay small
        if len(longs) > 4000:
            longs[:] = [pos for pos in longs if pos[1] > 0]
            shorts[:] = [pos for pos in shorts if pos[1] > 0]
    while c_ptr < len(candles):
        apply_candle(candles[c_ptr])
        c_ptr += 1
    if last_price is None:
        return None

    step = price * band_pct / 100.0
    lo_edge = price * (1.0 - range_pct / 100.0)
    count = int(round(2 * range_pct / band_pct))
    bands = [LiqBand(lo_edge + k * step, lo_edge + (k + 1) * step, 0.0, 0.0) for k in range(count)]
    for liq, usd in longs:
        if usd > 0 and liq < price and lo_edge <= liq < lo_edge + count * step:
            bands[int((liq - lo_edge) // step)].long_usd += usd
    for liq, usd in shorts:
        if usd > 0 and liq > price and lo_edge <= liq < lo_edge + count * step:
            bands[int((liq - lo_edge) // step)].short_usd += usd
    notes = []
    if not long_share:
        notes.append("long/short split unknown: assumed 50/50")
    return LiqMap(
        price=price, bands=bands, hours=len(oi), band_pct=band_pct, range_pct=range_pct,
        long_total_usd=sum(b.long_usd for b in bands), short_total_usd=sum(b.short_usd for b in bands), notes=notes,
    )
