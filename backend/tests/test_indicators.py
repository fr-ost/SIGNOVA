"""Indicator math. Reference values come from published examples and from the `ta`
library (cross-checked during development; not a runtime dependency)."""

import math

import pytest

from app.analysis import indicators as ind

# StockCharts ChartSchool RSI example (Wilder, 14 periods).
RSI_CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826, 45.8931, 46.0328,
    45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439, 46.2122, 46.2521, 45.7137, 46.4515,
    45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672, 43.4205, 42.6628, 43.1314,
]
RSI_EXPECTED = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71, 50.42, 39.99, 41.46,
                41.87, 45.46, 37.30, 33.08, 37.77]


def wave(n: int = 120):
    closes = [100 + 10 * math.sin(i / 7) + 0.2 * i + 3 * math.cos(i / 3) for i in range(n)]
    highs = [c + 1 + 0.5 * math.sin(i) for i, c in enumerate(closes)]
    lows = [c - 1 - 0.5 * math.cos(i) for i, c in enumerate(closes)]
    return closes, highs, lows


def test_rsi_matches_published_wilder_example():
    values = ind.rsi(RSI_CLOSES, 14)
    assert values[:14] == [None] * 14
    assert [round(v, 2) for v in values[14:]] == RSI_EXPECTED


def test_rsi_edge_cases():
    assert ind.rsi(list(range(1, 40)), 14)[-1] == 100.0  # only gains
    assert ind.rsi([5.0] * 30, 14)[-1] == 50.0  # no movement
    assert ind.rsi([1.0] * 10, 14) == [None] * 10  # not enough history


def test_sma_and_ema_seeding():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert ind.sma(values, 3) == [None, None, 2.0, 3.0, 4.0, 5.0]
    ema = ind.ema(values, 3)
    assert ema[:2] == [None, None] and ema[2] == 2.0  # seeded with the SMA
    assert ema[3] == pytest.approx(3.0) and ema[5] == pytest.approx(5.0)
    assert ind.ema([7.0] * 50, 20)[-1] == pytest.approx(7.0)
    with pytest.raises(ValueError):
        ind.ema(values, 0)


def test_macd_alignment_and_constant_series():
    closes, _, _ = wave()
    m = ind.macd(closes)
    assert next(i for i, v in enumerate(m.line) if v is not None) == 25
    assert next(i for i, v in enumerate(m.signal) if v is not None) == 33
    assert m.histogram[-1] == pytest.approx(m.line[-1] - m.signal[-1])
    flat = ind.macd([10.0] * 60)
    assert flat.line[-1] == pytest.approx(0.0) and flat.histogram[-1] == pytest.approx(0.0)


def test_adx_and_directional_index_match_reference():
    closes, highs, lows = wave()
    d = ind.dmi(highs, lows, closes, 14)
    assert d.adx[26] is None and d.adx[27] == pytest.approx(54.481021, abs=1e-5)
    assert d.adx[60] == pytest.approx(58.505512, abs=1e-5)
    assert d.adx[119] == pytest.approx(33.864309, abs=1e-5)
    assert d.plus_di[60] == pytest.approx(34.682975, abs=1e-5)
    assert d.minus_di[119] == pytest.approx(37.112737, abs=1e-5)


def test_atr_uses_wilder_smoothing_from_first_true_range():
    closes, highs, lows = wave()
    a = ind.atr(highs, lows, closes, 14)
    assert a[13] is None
    tr = ind.true_range(highs, lows, closes)
    assert a[14] == pytest.approx(sum(tr[1:15]) / 14)
    assert a[15] == pytest.approx((a[14] * 13 + tr[15]) / 14)
    assert a[119] == pytest.approx(2.254218, abs=1e-5)


def test_bollinger_bands_match_reference():
    closes, _, _ = wave()
    b = ind.bollinger(closes, 20, 2.0)
    assert b.upper[119] == pytest.approx(130.956003, abs=1e-5)
    assert b.middle[119] == pytest.approx(122.288627, abs=1e-5)
    assert b.lower[119] == pytest.approx(113.62125, abs=1e-5)
    assert b.upper[18] is None


def test_obv_roc_slope_and_percentile():
    assert ind.obv([1, 2, 2, 1], [10, 20, 30, 40]) == [0.0, 20.0, 20.0, -20.0]
    assert ind.roc([100.0, 110.0, 121.0], 1)[1:] == [pytest.approx(10.0), pytest.approx(10.0)]
    assert ind.linear_slope([1.0, 3.0, 5.0, 7.0]) == pytest.approx(2.0)
    assert ind.linear_slope([4.0]) == 0.0
    assert ind.percentile_rank([1, 2, 3, 4], 3) == 75.0
    assert ind.last([1.0, 2.0, None], 0) is None and ind.last([1.0, 2.0], 1) == 1.0
