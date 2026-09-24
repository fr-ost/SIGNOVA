"""Per-timeframe indicator snapshot: the latest indicator values the engine reasons about.

Computed from validated closed candles only. A value that needs more history than is
available is None; the engine treats None as "not confirmed", never as neutral.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.analysis import indicators as ind
from app.core.enums import Timeframe
from app.data.normalization.schemas import Candle

FEATURE_VERSION = "f1"

MIN_CANDLES = 30
PERCENTILE_WINDOW = 200


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    timeframe: Timeframe
    candles: int
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    ema20: float | None
    ema50: float | None
    ema200: float | None
    ema20_slope_pct: float | None  # EMA20 change over the last 5 candles, percent
    ema50_slope_pct: float | None  # EMA50 change over the last 10 candles, percent
    rsi14: float | None
    rsi14_prev: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None
    macd_hist_prev: float | None
    atr14: float | None
    atr_pct: float | None
    atr_pct_percentile: float | None
    bb_upper: float | None
    bb_middle: float | None
    bb_lower: float | None
    bb_pct_b: float | None
    bb_width_pct: float | None
    bb_width_percentile: float | None
    adx14: float | None
    plus_di: float | None
    minus_di: float | None
    obv_slope: float | None  # OBV trend over 20 candles, in average candle volumes per candle
    volume_ratio: float | None  # last candle volume / average of the previous 20
    up_down_volume_ratio: float | None  # up-candle volume / down-candle volume, last 20 candles
    roc10: float | None
    roc20: float | None
    realized_vol_pct: float | None  # standard deviation of log returns, last 20 candles

    @property
    def label(self) -> str:
        return self.timeframe.label

    def distance_atr(self, price: float, reference: float | None) -> float | None:
        """(price - reference) in ATRs of this timeframe."""
        if reference is None or not self.atr14:
            return None
        return (price - reference) / self.atr14

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe values (10 significant digits) for storage and the API."""
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            if key == "timeframe":
                out[key] = self.timeframe.value
            elif isinstance(value, datetime):
                out[key] = value.isoformat()
            elif isinstance(value, float):
                out[key] = round_sig(value)
            else:
                out[key] = value
        return out


def round_sig(value: float, digits: int = 10) -> float:
    return float(f"{value:.{digits}g}") if math.isfinite(value) else value


def _pct_change(series: ind.Series, steps: int) -> float | None:
    current, before = ind.last(series), ind.last(series, steps)
    if current is None or before is None or before == 0:
        return None
    return (current / before - 1.0) * 100.0


def _percentile_of_last(series: Sequence[float | None], window: int = PERCENTILE_WINDOW) -> float | None:
    values = [v for v in series[-window:] if v is not None]
    if len(values) < 20:
        return None
    return ind.percentile_rank(values, values[-1])


def _up_down_volume(candles: Sequence[Candle], window: int = 20) -> float | None:
    recent = candles[-window:]
    up = math.fsum(c.volume for c in recent if c.close > c.open)
    down = math.fsum(c.volume for c in recent if c.close < c.open)
    if up == 0 and down == 0:
        return None
    if down == 0:
        return 10.0
    return min(up / down, 10.0)


def compute_snapshot(timeframe: Timeframe, candles: Sequence[Candle]) -> IndicatorSnapshot | None:
    """Indicator snapshot at the last closed candle, or None with fewer than MIN_CANDLES."""
    if len(candles) < MIN_CANDLES:
        return None
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    volumes = [c.volume for c in candles]
    lastc = candles[-1]
    close = lastc.close

    ema20_s = ind.ema(closes, 20)
    ema50_s = ind.ema(closes, 50)
    rsi_s = ind.rsi(closes, 14)
    macd_s = ind.macd(closes)
    atr_s = ind.atr(highs, lows, closes, 14)
    bands = ind.bollinger(closes, 20, 2.0)
    dmi_s = ind.dmi(highs, lows, closes, 14)

    atr14 = ind.last(atr_s)
    atr_pct_s = [a / c * 100.0 if a is not None and c > 0 else None for a, c in zip(atr_s, closes, strict=True)]
    upper, middle, lower = ind.last(bands.upper), ind.last(bands.middle), ind.last(bands.lower)
    pct_b = (close - lower) / (upper - lower) if upper is not None and lower is not None and upper > lower else None
    width_s = [
        (u - lo) / m * 100.0 if u is not None and lo is not None and m else None
        for u, lo, m in zip(bands.upper, bands.lower, bands.middle, strict=True)
    ]

    obv_slope = None
    if len(candles) >= 21:
        avg_volume = statistics.fmean(volumes[-20:])
        if avg_volume > 0:
            obv_slope = ind.linear_slope(ind.obv(closes, volumes)[-20:]) / avg_volume
    volume_ratio = None
    if len(candles) >= 21:
        previous = statistics.fmean(volumes[-21:-1])
        volume_ratio = volumes[-1] / previous if previous > 0 else None
    realized = None
    if len(candles) >= 21:
        returns = [math.log(b / a) for a, b in zip(closes[-21:-1], closes[-20:], strict=True) if a > 0 and b > 0]
        realized = statistics.pstdev(returns) * 100.0 if len(returns) >= 2 else None

    return IndicatorSnapshot(
        timeframe=timeframe,
        candles=len(candles),
        open_time=lastc.open_time,
        open=lastc.open,
        high=lastc.high,
        low=lastc.low,
        close=close,
        volume=lastc.volume,
        ema20=ind.last(ema20_s),
        ema50=ind.last(ema50_s),
        ema200=ind.last(ind.ema(closes, 200)),
        ema20_slope_pct=_pct_change(ema20_s, 5),
        ema50_slope_pct=_pct_change(ema50_s, 10),
        rsi14=ind.last(rsi_s),
        rsi14_prev=ind.last(rsi_s, 1),
        macd=ind.last(macd_s.line),
        macd_signal=ind.last(macd_s.signal),
        macd_hist=ind.last(macd_s.histogram),
        macd_hist_prev=ind.last(macd_s.histogram, 1),
        atr14=atr14,
        atr_pct=ind.last(atr_pct_s),
        atr_pct_percentile=_percentile_of_last(atr_pct_s),
        bb_upper=upper,
        bb_middle=middle,
        bb_lower=lower,
        bb_pct_b=pct_b,
        bb_width_pct=ind.last(width_s),
        bb_width_percentile=_percentile_of_last(width_s),
        adx14=ind.last(dmi_s.adx),
        plus_di=ind.last(dmi_s.plus_di),
        minus_di=ind.last(dmi_s.minus_di),
        obv_slope=obv_slope,
        volume_ratio=volume_ratio,
        up_down_volume_ratio=_up_down_volume(candles),
        roc10=ind.last(ind.roc(closes, 10)),
        roc20=ind.last(ind.roc(closes, 20)),
        realized_vol_pct=realized,
    )
