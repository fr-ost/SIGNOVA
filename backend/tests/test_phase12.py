"""Phase 12: futures signals (long and short), the mirror, leverage plans, side-aware evidence, shorts in the record."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.analysis import evidence as ev
from app.analysis import futures as fu
from app.analysis import scalp as sc
from app.analysis import strategies as st
from app.core.enums import SignalLabel, Timeframe
from app.data.derivatives import Liquidation, Point
from app.models import Signal
from tests.test_phase6 import T0, bar
from tests.test_phase7_9 import env  # noqa: F401  (fixture)
from tests.test_phase10 import NOW, board, hourly, snapshot
from tests.walk_market import aggregate, walk


def _market(drift: float, seed: int = 5):
    c5 = walk(24000, seed=seed, end=datetime(2026, 3, 2, 9, 40, tzinfo=UTC), drift=drift)
    return {tf: aggregate(c5, tf) for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)}


def _sides(market, horizon="1h"):
    prof = sc.PROFILES[horizon]
    setup, trend, filt = market[prof.setup], market[prof.trend], market[prof.filter]
    p = sc.ScalpParams(fee_pct=0.05, slippage_pct=0.02)
    k = fu.mirror_k(setup)
    long_a = fu.analyze_side("long", sc.build_series(prof, setup, trend, filt), 1.0, p, symbol="X")
    inv = [fu.invert_candles(c, k) for c in (setup, trend, filt)]
    short_a = fu.analyze_side("short", sc.build_series(prof, *inv), k, p, symbol="X")
    return long_a, short_a, k, p


# ----------------------------------------------------------------------------- the mirror


def test_mirror_turns_a_downtrend_into_an_uptrend():
    c = bar(0, 100.0, 104.0, 95.0, 98.0)
    c = type(c)(**{**{f: getattr(c, f) for f in c.__slots__}, "volume": 10.0, "taker_buy_base": 7.0})
    k = 100.0 * 100.0
    m = fu.invert_candles([c], k)[0]
    assert m.open == pytest.approx(100.0) and m.high == pytest.approx(k / 95.0) and m.low == pytest.approx(k / 104.0)
    assert m.close == pytest.approx(k / 98.0) and m.taker_buy_base == pytest.approx(3.0)  # sellers become the buyers
    assert fu.invert_candles(fu.invert_candles([c], k), k)[0].close == pytest.approx(98.0)
    cand = sc.Candidate("donchian", 10, T0, 100.0, 98.0, 104.0, 106.0, 2.0, 1.0, [], level=103.0)
    real = fu.unmirror(cand, k)
    assert real.entry == pytest.approx(100.0) and real.stop > real.entry > real.tp1 > real.tp2
    assert real.risk_pct == pytest.approx((k / 98.0 - 100.0) / 100.0 * 100.0)
    assert fu.label_text("short", SignalLabel.STRONG_BUY) == "STRONG SHORT" and fu.label_text("long", SignalLabel.BUY) == "LONG"


def test_shorts_are_found_in_a_downtrend_and_longs_in_an_uptrend():
    down_long, down_short, _, _ = _sides(_market(-0.00006))
    up_long, up_short, _, _ = _sides(_market(+0.00006, seed=7))
    trades = lambda a: sum(len(v) for v in a.records.values())  # noqa: E731
    assert trades(down_short) > 2 * trades(down_long)
    assert trades(up_long) > 2 * trades(up_short)
    for key, cand in down_short.now.items():
        assert cand.stop > cand.entry > cand.tp1, key
    lib = [key for key in st.LIBRARY if down_short.records.get(key)]
    assert len(lib) >= 4  # the whole library works on the mirror


# ----------------------------------------------------------------------------- leverage


def test_research_table_puts_evidence_before_a_few_lucky_trades():
    def row(key, train, test, mean, validated=False):
        def split(n):
            return st.Split(n, 50.0, mean, None, n * mean)

        return st.Research(key, key, "", "", "", "", 5, split(train), split(test), split(train + test), None, validated)

    lucky = row("lucky", 3, 6, 1.2)  # +1.2R over 9 trades: not evidence
    close = row("close", 58, 24, 0.1)  # nearly enough trades to judge
    judged = row("judged", 70, 28, 0.02)
    losing = row("losing", 90, 30, -0.1)
    proven = row("proven", 80, 30, 0.2, validated=True)
    order = [r.key for r in sorted([lucky, losing, close, judged, proven], key=st.research_order)]
    assert order == ["proven", "judged", "losing", "close", "lucky"]


def test_leverage_plan_keeps_liquidation_far_beyond_the_stop():
    plan = fu.leverage_plan("long", 100.0, 98.0, cost_pct=0.14, funding_rate_pct_8h=0.01, hold_hours=8,
                            risk_pct_equity=1.0, max_leverage=20, mmr_pct=1.0, equity=1000.0)
    assert plan.max_safe_leverage == pytest.approx(25.0) and plan.leverage == 20  # 1 / (2% stop + 1% buffer + 1% MMR), capped
    assert plan.liquidation_price < plan.stop  # below the stop for a long...
    assert (plan.stop - plan.liquidation_price) / plan.entry * 100 >= fu.LIQ_BUFFER_MIN_PCT - 1e-9  # ...with room
    assert plan.loss_at_stop_usd == pytest.approx(10.0)  # the stop costs 1% of equity whatever the leverage
    assert plan.margin_usd == pytest.approx(plan.notional_usd / plan.leverage)
    short = fu.leverage_plan("short", 100.0, 102.0, cost_pct=0.14, funding_rate_pct_8h=0.05, hold_hours=16,
                             risk_pct_equity=1.0, max_leverage=5, mmr_pct=1.0)
    assert short.leverage == 5 and short.liquidation_price > short.stop and short.funding_pct < 0  # shorts receive funding
    wide = fu.leverage_plan("long", 100.0, 80.0, cost_pct=0.14, funding_rate_pct_8h=None, hold_hours=48,
                            risk_pct_equity=1.0, max_leverage=20, mmr_pct=1.0)
    assert wide.leverage <= 3 and wide.liquidation_price < 80.0
    tight = fu.leverage_plan("long", 100.0, 99.9, cost_pct=0.14, funding_rate_pct_8h=0.01, hold_hours=1,
                             risk_pct_equity=1.0, max_leverage=1, mmr_pct=1.0)
    assert tight.margin_pct_of_equity <= 100.0 and tight.notes and "capped" in tight.notes[0]


# ----------------------------------------------------------------------------- evidence for shorts


def test_evidence_board_flips_for_shorts():
    hours = [NOW - timedelta(hours=48 - k) for k in range(49)]
    crowded_longs = snapshot(
        funding_pct=0.12, oi_history=[Point(t, 1000.0 * (1.01 ** k)) for k, t in enumerate(hours)],
        long_share=[Point(t, 0.76) for t in hours], top_long_share=[Point(t, 0.6 - 0.004 * k) for k, t in enumerate(hours)],
        taker_ratio=[Point(t, 0.8) for t in hours],
    )
    long_board = board(deriv=crowded_longs)
    short_board = board(deriv=crowded_longs, side="short")
    assert long_board.vetoes and long_board.score <= ev.STRONG_AGAINST
    assert not short_board.vetoes and short_board.score >= ev.STRONG_FOR and short_board.side == "short"
    assert ev.apply_to_label(SignalLabel.BUY, short_board, "filter")[0] == SignalLabel.BUY
    crowded_shorts = snapshot(
        funding_pct=-0.08, oi_history=[Point(t, 1000.0 * (1.006 ** k)) for k, t in enumerate(hours)],
        long_share=[Point(t, 0.35) for t in hours],
    )
    squeeze_risk = board(deriv=crowded_shorts, side="short", h1=hourly(200, lambda k: 100.0 * (1 + 0.0005 * k)))
    assert any("crowded, leveraged shorts" in v for v in squeeze_risk.vetoes)
    squeeze = snapshot(sources={"liquidations": "okx"}, open_interest_usd=50e6, funding_pct=None,
                       liquidations=[Liquidation(NOW - timedelta(minutes=15), "short", 120.0, 900_000.0)])
    rising = [type(c)(**{**{f: getattr(c, f) for f in c.__slots__}, "open": c.close - 0.3})
              for c in hourly(60, lambda k: 100.0 + 0.3 * k)]
    assert any("short squeeze in progress" in v for v in board(deriv=squeeze, h1=rising, side="short").vetoes)
    assert not board(deriv=squeeze, h1=rising).vetoes  # no squeeze veto for longs
    listing = [(NOW - timedelta(hours=3), "Binance lists SOL perpetuals and spot", "neutral")]
    assert any("do not short" in v for v in board(headlines=listing, side="short").vetoes)
    hack = [(NOW - timedelta(hours=2), "Solana DeFi protocol hacked for $50M", "negative")]
    assert board(headlines=hack).vetoes and not board(headlines=hack, side="short").vetoes


def test_short_liquidation_zone_at_the_stop_gives_a_hint_above():
    h1 = hourly(80, lambda k: 100.0)
    oi = [Point(c.close_time, 1000.0 + 20 * k) for k, c in enumerate(h1[10:70])]
    deriv = snapshot(oi_history=oi, long_share=[], sources={"oi_history": "binance"}, funding_pct=None,
                     top_long_share=[], taker_ratio=[])
    b = board(deriv=deriv, h1=h1, side="short", entry=100.0, stop=109.0, tp1=91.0, tp2=82.0)
    assert b.stop_hint is not None and b.stop_hint > 109.0
    assert any("squeeze there can run through a stop" in n for n in b.notes)
    assert any("fuel a drop toward the targets" in n for n in b.notes)


# ----------------------------------------------------------------------------- the service


def _research(side, key, split, rs=(1.5, -1.0, 0.8, -0.6, 1.2)):
    from tests.test_phase11 import _records

    return {(side, key): st.research(key, {"A": (_records(list(rs) * 20, key=key), split)})}


async def test_validated_short_becomes_a_futures_signal_with_a_safe_plan(env):  # noqa: F811
    http, c, sessions, _ = env
    from app.services.futures import _FWork
    from app.services.outcomes import OutcomeTracker
    from tests.test_phase11 import _records
    from tests.test_signal_engine import book

    long_a, short_a, k, _ = _sides(_market(-0.00006))
    asset = (await c.universe.get()).find("ETH")
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    cand = sc.Candidate("donchian", 500, now - timedelta(minutes=10), 3000.0, 3060.0, 2880.0, 2820.0, 2.0, 30.0, [])
    short_a.now = {"donchian": cand}
    short_a.records = {"donchian": _records([1.0, -1.0, 2.0], key="donchian")}
    long_a.now = {}
    c.futures._research["1h"] = _research("short", "donchian", T0 + timedelta(hours=70))
    w = _FWork(asset=asset, price=2995.0, quote_asset="USDT", book=book(3000.0), volume_24h_quote=1e9, quote_usd_rate=None,
               long=long_a, short=short_a, split=T0, listed=True, mark=2996.0, funding_pct=0.02)
    r = await c.futures._judge(w, sc.PROFILES["1h"], c.futures.params("1h"))
    assert r["side"] == "short" and r["signal"] == "BUY" and r["label"] == "SHORT", r["reasons"]
    assert r["strategy"]["rule"].startswith("close below the lowest low") and "Turtle" in r["strategy"]["source"]
    plan, lev = r["plan"], r["leverage"]
    assert plan["stop"] > plan["entry"] > plan["tp1"] and plan["entry_low"] < plan["entry"] < plan["entry_high"]
    assert "buy back" in plan["exit_rule"] or "trail" in plan["exit_rule"]
    assert lev["leverage"] <= c.futures.max_leverage and lev["liquidation_price"] > plan["stop"]
    assert lev["funding_pct"] < 0  # positive funding pays shorts
    async with sessions() as s:
        row = (await s.execute(select(Signal).where(Signal.symbol == "ETH", Signal.strategy == "fut_1h"))).scalar_one()
        assert row.signal == "SHORT" and row.quant_output["side"] == "short" and row.stop_loss > row.entry_high
        created = row.created_at
    # the price falls: the short trails down and closes in profit (followed on the mirror)
    m15 = Timeframe.M15
    start = created.replace(tzinfo=UTC) - timedelta(minutes=15 * 25)
    start = start - timedelta(minutes=start.minute % 15, seconds=start.second, microseconds=start.microsecond)
    candles = [bar(q, 3000, 3002, 2998, 3000, m15, start) for q in range(26)]
    candles += [bar(26 + q, 3000 - 20 * q, 3005 - 20 * q, 2975 - 20 * q, 2980 - 20 * q, m15, start) for q in range(25)]
    candles += [bar(51, 2520, 2950, 2518, 2940, m15, start)]  # a bounce through the trailing stop (about 2905)
    assert await OutcomeTracker(sessions).update("ETH", {m15: candles}) == 1
    async with sessions() as s:
        row = (await s.execute(select(Signal).where(Signal.symbol == "ETH", Signal.strategy == "fut_1h"))).scalar_one()
        assert row.status == "WIN"
    # the same setup with the price already beyond the stop is not a trade
    w.price = 3070.0
    assert (await c.futures._judge(w, sc.PROFILES["1h"], c.futures.params("1h")))["signal"] == "NO TRADE"
    # no perpetual listed: no trade
    w.price, w.listed = 2995.0, False
    assert (await c.futures._judge(w, sc.PROFILES["1h"], c.futures.params("1h")))["signal"] == "NO TRADE"


async def test_futures_scan_api_settings_and_emergency_stop(env):  # noqa: F811
    http, c, _, _ = env
    s = (await http.get("/api/futures/settings")).json()
    assert s["max_leverage"] == 5 and s["cost_pct"] == pytest.approx(0.14)
    s = (await http.put("/api/futures/settings", json={"max_leverage": 3, "fee_pct": 0.045})).json()
    assert s["max_leverage"] == 3 and s["fee_pct"] == 0.045
    assert (await http.put("/api/futures/settings", json={"max_leverage": 50})).status_code == 422
    await http.put("/api/control/selection", json={"mode": "selected", "symbols": ["BTC", "ETH", "SOL"]})
    assert (await http.post("/api/futures/scan", params={"horizon": "4h"})).json()["started"] is True
    await c.futures.wait("4h")
    body = (await http.get("/api/futures", params={"horizon": "4h"})).json()
    assert body["status"]["4h"]["outcome"] == "completed", body["status"]["4h"]
    res = body["result"]
    assert len(res["research"]) == 16 and {r["side"] for r in res["research"]} == {"long", "short"}
    assert set(res["counts"]) == {"LONG", "SHORT", "WATCH", "NO TRADE"} and len(res["signals"]) == 3
    trades = [x for x in res["signals"] if x["signal"] in ("BUY", "STRONG BUY")]
    assert (res["no_trade_reason"] is None) == bool(trades)
    for x in trades:
        assert x["leverage"]["leverage"] <= 3 and x["label"] in ("LONG", "SHORT", "STRONG LONG", "STRONG SHORT")
    assert (await http.post("/api/ai/review", json={"kind": "futures", "symbol": "DOGE", "horizon": "4h"})).status_code in (404, 503)
    await http.post("/api/control/kill")
    assert (await http.post("/api/futures/scan", params={"horizon": "1h"})).status_code == 503
    assert (await http.get("/api/futures", params={"horizon": "4h"})).status_code == 200
    assert (await http.get("/api/control/status")).json()["futures"]["4h"]["outcome"] == "completed"
    await http.post("/api/control/resume")
