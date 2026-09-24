"""Signal engine, trade plan, risk checks and final validation on synthetic markets.

The markets are deterministic price functions sampled into consistent 5m-1D candles at a
fixed clock, so every label below is reproducible.
"""

import math
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.analysis.engine import AnalysisInputs, EngineParams, SignalEngine
from app.analysis.features import compute_snapshot
from app.analysis.finalize import final_validation
from app.analysis.regime import classify_market_regime
from app.analysis.structure import analyze_structure
from app.core.enums import DATA_GATE_STAGES, DataState, GateStage, MarketRegimeLabel, RiskSeverity, SignalLabel, Timeframe
from app.data.validation.gate import IntegrityResult, StageResult
from app.data.validation.orderbook import OrderBookSummary
from app.risk.params import RiskParams
from app.risk.plan import build_plan
from tests.conftest import market_candles

NOW = datetime(2026, 3, 2, 9, 41, tzinfo=UTC)


def waves(drift: float, amplitude: float, period_hours: float, phase_twelfths: int):
    """Exponential drift per day with a sine swing; argument is hours before NOW."""
    phase = 2 * math.pi * phase_twelfths / 12
    return lambda h: 100 * math.exp(-drift * h / 24) * (1 + amplitude * math.sin(2 * math.pi * (-h / period_hours) + phase))


def noisy(drift: float, amplitude: float, period_hours: float, phase_twelfths: int, noise: float):
    """`waves` plus small intraday noise, so short timeframes are not one-directional."""
    wave = waves(drift, amplitude, period_hours, phase_twelfths)
    return lambda h: wave(h) * (1 + noise * math.sin(2 * math.pi * h / 1.5) + 0.6 * noise * math.sin(2 * math.pi * h / 0.7 + 1.0))


BULL = noisy(0.01, 0.02, 96, 0, 0.004)  # uptrend bouncing off a higher low
SMOOTH_BULL = waves(0.01, 0.025, 96, 10)  # every 15m candle up (RSI 99) under a nearby resistance
BEAR = waves(-0.01, 0.025, 96, 10)


def PUMP(h: float) -> float:  # uptrend plus a 20% spike over the last 30 hours
    return BULL(h) * (1 + 0.2 * max(0.0, 1 - h / 30))


def integrity(passed: bool = True) -> IntegrityResult:
    stages = [StageResult(s, True, DataState.HEALTHY, []) for s in DATA_GATE_STAGES]
    reasons = []
    if not passed:
        stages[4] = StageResult(GateStage.SOURCE_CONSISTENCY_CHECK, False, DataState.DATA_CONFLICT, ["prices differ by 3%"])
        reasons = ["prices differ by 3%"]
    return IntegrityResult("X", NOW, passed, DataState.HEALTHY if passed else DataState.DATA_CONFLICT, 90, {}, stages, reasons)


def book(price: float, spread_bps: float = 2.0) -> OrderBookSummary:
    half = spread_bps / 20_000
    return OrderBookSummary("fake", "XUSDT", True, price * (1 - half), price * (1 + half), price, spread_bps, 1.0,
                            2e6, 2e6, 0.0, 100, 100, [])


def regime(btc_fn=BULL, breadth=(14, 5)):
    btc = compute_snapshot(Timeframe.D1, market_candles(btc_fn, now=NOW, counts={Timeframe.D1: 300})[Timeframe.D1])
    return classify_market_regime(now=NOW, btc_daily=btc, breadth=[True] * breadth[0] + [False] * breadth[1],
                                  fear_greed=None, global_metrics=None)


def run(fn=BULL, *, symbol="ETH", integrity_ok=True, spread=2.0, market=None, candles=None, pct24=2.0,
        supported=True, params=None):
    candles = candles if candles is not None else market_candles(fn, now=NOW, wick=0.004)
    price = fn(0.0)
    return SignalEngine(params).evaluate(
        AnalysisInputs(
            symbol=symbol, name=symbol.title(), universe_rank=2, now=NOW, supported=supported,
            unsupported_reason=None if supported else "no USDT/USD spot pair on supported exchanges",
            candles=candles if supported else {}, price=price if supported else None, quote_asset="USDT",
            market_source="fake", quote_usd_rate=1.0, pct_change_24h=pct24, volume_24h_quote=5e8,
            order_book=book(price, spread), integrity=integrity(integrity_ok), market=market or regime(),
        )
    )


def failed(result, severity=None):
    return {c.key for c in result.risk_checks if not c.passed and (severity is None or c.severity == severity)}


def test_uptrend_bounce_is_an_actionable_strong_buy():
    r = run()
    assert r.signal == SignalLabel.STRONG_BUY and r.score >= 80
    assert all(stage.passed for stage in r.pipeline)
    assert [s.stage for s in r.pipeline][-3:] == ["ANALYSIS_CHECK", "RISK_CHECK", "FINAL_VALIDATION"]
    assert not failed(r)
    p = r.plan
    assert p is not None and p.actionable
    assert p.stop_loss < p.entry_low < p.entry_high == pytest.approx(r.price)
    prices = [t.price for t in p.targets]
    assert p.entry_high < prices[0] < prices[1] < prices[2]
    assert p.reward_risk >= 2.0 and p.suggested_allocation_pct <= 10.0 and p.risk_at_allocation_pct <= 1.0 + 1e-9
    assert sum(t.allocation_pct for t in p.targets) == pytest.approx(100.0)
    assert r.summary.startswith("STRONG BUY (score") and "TP2" in r.summary
    assert r.trend == "UP" and r.setup_candle_open_time == r.snapshots[Timeframe.H4].open_time
    assert len(r.factors) == 6 and sum(f.max_score for f in r.factors) == 100


def test_strong_buy_needs_room_and_no_short_term_spike():
    r = run(SMOOTH_BULL)
    assert r.score_label == SignalLabel.STRONG_BUY and r.signal == SignalLabel.BUY
    assert {"short_term_spike", "resistance_room_strong"} <= failed(r, RiskSeverity.DOWNGRADE)
    assert r.reasons[0].startswith("Uptrend on 1D and 4H") and "net to TP2" in r.reasons[0]
    assert any(reason.startswith("not STRONG BUY: 15m RSI") for reason in r.reasons)
    assert any("short-term spike" in risk for risk in r.risks)
    assert r.plan.actionable


def test_downtrend_is_no_trade_without_plan():
    r = run(BEAR, market=regime(BULL))
    assert r.signal == SignalLabel.NO_TRADE and r.plan is None
    assert "trend_filter" in failed(r, RiskSeverity.CAP)
    assert any(reason.startswith("score") and "below the BUY threshold" in reason for reason in r.reasons)
    assert not r.reasons[0].startswith("score")
    assert r.summary.startswith("NO TRADE: ") and "threshold" not in r.summary  # leads with the cause
    assert r.trend == "DOWN"


def test_parabolic_move_is_capped_at_watch_with_a_non_actionable_plan():
    r = run(PUMP, pct24=19.0)
    assert r.signal == SignalLabel.WATCH
    assert {"extension", "overbought"} & failed(r, RiskSeverity.CAP)
    assert r.plan is not None and not r.plan.actionable
    assert r.summary.startswith("WATCH (score")


def test_failed_integrity_gate_always_blocks():
    r = run(integrity_ok=False)
    assert r.signal == SignalLabel.NO_TRADE and r.plan is None
    assert r.reasons[0].startswith("data integrity:")
    assert not r.integrity_passed and r.data_state == DataState.DATA_CONFLICT
    consistency = next(s for s in r.pipeline if s.stage == "SOURCE_CONSISTENCY_CHECK")
    assert consistency.outcome == "BLOCK"


def test_wide_spread_blocks_even_a_strong_setup():
    r = run(spread=80.0)
    assert r.signal == SignalLabel.NO_TRADE
    assert "spread" in failed(r, RiskSeverity.BLOCK)
    risk = next(s for s in r.pipeline if s.stage == "RISK_CHECK")
    assert risk.outcome == "BLOCK" and not risk.passed


def test_market_regime_caps_signals():
    bear_market = regime(BEAR, breadth=(3, 16))
    assert bear_market.regime == MarketRegimeLabel.BEAR
    capped = run(market=bear_market)
    assert capped.signal == SignalLabel.WATCH and "market_regime" in failed(capped, RiskSeverity.CAP)

    neutral = regime(BULL, breadth=(5, 14))
    assert neutral.regime == MarketRegimeLabel.NEUTRAL
    downgraded = run(market=neutral)
    assert downgraded.signal == SignalLabel.BUY and "market_regime" in failed(downgraded, RiskSeverity.DOWNGRADE)


def test_insufficient_history_fails_the_analysis_check():
    counts = {Timeframe.D1: 300, Timeframe.H4: 120, Timeframe.H1: 400, Timeframe.M15: 300, Timeframe.M5: 300}
    r = run(candles=market_candles(BULL, now=NOW, counts=counts, wick=0.004))
    assert r.signal == SignalLabel.NO_TRADE
    analysis = next(s for s in r.pipeline if s.stage == "ANALYSIS_CHECK")
    assert not analysis.passed and any("4H: ema200" in reason for reason in analysis.reasons)
    assert r.reasons[0].startswith("analysis:")


def test_unsupported_asset_is_no_trade():
    r = run(supported=False)
    assert r.signal == SignalLabel.NO_TRADE and r.plan is None and r.factors == []
    assert "no USDT/USD spot pair" in r.reasons[0]


def test_stricter_score_threshold_changes_the_label():
    params = EngineParams(signal=replace(EngineParams().signal, min_score_strong_buy=101, min_score_buy=101))
    r = run(params=params)
    assert r.signal == SignalLabel.WATCH and r.score_label == SignalLabel.WATCH


def _plan(resistances, params=RiskParams()):
    candles = market_candles(BULL, now=NOW, wick=0.004)
    h4 = compute_snapshot(Timeframe.H4, candles[Timeframe.H4])
    structure = analyze_structure(Timeframe.H4, candles[Timeframe.H4], h4.atr14)
    return build_plan(price=BULL(0.0), quote_asset="USDT", h4=h4, structure_4h=structure,
                      resistances=resistances, params=params), h4


def test_plan_math_net_of_costs_and_sizing():
    params = RiskParams()
    price = BULL(0.0)
    plan, h4 = _plan([price * 1.05, price * 1.10], params)
    atr = h4.atr14
    risk = plan.entry_reference - plan.stop_loss
    assert params.min_stop_atr * atr - 1e-9 <= risk <= params.max_stop_atr * atr + 1e-9
    cost = plan.entry_reference * params.round_trip_cost_pct / 100
    tp2 = plan.targets[1].price
    assert plan.reward_risk == pytest.approx((tp2 - plan.entry_reference - cost) / (risk + cost))
    assert plan.targets[0].price == pytest.approx(price * 1.05 - params.target_buffer_atr * atr)
    loss_pct = plan.stop_distance_pct + params.round_trip_cost_pct
    assert plan.suggested_allocation_pct == pytest.approx(min(100 * params.max_risk_per_signal_pct / loss_pct, 10.0))
    assert plan.room_to_resistance_r == pytest.approx((price * 1.05 - plan.entry_reference) / risk)


def test_plan_projections_in_price_discovery_and_room_under_resistance():
    discovery, _ = _plan([])
    assert all(t.projected for t in discovery.targets)
    assert [round(t.r_multiple, 2) for t in discovery.targets] == [1.5, 3.0, 4.5]
    assert discovery.room_to_resistance_r is None

    price = BULL(0.0)
    pinned, _ = _plan([price * 1.001])
    assert pinned.room_to_resistance_r < 0.75
    assert pinned.targets[0].price == pytest.approx(price * 1.001)  # nearest level is never skipped
    if pinned.better_entry_below is not None:
        assert pinned.stop_loss < pinned.better_entry_below < pinned.entry_reference


def test_final_validation_rejects_inconsistent_plans():
    params = RiskParams()
    plan, _ = _plan([])
    assert final_validation(SignalLabel.BUY, plan, integrity_passed=True, analysis_passed=True,
                            risk_blocked=False, params=params) == []
    broken = replace(plan, targets=list(reversed(plan.targets)))
    problems = final_validation(SignalLabel.BUY, broken, integrity_passed=True, analysis_passed=True,
                                risk_blocked=False, params=params)
    assert "targets must rise above the entry zone" in problems
    assert final_validation(SignalLabel.BUY, None, integrity_passed=False, analysis_passed=True,
                            risk_blocked=False, params=params)
    # WATCH and NO TRADE carry no buy promise, so nothing to validate.
    assert final_validation(SignalLabel.WATCH, broken, integrity_passed=False, analysis_passed=False,
                            risk_blocked=True, params=params) == []


def test_signal_label_ordering():
    assert SignalLabel.STRONG_BUY.cap(SignalLabel.WATCH) == SignalLabel.WATCH
    assert SignalLabel.WATCH.cap(SignalLabel.BUY) == SignalLabel.WATCH
    assert SignalLabel.NO_TRADE.rank < SignalLabel.WATCH.rank < SignalLabel.BUY.rank < SignalLabel.STRONG_BUY.rank
