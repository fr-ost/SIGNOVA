"""Phase 11: managed exits, the strategy library, walk-forward research, verdicts, 1-day horizon."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.analysis import scalp as sc
from app.analysis import strategies as st
from app.analysis.trade_sim import EXIT, STOP, TRAIL, SimTarget, simulate_long
from app.core.enums import SignalLabel, Timeframe
from app.models import Signal
from tests.test_phase6 import T0, _series, bar
from tests.test_phase6 import market  # noqa: F401  (fixture)
from tests.test_phase7_9 import env  # noqa: F401  (fixture)


# ----------------------------------------------------------------------------- managed exits


def test_trailing_stop_rises_and_exits_in_profit():
    candles = [bar(0, 100, 100.5, 99.5, 100)] + [bar(k, 100 + k, 101 + k, 99.8 + k, 100.8 + k) for k in range(1, 8)] + [
        bar(8, 108, 108.2, 103.0, 103.5)]
    rules = st.ExitRules(candles)
    trail, _ = rules.functions(st.ExitSpec("trail", trail="donchian", trail_len=3, breakeven=False), 0)
    res = simulate_long(candles, 1, entry=100.0, stop=97.0, targets=[], cost_pct=0.2, max_hold=20, trail=trail)
    assert res.outcome == TRAIL and res.exit_price > 100 and res.r_multiple > 0
    assert res.exit_index == 8  # the stop only applies from the candle after it was set


def test_exit_signal_and_gap_fill():
    candles = [bar(0, 100, 100.2, 99.8, 100), bar(1, 100, 100.3, 99.0, 99.2), bar(2, 99.2, 101.5, 99.1, 101.4)]
    rules = st.ExitRules(candles)
    _, exit_signal = rules.functions(st.ExitSpec("mean", exit_ma=2, breakeven=False), 0)
    res = simulate_long(candles, 1, entry=100.0, stop=97.0, targets=[], cost_pct=0.2, max_hold=5, exit_signal=exit_signal)
    assert res.outcome == EXIT and res.exit_price == 101.4
    gap = [bar(0, 100, 100.2, 99.8, 100), bar(1, 95.0, 96.0, 94.0, 95.5)]  # opens far below the stop
    res = simulate_long(gap, 1, entry=100.0, stop=98.0, targets=[SimTarget(104.0, 1.0)], cost_pct=0.2)
    assert res.outcome == STOP and res.exit_price == 95.0 and res.r_multiple < -2  # filled at the open, not the stop
    with pytest.raises(ValueError):
        simulate_long(gap, 1, entry=100.0, stop=98.0, targets=[], cost_pct=0.2)


# ----------------------------------------------------------------------------- strategies


def test_library_strategies_use_no_future_data(market):  # noqa: F811
    """Every library signal found with the full history is identical when the history ends at that candle."""
    p = sc.ScalpParams()
    full = _series(market, "1h")
    seen = 0
    for key in st.LIBRARY:
        found = [c for i in range(sc.WARMUP, len(full)) if (c := st.signal_at(full, i, key, p)) is not None]
        for cand in found[:6]:
            cut = _series(market, "1h", until=cand.time)
            again = st.signal_at(cut, len(cut) - 1, key, p)
            assert again is not None, (key, cand.time)
            assert (again.entry, again.stop, again.tp1, again.tp2) == pytest.approx((cand.entry, cand.stop, cand.tp1, cand.tp2))
            seen += 1
    assert seen >= 12  # most strategies fire in the test market


def test_library_backtest_accounting(market):  # noqa: F811
    s = _series(market, "1h")
    for key in st.LIBRARY:
        records = st.backtest_key(s, key, sc.ScalpParams(), symbol="ETH")
        for a, b in zip(records, records[1:], strict=False):
            assert b.entry_time >= a.exit_time  # one trade at a time
        for t in records:
            assert t.kind == key and t.symbol == "ETH" and t.stop < t.entry
            assert t.outcome in ("STOP", "TRAIL", "EXIT", "TARGETS", "BREAKEVEN", "TIME")
    assert set(st.STRATEGIES) == {"pullback", "breakout", *st.LIBRARY}
    for key in st.LIBRARY:
        s_ = st.STRATEGIES[key]
        assert s_.source and s_.rule and st.exit_text(s_.exit, 100.0, 98.0, 104.0)


def _records(rs, start=T0, key="donchian"):
    return [sc.TradeRecord(key, start + timedelta(hours=k), start + timedelta(hours=k, minutes=30), 100, 99, "TRAIL",
                           r, r, 3) for k, r in enumerate(rs)]


def test_research_needs_profit_on_both_parts_and_enough_trades():
    split = T0 + timedelta(hours=70)
    good = _records([1.5, -1.0, 0.8, -0.6, 1.2] * 20)  # +0.38R, 100 trades
    r = st.research("donchian", {"A": (good, split)})
    assert r.validated and r.train.trades == 70 and r.test.trades == 30 and r.t_stat > 1.5
    decays = _records([1.5, -1.0, 0.8, -0.6, 1.2] * 14 + [-1.0, 0.3] * 15)
    r = st.research("donchian", {"A": (decays, split)})
    assert not r.validated and "newer 30% did not confirm" in r.reasons[0]
    few = _records([1.0, -0.5] * 10)
    assert "only" in st.research("donchian", {"A": (few, split)}).reasons[0]
    noisy = _records([3.0, -1.0, -1.0, -0.9] * 25)  # +0.03R: too little and could be luck
    assert not st.research("donchian", {"A": (noisy, split)}).validated


def test_verdicts():
    split = T0 + timedelta(hours=70)
    res = st.research("donchian", {"A": (_records([1.5, -1.0, 0.8, -0.6, 1.2] * 20), split)})
    label, why = st.verdict("donchian", _records([1.0, -1.0, 2.0]), res)
    assert label == SignalLabel.BUY and "validated" in why[0]
    assert st.verdict("donchian", _records([-1.0] * 9), res)[0] == SignalLabel.WATCH  # lost on this coin
    strong = _records([1.5, -1.0, 1.2, 0.9] * 5)
    assert st.verdict("donchian", strong, res)[0] == SignalLabel.STRONG_BUY
    losing = st.research("donchian", {"A": (_records([-1.0, 0.5] * 50), split)})
    assert st.verdict("donchian", [], losing)[0] == SignalLabel.NO_TRADE
    assert st.verdict("donchian", [], None)[0] == SignalLabel.WATCH


# ----------------------------------------------------------------------------- integration


async def test_validated_library_strategy_gives_a_buy_with_its_exit_rules(env):  # noqa: F811
    http, c, sessions, _ = env
    from app.services.outcomes import OutcomeTracker
    from app.services.scalp import _Work
    from tests.test_phase6 import _stats
    from tests.test_signal_engine import book

    await c.evidence.update(mode="advisory")
    asset = (await c.universe.get()).find("ETH")
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    cand = sc.Candidate("donchian", 500, now - timedelta(minutes=10), 3000.0, 2940.0, 3120.0, 3180.0, 2.0, 30.0,
                        ["15m close above the 55-candle high"])
    split = T0 + timedelta(hours=70)
    good = _records([1.5, -1.0, 0.8, -0.6, 1.2] * 20)
    c.scalp._research["1h"] = {"donchian": st.research("donchian", {"A": (good, split)})}
    work = _Work(asset, _stats([-1.0, 0.2] * 10), None, [], True, 3003.0, "USDT", True, [], book(3000.0), 1e9, None,
                 library={"donchian": _records([1.0, -1.0, 2.0])}, library_now={"donchian": cand})
    r = await c.scalp._judge(work, sc.PROFILES["1h"], c.scalp.params("1h"), None)
    assert r.signal == SignalLabel.BUY and r.setup == "donchian" and r.evidence == "research"
    assert r.strategy["name"] == "Turtle 55-candle breakout" and "Turtle" in r.strategy["source"]
    assert "trail" in r.plan.exit_rule and r.strategy["hold"] == sc.PROFILES["1h"].max_hold * 4
    # quote USD rate missing: a USDT pair still passes the liquidity checks
    assert not any("volume" in x for x in r.reasons)
    async with sessions() as s:
        row = (await s.execute(select(Signal).where(Signal.symbol == "ETH", Signal.strategy == "scalp_1h"))).scalar_one()
        assert row.signal == "BUY" and row.quant_output["exit"]["trail"] == "donchian"
        assert row.quant_output["strategy"] == "donchian" and row.quant_output["max_hold"] == 32
        created = row.created_at
    # the track record follows the same trailing stop as the backtest
    m15 = Timeframe.M15
    start = created.replace(tzinfo=UTC) - timedelta(minutes=15 * 25)
    start = start - timedelta(minutes=start.minute % 15, seconds=start.second, microseconds=start.microsecond)
    candles = [bar(k, 3000, 3002, 2998, 3000, m15, start) for k in range(26)]
    candles += [bar(26 + k, 3000 + 20 * k, 3025 + 20 * k, 2995 + 20 * k, 3020 + 20 * k, m15, start) for k in range(25)]
    candles += [bar(51, 3480, 3482, 3000, 3010, m15, start)]  # falls through the trailing stop (about 3095)
    assert await OutcomeTracker(sessions).update("ETH", {m15: candles}) == 1
    async with sessions() as s:
        row = (await s.execute(select(Signal).where(Signal.symbol == "ETH", Signal.strategy == "scalp_1h"))).scalar_one()
        assert row.status == "WIN"


async def test_no_buy_reason_and_research_in_scans(env):  # noqa: F811
    http, c, _, _ = env
    await http.put("/api/control/selection", json={"mode": "selected", "symbols": ["BTC", "ETH", "SOL"]})
    await http.post("/api/scalp/scan", params={"horizon": "1h"})
    await c.scalp.wait("1h")
    res = (await http.get("/api/scalp", params={"horizon": "1h"})).json()["result"]
    assert {r["key"] for r in res["research"]} == {"pullback", "breakout", *st.LIBRARY}
    for r in res["research"]:
        assert set(r) >= {"name", "source", "rule", "exit", "train", "test", "all", "validated", "reasons"}
    buys = [x for x in res["signals"] if x["signal"] in ("BUY", "STRONG BUY")]
    assert (res["no_buy_reason"] is None) == bool(buys)
    if not buys:  # one of: nothing pays here / promising but too few trades / validated, with what fired
        assert any(k in res["no_buy_reason"] for k in ("None of the 8", "Promising", "Validated here", "held back"))


async def test_one_day_horizon(env):  # noqa: F811
    http, c, _, _ = env
    assert sc.PROFILES["1d"].setup == Timeframe.H4 and sc.PROFILES["1d"].trend == Timeframe.D1
    await http.put("/api/control/selection", json={"mode": "selected", "symbols": ["BTC", "ETH"]})
    assert (await http.post("/api/scalp/scan", params={"horizon": "1d"})).status_code == 200
    await c.scalp.wait("1d")
    body = (await http.get("/api/scalp", params={"horizon": "1d"})).json()
    assert body["status"]["1d"]["outcome"] == "completed", body["status"]["1d"]
    assert body["result"]["label"] == "1-day trade" and body["result"]["setup_timeframe"] == "4H"
    assert (await http.get("/api/lab", params={"horizon": "1d"})).status_code == 200
    assert (await http.get("/api/evidence/ETH", params={"horizon": "1d"})).status_code in (200, 404)
