"""Quantitative signal score: six explainable factors, 100 points in total.

Strategy: multi-timeframe trend following with pullback entries, spot long only.
1D sets the primary trend, 4H is the setup timeframe, 1H confirms momentum and 15m
times the entry. Every point awarded or withheld is recorded as a reason.

| Factor      | Points | Looks at                                                   |
|-------------|--------|------------------------------------------------------------|
| trend       | 30     | EMA alignment and structure on 1D, 4H, 1H; 4H ADX/DMI       |
| momentum    | 20     | RSI and MACD histogram on 4H and 1H                         |
| structure   | 15     | 4H swing structure, latest break, support, room overhead    |
| location    | 15     | distance from 4H EMA20 in ATRs, Bollinger %B, 15m RSI       |
| volume      | 10     | 4H OBV trend, up versus down volume on 4H and 1H            |
| market      | 10     | market regime, 20-day relative strength versus Bitcoin      |

The score ranks setups; it is not a probability. The risk engine and the integrity
gate decide what may actually be shown as a buy signal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.analysis.features import IndicatorSnapshot
from app.analysis.regime import MarketRegimeResult
from app.analysis.structure import StructureAnalysis
from app.core.enums import MarketRegimeLabel, SignalLabel, StructureTrend, Timeframe
from app.core.formatting import fmt_price

D1, H4, H1, M15 = Timeframe.D1, Timeframe.H4, Timeframe.H1, Timeframe.M15


@dataclass(frozen=True)
class SignalParams:
    min_score_strong_buy: float = 80.0
    min_score_buy: float = 65.0
    min_score_watch: float = 50.0


@dataclass
class Factor:
    key: str
    name: str
    max_score: float
    score: float = 0.0
    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)

    def check(self, points: float, condition: bool, positive: str, negative: str) -> None:
        if condition:
            self.score += points
            self.positives.append(positive)
        else:
            self.negatives.append(negative)

    def award(self, points: float, text: str, *, partial_note: str | None = None) -> None:
        self.score += points
        self.positives.append(text)
        if partial_note:
            self.negatives.append(partial_note)


def _gt(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and a > b


@dataclass
class ScoreInputs:
    price: float
    snapshots: dict[Timeframe, IndicatorSnapshot]
    structures: dict[Timeframe, StructureAnalysis]
    market: MarketRegimeResult
    is_btc: bool
    resistances: Sequence[float]  # overhead resistance prices above `price`, ascending


def _structure_points(f: Factor, s: StructureAnalysis | None, label: str, bullish: float, ranged: float) -> None:
    if s is None or s.trend == StructureTrend.UNCLEAR:
        f.negatives.append(f"{label} structure unclear (too few swing points)")
    elif s.trend == StructureTrend.BULLISH:
        f.award(bullish, f"{label} structure: {s.reason}")
    elif s.trend == StructureTrend.RANGE:
        f.award(ranged, f"{label} structure: range", partial_note=f"{label} structure: {s.reason}")
    else:
        f.negatives.append(f"{label} structure bearish: {s.reason}")


def trend_factor(i: ScoreInputs) -> Factor:
    f = Factor("trend", "Trend alignment", 30)
    d, h4, h1 = i.snapshots.get(D1), i.snapshots.get(H4), i.snapshots.get(H1)
    if d:
        f.check(4, _gt(d.close, d.ema200), "1D close above EMA200", "1D close below EMA200")
        f.check(4, _gt(d.ema50, d.ema200), "1D EMA50 above EMA200", "1D EMA50 below EMA200")
        f.check(2, _gt(d.close, d.ema50), "1D close above EMA50", "1D close below EMA50")
    _structure_points(f, i.structures.get(D1), "1D", 2, 1)
    if h4:
        f.check(3, _gt(h4.close, h4.ema50), "4H close above EMA50", "4H close below EMA50")
        f.check(3, _gt(h4.ema20, h4.ema50), "4H EMA20 above EMA50", "4H EMA20 below EMA50")
        f.check(2, _gt(h4.ema50_slope_pct, 0.0), "4H EMA50 rising", "4H EMA50 flat or falling")
        adx_ok = h4.adx14 is not None and h4.adx14 >= 20 and _gt(h4.plus_di, h4.minus_di)
        f.check(
            2,
            adx_ok,
            f"4H ADX {h4.adx14 or 0:.0f} with +DI leading",
            f"4H directional trend weak or bearish (ADX {h4.adx14 or 0:.0f})",
        )
    _structure_points(f, i.structures.get(H4), "4H", 2, 1)
    if h1:
        f.check(2, _gt(h1.close, h1.ema50), "1H close above EMA50", "1H close below EMA50")
        f.check(2, _gt(h1.ema20, h1.ema50), "1H EMA20 above EMA50", "1H EMA20 below EMA50")
    s1 = i.structures.get(H1)
    f.check(2, s1 is not None and s1.trend != StructureTrend.BEARISH, "1H structure not bearish", "1H structure bearish")
    return f


def _rsi_points(f: Factor, rsi: float | None, label: str, full: float, band: tuple[float, float],
                half_band: tuple[float, float, float, float]) -> None:
    if rsi is None:
        f.negatives.append(f"{label} RSI unavailable")
        return
    low, high = band
    lo2, lo1, hi1, hi2 = half_band
    if low <= rsi <= high:
        f.award(full, f"{label} RSI {rsi:.0f} in the bullish zone")
    elif lo2 <= rsi < lo1 or hi1 < rsi <= hi2:
        f.award(full / 2, f"{label} RSI {rsi:.0f} acceptable", partial_note=f"{label} RSI {rsi:.0f} outside the ideal zone")
    elif rsi > hi2:
        f.negatives.append(f"{label} RSI {rsi:.0f} overbought")
    else:
        f.negatives.append(f"{label} RSI {rsi:.0f} weak")


def momentum_factor(i: ScoreInputs) -> Factor:
    f = Factor("momentum", "Momentum", 20)
    h4, h1 = i.snapshots.get(H4), i.snapshots.get(H1)
    _rsi_points(f, h4.rsi14 if h4 else None, "4H", 6, (50, 68), (45, 50, 68, 74))
    if h4:
        f.check(3, _gt(h4.macd_hist, 0.0), "4H MACD histogram positive", "4H MACD histogram negative")
        f.check(3, _gt(h4.macd_hist, h4.macd_hist_prev), "4H MACD momentum rising", "4H MACD momentum falling")
    _rsi_points(f, h1.rsi14 if h1 else None, "1H", 4, (45, 70), (40, 45, 70, 78))
    if h1:
        f.check(2, _gt(h1.macd_hist, 0.0), "1H MACD histogram positive", "1H MACD histogram negative")
        f.check(2, _gt(h1.macd_hist, h1.macd_hist_prev), "1H MACD momentum rising", "1H MACD momentum falling")
    return f


def structure_factor(i: ScoreInputs) -> Factor:
    f = Factor("structure", "Market structure", 15)
    s4, h4 = i.structures.get(H4), i.snapshots.get(H4)
    _structure_points(f, s4, "4H", 5, 2)
    brk = s4.last_break if s4 else None
    if brk is None:
        f.award(1, "no recent 4H break of structure", partial_note="no confirmed 4H breakout")
    elif brk.direction == "UP":
        f.award(4, f"4H closed above swing high {fmt_price(brk.level)} {brk.candles_ago} candles ago")
    else:
        f.negatives.append(f"latest 4H break was down, below {fmt_price(brk.level)}")
    atr = h4.atr14 if h4 else None
    support = next((lv for lv in (s4.supports if s4 else []) if lv.price < i.price), None)
    if support is not None and atr and (i.price - support.price) / atr <= 3.0:
        f.award(3, f"4H support at {fmt_price(support.price)} ({(i.price - support.price) / atr:.1f} ATR below)")
    else:
        f.negatives.append("no 4H support within 3 ATR below the price")
    if not i.resistances:
        f.award(3, "no resistance overhead in the analysed history")
    elif atr:
        room = (i.resistances[0] - i.price) / atr
        text = f"nearest resistance {fmt_price(i.resistances[0])} is {room:.1f} ATR above"
        if room >= 3.0:
            f.award(3, text)
        elif room >= 1.5:
            f.award(2, text)
        elif room >= 0.75:
            f.award(1, text, partial_note=f"limited room: {text}")
        else:
            f.negatives.append(f"resistance close overhead: {text}")
    return f


def location_factor(i: ScoreInputs) -> Factor:
    f = Factor("location", "Entry location", 15)
    h4, m15 = i.snapshots.get(H4), i.snapshots.get(M15)
    distance = h4.distance_atr(i.price, h4.ema20) if h4 else None
    if distance is None:
        f.negatives.append("distance from 4H EMA20 unavailable")
    elif -0.5 <= distance <= 1.0:
        f.award(8, f"price near the 4H EMA20 value zone ({distance:+.1f} ATR)")
    elif 1.0 < distance <= 2.0:
        f.award(4, f"price {distance:.1f} ATR above the 4H EMA20", partial_note=f"moderately extended ({distance:.1f} ATR above 4H EMA20)")
    elif distance > 2.0:
        f.negatives.append(f"extended: {distance:.1f} ATR above the 4H EMA20")
    elif h4 and _gt(i.price, h4.ema50):
        f.award(6, "pullback between the 4H EMA20 and EMA50")
    else:
        f.negatives.append("price below the 4H EMA50")
    pct_b = h4.bb_pct_b if h4 else None
    if pct_b is None:
        f.negatives.append("4H Bollinger position unavailable")
    elif 0.2 <= pct_b <= 0.85:
        f.award(3, f"inside the 4H Bollinger bands (%B {pct_b:.2f})")
    elif pct_b > 1.0:
        f.negatives.append(f"above the upper 4H Bollinger band (%B {pct_b:.2f})")
    else:
        f.award(1, f"4H Bollinger %B {pct_b:.2f}", partial_note=f"4H Bollinger %B {pct_b:.2f} near a band edge")
    rsi15 = m15.rsi14 if m15 else None
    if rsi15 is None:
        f.negatives.append("15m RSI unavailable")
    elif 35 <= rsi15 <= 70:
        f.award(4, f"15m RSI {rsi15:.0f} balanced")
    elif rsi15 > 70:
        f.negatives.append(f"15m RSI {rsi15:.0f} overbought: wait for a cool-off")
    else:
        f.award(1, f"15m RSI {rsi15:.0f}", partial_note=f"15m momentum weak (RSI {rsi15:.0f})")
    return f


def volume_factor(i: ScoreInputs) -> Factor:
    f = Factor("volume", "Volume", 10)
    h4, h1 = i.snapshots.get(H4), i.snapshots.get(H1)
    f.check(4, h4 is not None and _gt(h4.obv_slope, 0.0), "4H on-balance volume rising", "4H on-balance volume falling")
    for snap, label in ((h4, "4H"), (h1, "1H")):
        ratio = snap.up_down_volume_ratio if snap else None
        f.check(
            3,
            ratio is not None and ratio >= 1.1,
            f"{label} buying volume {ratio or 0:.2f}x selling volume (20 candles)",
            f"{label} buying volume not dominant ({ratio or 0:.2f}x selling volume)",
        )
    return f


def market_factor(i: ScoreInputs) -> Factor:
    f = Factor("market", "Market context", 10)
    regime = i.market.regime
    if regime == MarketRegimeLabel.BULL:
        f.award(6, "bull market regime")
    elif regime == MarketRegimeLabel.NEUTRAL:
        f.award(3, "neutral market regime", partial_note="market regime only neutral")
    else:
        f.negatives.append(f"{regime.value.lower()} market regime")
    d = i.snapshots.get(D1)
    own = d.roc20 if d else None
    if i.is_btc:
        f.check(4, own is not None and own > 0, f"Bitcoin up {own or 0:.1f}% over 20 days",
                f"Bitcoin down {abs(own or 0):.1f}% over 20 days")
    elif own is None or i.market.btc_roc20 is None:
        f.negatives.append("relative strength versus Bitcoin unavailable")
    else:
        rs = own - i.market.btc_roc20
        if rs > 0:
            f.award(4, f"outperforming Bitcoin by {rs:.1f} points over 20 days")
        elif rs >= -5:
            f.award(2, f"in line with Bitcoin over 20 days ({rs:+.1f} points)",
                    partial_note=f"not outperforming Bitcoin ({rs:+.1f} points over 20 days)")
        else:
            f.negatives.append(f"underperforming Bitcoin by {abs(rs):.1f} points over 20 days")
    return f


def score_factors(inputs: ScoreInputs) -> list[Factor]:
    return [
        trend_factor(inputs),
        momentum_factor(inputs),
        structure_factor(inputs),
        location_factor(inputs),
        volume_factor(inputs),
        market_factor(inputs),
    ]


def total_score(factors: Sequence[Factor]) -> int:
    return int(round(sum(f.score for f in factors)))


def label_for_score(score: float, params: SignalParams) -> SignalLabel:
    if score >= params.min_score_strong_buy:
        return SignalLabel.STRONG_BUY
    if score >= params.min_score_buy:
        return SignalLabel.BUY
    if score >= params.min_score_watch:
        return SignalLabel.WATCH
    return SignalLabel.NO_TRADE
