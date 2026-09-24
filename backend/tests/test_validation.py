from dataclasses import replace
from datetime import timedelta

import pytest

from app.core.enums import CrossCheckStatus, DataState, GateStage, ProviderStatus, Timeframe
from app.core.timeutil import utcnow
from app.data.normalization.schemas import OrderBook, Ticker
from app.data.normalization.stablecoins import classify_listing
from app.data.validation.candles import validate_candles
from app.data.validation.gate import IntegrityGate, IntegrityInputs
from app.data.validation.health_score import freshness_component, health_score
from app.data.validation.orderbook import summarize_order_book
from app.data.validation.prices import cross_validate_price, quote_to_usd_rate
from app.data.validation.volatility import check_volatility
from tests.conftest import make_candles, make_listing

# ------------------------------------------------------------------ stablecoins


@pytest.mark.parametrize(
    ("symbol", "name", "price", "tags", "stable", "wrapped"),
    [
        ("USDT", "Tether", 1.0, (), True, False),
        ("NEWUSD", "New Dollar", 0.999, (), True, False),
        ("ABC", "Abc", 1.0, ("stablecoin",), True, False),
        ("WBTC", "Wrapped Bitcoin", 60000, (), False, True),
        ("STETH", "Lido Staked ETH", 3000, (), False, True),
        ("XYZ", "Xyz", 5.0, ("liquid-staking-derivatives",), False, True),
        ("BTC", "Bitcoin", 60000, ("mineable",), False, False),
        ("SUI", "Sui", 1.02, (), False, False),
    ],
)
def test_classify_listing(symbol, name, price, tags, stable, wrapped):
    result = classify_listing(make_listing(symbol, price, 1e9, tags=tags, name=name))
    assert result.is_stablecoin is stable
    assert result.is_wrapped_or_derivative is wrapped


# ------------------------------------------------------------------ candles


def test_clean_series_passes():
    now = utcnow()
    closed, report = validate_candles(make_candles(Timeframe.H1, 300, now=now), Timeframe.H1, now, min_required=210)
    assert report.ok, report.critical_issues
    assert report.closed == 300 and len(closed) == 300
    assert report.forming_present is True
    assert report.completeness_pct == 100.0
    assert all(c.is_closed for c in closed)


def test_missing_recent_candles_are_critical():
    now = utcnow()
    candles = make_candles(Timeframe.M15, 300, now=now)
    del candles[-10:-5]
    _, report = validate_candles(candles, Timeframe.M15, now, min_required=210)
    assert report.missing_candles == 5 and report.missing_in_recent_window == 5
    assert any("missing" in issue for issue in report.critical_issues)
    assert report.gap_ranges


def test_old_gap_is_only_a_warning():
    now = utcnow()
    candles = make_candles(Timeframe.M15, 300, now=now)
    del candles[10]
    _, report = validate_candles(candles, Timeframe.M15, now, min_required=210)
    assert report.ok and report.missing_candles == 1
    assert any("older missing" in i for i in report.issues)


def test_duplicates_identical_vs_conflicting():
    now = utcnow()
    candles = make_candles(Timeframe.H1, 250, now=now)
    identical = candles + [candles[100]]
    _, report = validate_candles(identical, Timeframe.H1, now, min_required=210)
    assert report.duplicates == 1 and report.ok
    conflicting = candles + [replace(candles[100], close=candles[100].close * 1.5, high=candles[100].close * 1.6)]
    _, report = validate_candles(conflicting, Timeframe.H1, now, min_required=210)
    assert report.conflicting_duplicates == 1 and not report.ok


def test_invalid_ohlc_and_misaligned_in_recent_window_are_critical():
    now = utcnow()
    candles = make_candles(Timeframe.H1, 250, now=now)
    candles[-3] = replace(candles[-3], high=candles[-3].low * 0.5)
    _, report = validate_candles(candles, Timeframe.H1, now, min_required=210)
    assert report.invalid_ohlc == 1 and not report.ok
    candles = make_candles(Timeframe.H1, 250, now=now)
    candles[-4] = replace(candles[-4], open_time=candles[-4].open_time + timedelta(minutes=7))
    _, report = validate_candles(candles, Timeframe.H1, now, min_required=210)
    assert report.misaligned == 1 and not report.ok


def test_stale_series_detected():
    now = utcnow()
    candles = make_candles(Timeframe.M5, 300, now=now - timedelta(hours=2))
    _, report = validate_candles(candles, Timeframe.M5, now, min_required=210)
    assert report.is_stale
    assert any("stale" in i for i in report.critical_issues)


def test_insufficient_history():
    now = utcnow()
    _, report = validate_candles(make_candles(Timeframe.D1, 120, now=now), Timeframe.D1, now, min_required=210)
    assert report.insufficient_history and not report.ok


def test_forming_candle_never_returned_as_closed():
    now = utcnow()
    candles = make_candles(Timeframe.H1, 250, now=now)
    forged = replace(candles[-1], is_closed=True)  # adapter bug: forming candle flagged closed
    closed, report = validate_candles(candles[:-1] + [forged], Timeframe.H1, now, min_required=210)
    assert closed[-1].open_time < forged.open_time


def test_recent_price_spike_is_flagged_as_anomaly():
    now = utcnow()
    candles = make_candles(Timeframe.M5, 300, now=now)
    c = candles[-2]
    candles[-2] = replace(c, close=c.open * 1.25, high=c.open * 1.26)
    _, report = validate_candles(candles, Timeframe.M5, now, min_required=210)
    assert report.recent_anomaly
    assert any(a.kind == "return_outlier" for a in report.anomalies)


# ------------------------------------------------------------------ prices / order book / volatility


def _cross(primary, reference, age=30, now=None):
    now = now or utcnow()
    return cross_validate_price(
        primary_price_usd=primary, primary_source="binance", reference_price_usd=reference,
        reference_source="cmc", reference_updated_at=now - timedelta(seconds=age), now=now,
        warn_pct=0.5, max_pct=1.5, max_reference_age_seconds=300,
    )


def test_cross_check_statuses():
    assert _cross(100.0, 100.2).status == CrossCheckStatus.CONSISTENT
    assert _cross(100.0, 101.0).status == CrossCheckStatus.WARNING
    conflict = _cross(100.0, 103.0)
    assert conflict.status == CrossCheckStatus.CONFLICT and conflict.deviation_pct > 1.5
    assert _cross(100.0, None).status == CrossCheckStatus.UNVERIFIED
    stale = _cross(100.0, 100.0, age=1000)
    assert stale.status == CrossCheckStatus.UNVERIFIED and "old" in stale.reason


def test_quote_conversion():
    assert quote_to_usd_rate("USD", {}) == (1.0, "native USD quote")
    assert quote_to_usd_rate("USDT", {"USDT": 0.9995})[0] == 0.9995
    assert quote_to_usd_rate("USDT", {})[0] is None


def test_order_book_summary_and_crossed_book():
    now = utcnow()
    book = OrderBook("x", "BTCUSDT", ((99.9, 10.0), (99.0, 5.0)), ((100.1, 4.0), (101.0, 1.0)), now)
    s = summarize_order_book(book, band_pct=1.0)
    assert s.valid and s.spread_bps == pytest.approx(20.0, rel=1e-3)
    assert s.imbalance > 0  # more bid depth inside the band
    crossed = OrderBook("x", "BTCUSDT", ((101.0, 1.0),), ((100.0, 1.0),), now)
    assert summarize_order_book(crossed).valid is False
    assert summarize_order_book(OrderBook("x", "S", (), ((1.0, 1.0),), now)).valid is False


def _crash(candles, drops):
    out = list(candles)
    price = out[-len(drops) - 1].close
    for i, drop in enumerate(drops):
        c = out[-len(drops) + i]
        new_close = price * (1 - drop)
        out[-len(drops) + i] = replace(c, open=price, close=new_close, high=price * 1.0005, low=new_close * 0.9995)
        price = new_close
    return out


def test_volatility_calm_market_is_not_extreme():
    calm = make_candles(Timeframe.M5, 300, now=utcnow(), include_forming=False, vol=0.001)
    result = check_volatility(calm)
    assert result.available and not result.extreme
    assert check_volatility(calm[:20]).available is False


def test_volatility_relative_spike_is_extreme():
    calm = make_candles(Timeframe.M5, 300, now=utcnow(), include_forming=False, vol=0.001)
    result = check_volatility(_crash(calm, [0.004, 0.006] * 6))  # ~6% slide, far above normal
    assert result.extreme and result.ratio >= 4 and result.move_pct > 3


def test_volatility_absolute_move_catches_crash_in_volatile_asset():
    volatile = make_candles(Timeframe.M5, 300, now=utcnow(), include_forming=False, vol=0.003)
    result = check_volatility(_crash(volatile, [0.015, 0.005] * 6))  # ~11% hourly crash
    assert result.ratio < 4  # the relative rule alone would miss it
    assert result.extreme and "move" in result.reason


def test_health_score_bounds():
    assert health_score({"freshness": 1, "completeness": 1, "consistency": 1, "provider": 1}) == 100
    assert health_score({}) == 0
    assert freshness_component(10, 120) == 1.0 and freshness_component(200, 120) == 0.0


# ------------------------------------------------------------------ integrity gate


def _ticker(now, *, age=2.0, price=100.0):
    return Ticker("binance", "BTCUSDT", "BTC", "USDT", price, price * 0.9999, price * 1.0001, None, price * 1.02,
                  price * 0.98, 1.0, 10.0, 1000.0, now - timedelta(seconds=age), now)


def _inputs(**overrides):
    now = utcnow()
    reports = {}
    closed_5m = None
    for tf in (Timeframe.M5, Timeframe.H1):
        closed, report = validate_candles(make_candles(tf, 300, now=now, end_price=100.0), tf, now, min_required=210)
        reports[tf] = report
        if tf == Timeframe.M5:
            closed_5m = closed
    base = dict(
        symbol="BTC", now=now, market_source="binance", provider_status=ProviderStatus.UP,
        ticker=_ticker(now), ticker_max_age_seconds=120, candle_reports=reports,
        required_timeframes=(Timeframe.M5, Timeframe.H1), cross_check=_cross(100.0, 100.1, now=now),
        volatility=check_volatility(closed_5m),
    )
    base.update(overrides)
    return IntegrityInputs(**base)


def test_gate_passes_on_clean_data():
    result = IntegrityGate().evaluate(_inputs())
    assert result.passed and result.decision == "PASS"
    assert result.state == DataState.HEALTHY
    assert [s.stage for s in result.stages] == list(GateStage)
    assert result.data_health_score >= 90


def test_gate_stale_ticker_is_no_trade():
    now = utcnow()
    result = IntegrityGate().evaluate(_inputs(now=now, ticker=_ticker(now, age=600)))
    assert result.decision == "NO TRADE" and result.state == DataState.STALE_DATA
    assert any("old" in r for r in result.reasons)


def test_gate_price_conflict_is_no_trade():
    result = IntegrityGate().evaluate(_inputs(cross_check=_cross(100.0, 105.0)))
    assert not result.passed and result.state == DataState.DATA_CONFLICT


def test_gate_unverified_price_fails_closed_by_default():
    result = IntegrityGate().evaluate(_inputs(cross_check=_cross(100.0, None)))
    assert not result.passed and result.state == DataState.DEGRADED
    relaxed = IntegrityGate().evaluate(_inputs(cross_check=_cross(100.0, None), require_cross_validation=False))
    assert relaxed.passed and relaxed.state == DataState.DEGRADED


def test_gate_provider_down_and_missing_ticker():
    result = IntegrityGate().evaluate(_inputs(provider_status=ProviderStatus.DOWN, ticker=None))
    assert result.state == DataState.API_FAILURE and not result.passed


def test_gate_missing_timeframe_and_signal_pause():
    inputs = _inputs()
    inputs.candle_reports.pop(Timeframe.H1)
    result = IntegrityGate().evaluate(inputs)
    assert not result.passed and any("1H candles unavailable" in r for r in result.reasons)
    paused = IntegrityGate().evaluate(_inputs(signal_paused_reason="emergency stop"))
    assert paused.state == DataState.SIGNAL_PAUSED and not paused.passed


def test_gate_extreme_volatility_and_unassessable_volatility():
    from app.data.validation.volatility import VolatilityCheck

    extreme = VolatilityCheck(True, True, 6.0, 1.0, 0.1, 5.0, "volatility 6.0x normal with a 5.00% move")
    result = IntegrityGate().evaluate(_inputs(volatility=extreme))
    assert result.state == DataState.EXTREME_VOLATILITY and not result.passed
    result = IntegrityGate().evaluate(_inputs(volatility=None))
    assert not result.passed and any("volatility could not be assessed" in r for r in result.reasons)


def test_gate_crossed_quote_and_future_timestamp():
    now = utcnow()
    bad = Ticker("binance", "BTCUSDT", "BTC", "USDT", 100.0, 101.0, 100.0, None, None, None, None, None, None,
                 now + timedelta(minutes=5), now)
    result = IntegrityGate().evaluate(_inputs(now=now, ticker=bad))
    assert result.state == DataState.DATA_CONFLICT
    assert any("crossed" in r for r in result.reasons) and any("future" in r for r in result.reasons)
