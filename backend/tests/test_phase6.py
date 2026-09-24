"""Phase 6: swing engine upgrades, scalp engine + backtest, track record."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.analysis import scalp as sc
from app.analysis.trade_sim import BREAKEVEN, OPEN, STOP, TARGETS, TIME, SimTarget, simulate_long
from app.core.enums import RiskSeverity, SignalLabel, Timeframe, TrendDirection
from app.data.normalization.schemas import Candle
from app.database import create_session_factory
from app.models import Base, Signal, SignalOutcome, SignalTarget
from app.services.outcomes import OutcomeTracker
from tests.test_phase3_4 import settings_for
from tests.test_signal_engine import BULL, book, failed, regime, run
from tests.walk_market import WalkSpotAdapter, aggregate, walk

T0 = datetime(2026, 3, 2, tzinfo=UTC)


def bar(k: int, o: float, h: float, lo: float, c: float, tf: Timeframe = Timeframe.H1, start: datetime = T0) -> Candle:
    t = start + timedelta(seconds=tf.seconds * k)
    return Candle("fake", "XUSDT", tf, t, t + timedelta(seconds=tf.seconds), o, h, lo, c, 100.0, None, True)


# ----------------------------------------------------------------------------- swing engine upgrades


def test_altcoins_are_capped_while_bitcoin_4h_trend_is_down():
    market = regime()
    market.btc_trend_4h = TrendDirection.DOWN
    alt = run(market=market)
    assert alt.signal == SignalLabel.WATCH and "btc_4h" in failed(alt, RiskSeverity.CAP)
    assert alt.reasons[0].startswith("Bitcoin 4H trend is down")
    assert run(symbol="BTC", market=market).signal == SignalLabel.STRONG_BUY  # the rule is for altcoins
    market.btc_trend_4h = TrendDirection.UP
    assert run(market=market).signal == SignalLabel.STRONG_BUY


def test_no_buy_without_a_1h_entry_trigger():
    from app.analysis.engine import AnalysisInputs, SignalEngine
    from app.analysis.features import compute_snapshot
    from tests.conftest import market_candles
    from tests.test_signal_engine import NOW, integrity

    candles = market_candles(BULL, now=NOW, wick=0.004)
    h1 = candles[Timeframe.H1]
    # replace the last 1H candles with a steady decline: close below EMA20, MACD and RSI falling
    falling = []
    price = h1[-13].close
    for c in h1[-12:]:
        price *= 0.994
        falling.append(Candle(c.source, c.symbol, c.timeframe, c.open_time, c.close_time, price / 0.994,
                              price / 0.994 * 1.001, price * 0.999, price, c.volume, None, True))
    candles[Timeframe.H1] = h1[:-12] + falling
    snap = compute_snapshot(Timeframe.H1, candles[Timeframe.H1])
    assert snap.close < snap.ema20
    r = SignalEngine().evaluate(AnalysisInputs(
        symbol="ETH", name="Eth", universe_rank=2, now=NOW, supported=True, unsupported_reason=None, candles=candles,
        price=BULL(0.0), quote_asset="USDT", market_source="fake", quote_usd_rate=1.0, pct_change_24h=2.0,
        volume_24h_quote=5e8, order_book=book(BULL(0.0)), integrity=integrity(True), market=regime(),
    ))
    assert "entry_trigger" in failed(r, RiskSeverity.CAP) and r.signal.rank <= SignalLabel.WATCH.rank


def test_sellers_dominating_the_book_prevent_strong_buy():
    from dataclasses import replace

    from app.analysis.engine import AnalysisInputs, SignalEngine
    from tests.conftest import market_candles
    from tests.test_signal_engine import NOW, integrity

    assert run().signal == SignalLabel.STRONG_BUY
    heavy = replace(book(BULL(0.0)), bid_depth_quote=5e5, ask_depth_quote=2e6, imbalance=-0.6)
    r2 = SignalEngine().evaluate(AnalysisInputs(
        symbol="ETH", name="Eth", universe_rank=2, now=NOW, supported=True, unsupported_reason=None,
        candles=market_candles(BULL, now=NOW, wick=0.004), price=BULL(0.0), quote_asset="USDT", market_source="fake",
        quote_usd_rate=1.0, pct_change_24h=2.0, volume_24h_quote=5e8, order_book=heavy, integrity=integrity(True),
        market=regime(),
    ))
    assert r2.signal == SignalLabel.BUY and "book_pressure" in failed(r2, RiskSeverity.DOWNGRADE)


# ----------------------------------------------------------------------------- trade simulator


def test_simulator_is_pessimistic_and_moves_the_stop_to_break_even():
    targets = [SimTarget(102, 0.5), SimTarget(104, 0.5)]
    # stop and target inside the same candle: the stop counts
    both = [bar(0, 100, 102.5, 98.5, 101)]
    r = simulate_long(both, 0, entry=100, stop=99, targets=targets, cost_pct=0.2)
    assert r.outcome == STOP and r.r_multiple == pytest.approx((-1.0 - 0.2) / 1.0)
    # TP1, then back to the entry: half at +2%, half at 0%
    be = [bar(0, 100, 102.1, 99.5, 101.5), bar(1, 101.5, 101.6, 99.9, 100.2)]
    r = simulate_long(be, 0, entry=100, stop=99, targets=targets, cost_pct=0.2)
    assert r.outcome == BREAKEVEN and r.hit_targets == [1] and r.return_pct == pytest.approx(1.0 - 0.2)
    # the candle paying TP1 cannot also stop out the rest at break-even
    same = [bar(0, 100, 102.1, 99.5, 101.5), bar(1, 101.5, 104.2, 101.0, 104)]
    r = simulate_long(same, 0, entry=100, stop=99, targets=targets, cost_pct=0.2)
    assert r.outcome == TARGETS and r.hit_targets == [1, 2] and r.r_multiple == pytest.approx((3.0 - 0.2) / 1.0)
    # time exit at the close
    flat = [bar(k, 100, 100.5, 99.6, 100.3) for k in range(3)]
    r = simulate_long(flat, 0, entry=100, stop=99, targets=targets, cost_pct=0.2, max_hold=2)
    assert r.outcome == TIME and r.held == 2 and r.exit_index == 1 and r.return_pct == pytest.approx(0.3 - 0.2)
    # not enough candles: OPEN
    assert simulate_long(flat, 0, entry=100, stop=99, targets=targets, cost_pct=0.2, max_hold=10).outcome == OPEN
    with pytest.raises(ValueError):
        simulate_long(flat, 0, entry=100, stop=101, targets=targets, cost_pct=0.2)


# ----------------------------------------------------------------------------- scalp engine


def _market(seed=3, n5=24000):
    c5 = walk(n5, seed=seed, end=datetime(2026, 3, 2, 9, 40, tzinfo=UTC))
    return {tf: aggregate(c5, tf) for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)}


@pytest.fixture(scope="module")
def market():
    return _market()


def _series(market, horizon, setup_limit=None, until=None):
    prof = sc.PROFILES[horizon]
    setup = market[prof.setup] if until is None else [c for c in market[prof.setup] if c.close_time <= until]
    if setup_limit:
        setup = setup[-setup_limit:]
    cut = setup[-1].close_time
    trend = [c for c in market[prof.trend] if c.close_time <= cut]
    filt = [c for c in market[prof.filter] if c.close_time <= cut] if prof.filter in market else trend
    return sc.build_series(prof, setup, trend, filt)


def test_scalp_setups_use_no_future_data(market):
    """Every setup found with the full history is identical when the history ends at that candle."""
    p = sc.ScalpParams()
    for horizon in ("15m", "1h"):
        full = _series(market, horizon)
        found = [c for i in range(sc.WARMUP, len(full)) if (c := sc.evaluate_at(full, i, p)[0]) is not None]
        assert found, f"expected setups in the {horizon} test market"
        for cand in found[:15]:
            cut = _series(market, horizon, until=cand.time)
            again, _ = sc.evaluate_at(cut, len(cut) - 1, p)
            assert again is not None, (horizon, cand.time)
            assert again.kind == cand.kind
            assert (again.entry, again.stop, again.tp1, again.tp2) == pytest.approx(
                (cand.entry, cand.stop, cand.tp1, cand.tp2))


def test_scalp_plan_invariants_and_backtest_accounting(market):
    p = sc.ScalpParams()
    s = _series(market, "1h")
    stats = sc.backtest(s, p)
    assert stats.trades >= 5 and stats.wins <= stats.trades
    assert stats.total_r == pytest.approx(sum(t.r_multiple for t in stats.records))
    assert sum(stats.outcomes.values()) == stats.trades
    assert sum(v.trades for v in stats.by_setup.values()) == stats.trades
    times = [(t.entry_time, t.exit_time) for t in stats.records]
    assert all(b[0] >= a[1] for a, b in zip(times, times[1:], strict=False))  # one trade at a time
    for i in range(sc.WARMUP, len(s)):
        cand, _ = sc.evaluate_at(s, i, p)
        if cand is None:
            continue
        risk = cand.entry - cand.stop
        assert cand.stop < cand.entry < cand.tp1 < cand.tp2
        assert p.min_stop_atr * cand.atr - 1e-9 <= risk <= p.max_stop_atr * cand.atr + 1e-9
        assert cand.risk_pct >= p.min_risk_cost_multiple * p.cost_pct
        assert cand.tp1 - cand.entry >= p.min_tp1_r * risk - 1e-9


def test_fees_block_moves_that_are_too_small(market):
    s = _series(market, "15m")
    expensive = sc.ScalpParams(fee_pct=0.5)  # 1.04% round trip
    reasons = [sc.evaluate_at(s, i, expensive, explain=True)[1] for i in range(sc.WARMUP, len(s))]
    assert not any(sc.evaluate_at(s, i, expensive)[0] for i in range(sc.WARMUP, len(s)))
    assert any(r and r[0].startswith("move too small for fees") for r in reasons)


def test_bitcoin_down_blocks_altcoin_scalps_but_not_bitcoin(market):
    prof = sc.PROFILES["1h"]
    btc_down = [Candle("fake", "BTCUSDT", Timeframe.H1, c.open_time, c.close_time, 100 - k * 0.1, 100 - k * 0.1,
                       99 - k * 0.1, 99 - k * 0.1, 1.0, None, True) for k, c in enumerate(market[Timeframe.H1])]
    s = sc.build_series(prof, market[prof.setup], market[prof.trend], market[prof.filter], btc_down)
    p = sc.ScalpParams()
    idx = range(sc.WARMUP, len(s))
    assert not any(sc.evaluate_at(s, i, p)[0] for i in idx)
    assert any(sc.evaluate_at(s, i, p, is_btc=True)[0] for i in idx)
    assert any("Bitcoin" in (sc.evaluate_at(s, i, p, explain=True)[1] or [""])[0] for i in idx)


def _stats(rs, kind="pullback"):
    records = [sc.TradeRecord(kind, T0 + timedelta(hours=k), T0 + timedelta(hours=k, minutes=30), 100, 99, "X", r, r, 3)
               for k, r in enumerate(rs)]
    return sc.summarize(records, horizon="1h", setup_timeframe="15m", candles=1000, period_start=T0,
                        period_end=T0 + timedelta(days=30), cost_pct=0.24)


def test_evidence_gate():
    p = sc.ScalpParams()
    few = _stats([0.5, 0.5])
    assert sc.evidence(few, "pullback", p)[0] == SignalLabel.WATCH
    losing_few = _stats([-1.2, -1.2, -1.2])
    assert sc.evidence(losing_few, "pullback", p)[0] == SignalLabel.NO_TRADE
    good_pool = sc.pool([_stats([0.6, -1.0, 1.4, 0.5] * 12)])
    label, reasons, source = sc.evidence(few, "pullback", p, good_pool)
    assert (label, source) == (SignalLabel.BUY, "pooled") and "all scanned coins" in reasons[0]
    bad_pool = sc.pool([_stats([-1.0, 0.2] * 25)])
    assert sc.evidence(few, "pullback", p, bad_pool)[0] == SignalLabel.NO_TRADE
    losing = _stats([-1.0, 0.3] * 10)
    assert sc.evidence(losing, "pullback", p)[0] == SignalLabel.NO_TRADE
    ok = _stats([1.0, -1.0, 0.8, 0.4] * 5)  # 20 trades, +0.3R, 75% wins
    assert sc.evidence(ok, "pullback", p)[0] == SignalLabel.BUY  # fewer than 25 trades: not STRONG
    strong = _stats([1.0, -1.0, 0.8, 0.4] * 8)
    assert sc.evidence(strong, "pullback", p)[0] == SignalLabel.STRONG_BUY
    # the setup type itself must not be losing
    mixed = sc.pool([_stats([1.0, 0.8] * 10, "breakout"), _stats([-0.3] * 10, "pullback")])
    assert sc.evidence(mixed, "pullback", p)[0] == SignalLabel.WATCH
    assert _stats([0.5] * 3).profit_factor == float("inf")


# ----------------------------------------------------------------------------- service and API


@pytest.fixture
async def scalp_env(tmp_path, request):
    extra = getattr(request, "param", {})
    settings = settings_for(tmp_path, **extra)
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = create_session_factory(engine)
    from app.main import create_app
    from app.services.container import build_container
    from tests.conftest import FakeAltcoinSeasonAdapter, FakeFearGreedAdapter, FakeGlobalAdapter, FakeListingAdapter

    def factory(s):
        c = build_container(
            s, engine=engine, session_factory=sessions, spot_adapters=[WalkSpotAdapter("fakeex", n5=16000)],
            listing_adapters=[FakeListingAdapter("fakelist")], global_adapters=[FakeGlobalAdapter()],
            fear_greed_adapters=[FakeFearGreedAdapter()], altcoin_season_adapters=[FakeAltcoinSeasonAdapter()],
            reference_candle_adapter=None,
        )
        offline = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
        c.news._http = c.onchain._http = c.sentiment._http = offline
        return c

    app = create_app(settings, container_factory=factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            await http.get("/api/market")
            await http.put("/api/control/selection", json={"mode": "selected", "symbols": ["BTC", "ETH", "SOL", "ADA"]})
            yield http, app.state.container, sessions
    await engine.dispose()


async def test_scalp_scan_api(scalp_env):
    http, c, _ = scalp_env
    assert (await http.get("/api/scalp", params={"horizon": "1h"})).json()["result"] is None
    assert (await http.post("/api/scalp/scan", params={"horizon": "2h"})).status_code == 422
    started = (await http.post("/api/scalp/scan", params={"horizon": "1h"})).json()
    assert started["started"] is True
    await c.scalp.wait("1h")
    body = (await http.get("/api/scalp", params={"horizon": "1h"})).json()
    assert body["status"]["1h"]["outcome"] == "completed" and body["status"]["1h"]["done"] == 4
    res = body["result"]
    assert res["setup_timeframe"] == "15m" and res["max_hold_minutes"] == 120 and not res["errors"]
    assert {s["symbol"] for s in res["signals"]} == {"BTC", "ETH", "SOL", "ADA"}  # the selection only
    assert res["pooled"]["coins"] == 4 and res["pooled"]["trades"] == sum(s["backtest"]["trades"] for s in res["signals"])
    for sig in res["signals"]:
        assert sig["data_ok"] and sig["backtest"]["candles"] > 1000 and "records" not in sig["backtest"]
        assert sig["signal"] in ("STRONG BUY", "BUY", "WATCH", "NO TRADE") and sig["summary"]
        if sig["plan"]:
            pl = sig["plan"]
            assert pl["stop"] < pl["entry_low"] < pl["entry"] < pl["entry_high"] < pl["tp1"] < pl["tp2"]
            assert pl["suggested_allocation_pct"] <= 10.0 + 1e-9 and pl["risk_at_allocation_pct"] <= 1.0 + 1e-9
    single = await http.get("/api/scalp/SOL", params={"horizon": "4h"})
    assert single.status_code == 200 and single.json()["horizon"] == "4h"
    assert (await http.get("/api/scalp/NOPE", params={"horizon": "1h"})).status_code == 404
    leo = await http.get("/api/scalp/LEO", params={"horizon": "1h"})  # in the Top 20, no spot market
    assert leo.status_code == 200 and leo.json()["signal"] == "NO TRADE" and leo.json()["plan"] is None
    status = (await http.get("/api/control/status")).json()
    assert status["scalp"]["1h"]["outcome"] == "completed"


async def test_scalp_judging_live_checks(scalp_env):
    """A setup is only a buy while the live price is still near its entry."""
    http, c, _ = scalp_env
    from app.services.scalp import _Work

    good_pool = sc.pool([_stats([0.6, -1.0, 1.4, 0.5] * 12)])
    universe = await c.universe.get()
    asset = universe.find("ETH")
    now = datetime.now(tz=UTC)
    cand = sc.Candidate("pullback", 100, now - timedelta(minutes=5), 3000.0, 2970.0, 3030.0, 3060.0, 1.0, 20.0,
                        ["test setup"])
    good_book = book(3000.0)

    def work(price):
        return _Work(asset, _stats([0.5]), cand, [], True, price, "USDT", True, [], good_book, 1e9, 1.0)

    prof, p = sc.PROFILES["1h"], c.scalp.params()
    fresh = await c.scalp._judge(work(3003.0), prof, p, good_pool)
    assert fresh.signal == SignalLabel.BUY and fresh.evidence == "pooled" and fresh.plan.entry == 3000.0
    assert fresh.plan.reward_risk_tp2 > fresh.plan.reward_risk_tp1 > 0
    ran = await c.scalp._judge(work(3015.0), prof, p, good_pool)
    assert ran.signal == SignalLabel.WATCH and ran.reasons[0].startswith("price ran")
    stopped = await c.scalp._judge(work(2960.0), prof, p, good_pool)
    assert stopped.signal == SignalLabel.NO_TRADE and stopped.reasons[0].startswith("invalidated")
    wide = _Work(asset, _stats([0.5]), cand, [], True, 3003.0, "USDT", True, [], book(3000.0, spread_bps=80), 1e9, 1.0)
    assert (await c.scalp._judge(wide, prof, p, good_pool)).signal == SignalLabel.NO_TRADE
    # a buy is stored once for the track record
    await c.scalp._judge(work(3003.0), prof, p, good_pool)
    async with c.session_factory() as s:
        rows = (await s.execute(select(Signal).where(Signal.strategy == "scalp_1h"))).scalars().all()
        assert len(rows) == 1 and rows[0].status == "OPEN" and rows[0].entry_high == 3000.0
        assert await s.scalar(select(func.count()).select_from(SignalTarget).where(SignalTarget.signal_id == rows[0].id)) == 3


@pytest.mark.parametrize("scalp_env", [{"admin_token": "tok"}], indirect=True)
async def test_scalp_scan_needs_the_admin_token(scalp_env):
    http, _, _ = scalp_env
    assert (await http.post("/api/scalp/scan", params={"horizon": "1h"})).status_code == 401
    assert (await http.get("/api/scalp", params={"horizon": "1h"})).status_code == 200
    # every control action returns the full status (the dashboard keeps its "Enter admin token" button)
    for path, body in (("/api/control/stop", None), ("/api/control/auto", {"minutes": 0}), ("/api/control/analyze", None)):
        r = (await http.post(path, json=body, headers={"X-Admin-Token": "tok"})).json()
        assert r["auth_required"] is True and "scalp" in r and "watchlist" in r


# ----------------------------------------------------------------------------- track record


async def _signal(sessions, *, symbol="ETH", strategy="scalp_1h", created=T0, entry=100.0, stop=99.0,
                  tps=((101.0, 50.0), (102.0, 50.0)), label="BUY", max_hold=8):
    async with sessions() as s:
        sig = Signal(symbol=symbol, timeframe="15m", strategy=strategy, signal=label, signal_score=60,
                     data_health_score=100, data_state="HEALTHY", entry_low=entry, entry_high=entry, stop_loss=stop,
                     status="OPEN", reasons=[], risks=[], input_features={}, quant_output={"max_hold": max_hold},
                     created_at=created)
        s.add(sig)
        await s.flush()
        s.add_all([SignalTarget(signal_id=sig.id, kind="TP", level_index=i, price=p, allocation_pct=a)
                   for i, (p, a) in enumerate(tps, start=1)])
        await s.commit()
        return sig.id


async def test_track_record_follows_signals(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = create_session_factory(engine)
    tracker = OutcomeTracker(sessions)
    m15 = Timeframe.M15
    win = await _signal(sessions, created=T0)
    overlap = await _signal(sessions, created=T0 + timedelta(minutes=10))  # during the first trade
    loss = await _signal(sessions, created=T0 + timedelta(hours=3))
    running = await _signal(sessions, created=T0 + timedelta(hours=6))
    candles = (
        [bar(0, 100, 100.4, 99.6, 100.2, m15), bar(1, 100.2, 101.2, 100.0, 101.0, m15),
         bar(2, 101.0, 102.3, 100.8, 102.0, m15)]  # TP1 then TP2
        + [bar(k, 101, 101.2, 100.8, 101, m15) for k in range(3, 12)]
        + [bar(12, 101, 101.1, 98.5, 99, m15)]  # stop for the 3h signal
        + [bar(k, 100, 100.4, 99.6, 100.1, m15) for k in range(13, 27)]  # the 6h signal is still running
    )
    assert await tracker.update("ETH", {m15: candles}) == 2
    async with sessions() as s:
        status = {sid: (await s.get(Signal, sid)).status for sid in (win, overlap, loss, running)}
        outcomes = {o.signal_id: o for o in (await s.execute(select(SignalOutcome))).scalars()}
    assert status == {win: "WIN", overlap: "SKIPPED", loss: "LOSS", running: "OPEN"}
    assert outcomes[win].outcome == TARGETS and outcomes[win].hit_targets == [1, 2]
    assert outcomes[loss].outcome == STOP and outcomes[loss].r_multiple < -1
    # a second pass changes nothing
    assert await tracker.update("ETH", {m15: candles}) == 0
    perf = await tracker.performance(days=3650)
    row = perf["strategies"][0]
    assert row["strategy"] == "scalp_1h" and row["closed"] == 2 and row["wins"] == 1 and row["win_rate"] == 50.0
    assert row["open"] == 1 and row["skipped"] == 1 and len(perf["recent"]) == 2
    # candles of another timeframe do not touch these signals
    assert await tracker.update("ETH", {Timeframe.H1: candles}) == 0
    # a signal whose candles are gone (server down past its holding period) expires
    old = await _signal(sessions, symbol="SOL", created=T0 - timedelta(days=5))
    await tracker.update("SOL", {m15: candles})
    async with sessions() as s:
        assert (await s.get(Signal, old)).status == "EXPIRED"
    await engine.dispose()


async def test_performance_endpoint(scalp_env):
    http, _, _ = scalp_env
    body = (await http.get("/api/performance", params={"days": 30})).json()
    assert body["days"] == 30 and body["persistence"] == "ok" and body["strategies"] == []
    assert (await http.get("/api/performance", params={"days": 0})).status_code == 422


@pytest.mark.skipif(not __import__("os").getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
async def test_postgres_track_record():
    """Signals, outcomes and the performance query on a real PostgreSQL (timestamptz, JSONB)."""
    import os

    from app.config import Settings

    settings = Settings(_env_file=None, database_url=os.environ["TEST_DATABASE_URL"], json_logs=False)
    engine = create_async_engine(settings.async_database_url, connect_args=settings.database_connect_args)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DELETE FROM signals WHERE symbol = 'PGT'")
    sessions = create_session_factory(engine)
    tracker = OutcomeTracker(sessions)
    m15 = Timeframe.M15
    start = datetime.now(tz=UTC).replace(second=0, microsecond=0) - timedelta(hours=10)
    start = start - timedelta(minutes=start.minute % 15)
    win = await _signal(sessions, symbol="PGT", created=start)
    candles = [bar(0, 100, 100.4, 99.6, 100.2, m15, start), bar(1, 100.2, 101.2, 100.0, 101.0, m15, start),
               bar(2, 101.0, 102.3, 100.8, 102.0, m15, start)]
    assert await tracker.update("PGT", {m15: candles}) == 1
    async with sessions() as s:
        assert (await s.get(Signal, win)).status == "WIN"
    perf = await tracker.performance(days=2)
    assert perf["persistence"] == "ok" and any(r["symbol"] == "PGT" for r in perf["recent"])
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DELETE FROM signals WHERE symbol = 'PGT'")
    await engine.dispose()
