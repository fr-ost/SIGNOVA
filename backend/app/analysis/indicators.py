"""Technical indicators over plain float sequences (oldest first).

Pure functions without third-party dependencies. Every function returns a list aligned
with its input; positions without enough history are None, never an estimated value.

Conventions follow Wilder and TA-Lib:
* EMA is seeded with the SMA of its first `period` values.
* RSI, ATR and ADX use Wilder smoothing; ATR and ADX start from the first true range
  that has a previous close (TA-Lib's lookback: ATR 14 -> index 14, ADX 14 -> index 27).
* Bollinger Bands use the population standard deviation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

Series = list[float | None]


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError("period must be >= 1")


def sma(values: Sequence[float], period: int) -> Series:
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    window = math.fsum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> Series:
    _check_period(period)
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    current = math.fsum(values[:period]) / period
    out[period - 1] = current
    for i in range(period, len(values)):
        current += alpha * (values[i] - current)
        out[i] = current
    return out


def ema_of_series(values: Series, period: int) -> Series:
    """EMA of a series that starts with None padding (e.g. the MACD line)."""
    out: Series = [None] * len(values)
    start = next((i for i, v in enumerate(values) if v is not None), None)
    if start is None:
        return out
    tail = values[start:]
    if any(v is None for v in tail):
        raise ValueError("series has gaps after its first value")
    for offset, value in enumerate(ema([float(v) for v in tail if v is not None], period)):
        out[start + offset] = value
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi(closes: Sequence[float], period: int = 14) -> Series:
    """Wilder's RSI. The first value is at index `period`."""
    _check_period(period)
    n = len(closes)
    out: Series = [None] * n
    if n <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


@dataclass(frozen=True, slots=True)
class Macd:
    line: Series
    signal: Series
    histogram: Series


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Macd:
    if fast >= slow:
        raise ValueError("fast period must be shorter than slow period")
    fast_ema, slow_ema = ema(closes, fast), ema(closes, slow)
    line: Series = [f - s if f is not None and s is not None else None for f, s in zip(fast_ema, slow_ema, strict=True)]
    signal_line = ema_of_series(line, signal)
    hist: Series = [m - s if m is not None and s is not None else None for m, s in zip(line, signal_line, strict=True)]
    return Macd(line, signal_line, hist)


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> Series:
    """True range from index 1 (index 0 has no previous close)."""
    n = len(closes)
    out: Series = [None] * n
    for i in range(1, n):
        prev = closes[i - 1]
        out[i] = max(highs[i] - lows[i], abs(highs[i] - prev), abs(lows[i] - prev))
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Series:
    """Wilder's Average True Range. The first value is at index `period`."""
    _check_period(period)
    n = len(closes)
    out: Series = [None] * n
    if n <= period:
        return out
    tr = true_range(highs, lows, closes)
    current = math.fsum(v for v in tr[1 : period + 1] if v is not None) / period
    out[period] = current
    for i in range(period + 1, n):
        current = (current * (period - 1) + tr[i]) / period  # type: ignore[operator]
        out[i] = current
    return out


@dataclass(frozen=True, slots=True)
class Dmi:
    plus_di: Series
    minus_di: Series
    adx: Series


def dmi(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> Dmi:
    """Wilder's Directional Movement Index: +DI, -DI (from index `period`) and ADX (from 2*period-1)."""
    _check_period(period)
    n = len(closes)
    plus_di: Series = [None] * n
    minus_di: Series = [None] * n
    adx_out: Series = [None] * n
    if n <= period:
        return Dmi(plus_di, minus_di, adx_out)
    tr = true_range(highs, lows, closes)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0

    s_tr = math.fsum(v for v in tr[1 : period + 1] if v is not None)
    s_plus = math.fsum(plus_dm[1 : period + 1])
    s_minus = math.fsum(minus_dm[1 : period + 1])
    dx: Series = [None] * n
    for i in range(period, n):
        if i > period:
            s_tr = s_tr - s_tr / period + tr[i]  # type: ignore[operator]
            s_plus = s_plus - s_plus / period + plus_dm[i]
            s_minus = s_minus - s_minus / period + minus_dm[i]
        p = 100.0 * s_plus / s_tr if s_tr > 0 else 0.0
        m = 100.0 * s_minus / s_tr if s_tr > 0 else 0.0
        plus_di[i], minus_di[i] = p, m
        dx[i] = 100.0 * abs(p - m) / (p + m) if p + m > 0 else 0.0

    first = 2 * period - 1
    if n > first:
        current = math.fsum(v for v in dx[period : first + 1] if v is not None) / period
        adx_out[first] = current
        for i in range(first + 1, n):
            current = (current * (period - 1) + dx[i]) / period  # type: ignore[operator]
            adx_out[i] = current
    return Dmi(plus_di, minus_di, adx_out)


@dataclass(frozen=True, slots=True)
class Bands:
    upper: Series
    middle: Series
    lower: Series


def bollinger(closes: Sequence[float], period: int = 20, width: float = 2.0) -> Bands:
    _check_period(period)
    n = len(closes)
    middle = sma(closes, period)
    upper: Series = [None] * n
    lower: Series = [None] * n
    for i in range(period - 1, n):
        mean = middle[i]
        assert mean is not None
        variance = math.fsum((x - mean) ** 2 for x in closes[i - period + 1 : i + 1]) / period
        sd = math.sqrt(variance)
        upper[i], lower[i] = mean + width * sd, mean - width * sd
    return Bands(upper, middle, lower)


def obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    """On-balance volume, cumulative from zero at the first candle."""
    out = [0.0] * len(closes)
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - volumes[i]
        else:
            out[i] = out[i - 1]
    return out


def roc(values: Sequence[float], period: int) -> Series:
    """Rate of change in percent over `period` steps."""
    _check_period(period)
    out: Series = [None] * len(values)
    for i in range(period, len(values)):
        base = values[i - period]
        if base != 0:
            out[i] = (values[i] / base - 1.0) * 100.0
    return out


def linear_slope(values: Sequence[float]) -> float:
    """Least-squares slope per step (0 for fewer than two points)."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = math.fsum(values) / n
    num = math.fsum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = math.fsum((i - mean_x) ** 2 for i in range(n))
    return num / den


def percentile_rank(values: Sequence[float], value: float) -> float:
    """Share of `values` at or below `value`, in percent (0-100)."""
    if not values:
        raise ValueError("values must not be empty")
    return 100.0 * sum(1 for v in values if v <= value) / len(values)


def last(series: Series, offset: int = 0) -> float | None:
    """Value `offset` steps before the end, or None."""
    index = len(series) - 1 - offset
    return series[index] if index >= 0 else None
