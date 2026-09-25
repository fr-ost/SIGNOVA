"""Phase 10: futures data (parsers, failover), liquidation map, evidence board, learning, held-back setups."""

import json
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.analysis import evidence as ev
from app.analysis import evidence_learn as el
from app.analysis import liqmap
from app.analysis import scalp as sc
from app.core.enums import SignalLabel, Timeframe
from app.data import derivatives as d
from app.data.derivatives import Point
from app.data.health import ProviderHealthRegistry
from app.data.normalization.schemas import Candle
from app.database import create_session_factory
from app.models import Base, Signal, SignalOutcome
from app.services.derivatives import DerivativesService, DerivativesSnapshot, _by_base
from app.services.outcomes import OutcomeTracker
from tests.test_phase3_4 import settings_for
from tests.test_phase6 import T0, _signal, bar
from tests.test_phase7_9 import env  # noqa: F401  (fixture)

NOW = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def hourly(n: int, price_fn, *, taker: float | None = 0.5, end: datetime = NOW) -> list[Candle]:
    out = []
    for k in range(n):
        t = end - timedelta(hours=n - k)
        p = price_fn(k)
        out.append(Candle("fake", "XUSDT", Timeframe.H1, t, t + timedelta(hours=1), p, p * 1.004, p * 0.996, p, 1000.0,
                          1000.0 * p, None, None if taker is None else 1000.0 * taker, True))
    return out


# ----------------------------------------------------------------------------- parsers


def test_parsers_follow_the_documented_shapes():
    prem = d.binance_premium([{"symbol": "BTCUSDT", "markPrice": "60000", "lastFundingRate": "0.0001"},
                              {"symbol": "1000PEPEUSDT", "markPrice": "0.012", "lastFundingRate": "0.0006"}, "junk"])
    assert prem["BTCUSDT"].funding_pct_8h == pytest.approx(0.01)
    bases = _by_base(prem, d.binance_candidates)
    assert bases["BTC"].symbol == "BTCUSDT" and bases["PEPE"].multiplier == 1000
    assert bases["PEPE"].mark == pytest.approx(0.000012)  # per spot coin
    s = d.binance_series([{"timestamp": 1700003600000, "sumOpenInterest": "10"},
                          {"timestamp": "1700000000000", "sumOpenInterest": "9"}], "sumOpenInterest", scale=1000)
    assert [p.value for p in s] == [9000, 10000]  # sorted by time, in coins
    with pytest.raises(d.ParseError):
        d.binance_series({"code": -1121, "msg": "Invalid symbol."}, "x")
    funding = d.binance_funding([{"fundingTime": 1700000000000, "fundingRate": "-0.0003"}])
    assert funding[0].value == pytest.approx(-0.03)

    by = d.bybit_tickers({"retCode": 0, "result": {"list": [
        {"symbol": "SHIB1000USDT", "fundingRate": "0.0002", "openInterestValue": "5000000", "markPrice": "0.02"},
        {"symbol": "BTCPERP", "fundingRate": "0.0001"}]}})
    shib = _by_base(by, d.bybit_candidates)["SHIB"]
    assert shib.multiplier == 1000 and shib.open_interest_usd == 5e6 and "BTC" not in _by_base(by, d.bybit_candidates)
    with pytest.raises(d.ParseError):
        d.bybit_list({"retCode": 10001, "retMsg": "params error"})
    ratio = d.bybit_series({"retCode": 0, "result": {"list": [{"buyRatio": "0.6", "timestamp": "1700000000000"}]}}, "buyRatio")
    assert ratio[0].value == 0.6

    assert d.okx_instruments({"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "ctVal": "0.01", "state": "live"},
                                                   {"instId": "BTC-USD-SWAP", "ctVal": "100"}]}) == {"BTC-USDT-SWAP": 0.01}
    liq = d.okx_liquidations({"code": "0", "data": [{"details": [
        {"posSide": "long", "side": "sell", "sz": "10", "bkPx": "60000", "ts": "1700000000000"},
        {"posSide": "", "side": "buy", "sz": "5", "bkPx": "61000", "ts": "1700000060000"},
        {"posSide": "long", "sz": "0", "bkPx": "1", "ts": "1700000000000"}]}]}, 0.01)
    assert [x.side for x in liq] == ["long", "short"] and liq[0].usd == pytest.approx(6000.0)
    taker = d.okx_taker_ratio({"code": "0", "data": [["1700003600000", "100", "150"], ["1700000000000", "200", "100"]]})
    assert [round(p.value, 2) for p in taker] == [0.5, 1.5]
    with pytest.raises(d.ParseError):
        d.okx_data({"code": "50011", "msg": "Too Many Requests"})

    hl = d.hyperliquid_contexts([
        {"universe": [{"name": "BTC"}, {"name": "kPEPE"}, {"name": "OLD", "isDelisted": True}]},
        [{"funding": "0.0000125", "openInterest": "100", "markPx": "60000"},
         {"funding": "0.00001", "openInterest": "1000000", "markPx": "0.012"}, {"funding": "0", "openInterest": "1", "markPx": "1"}],
    ])
    assert hl["BTC"].funding_pct_8h == pytest.approx(0.01) and hl["BTC"].open_interest_usd == 6e6 and "OLD" not in hl
    assert _by_base(hl, d.hyperliquid_candidates)["PEPE"].multiplier == 1000
    with pytest.raises(d.ParseError):
        d.hyperliquid_contexts({"error": "bad"})


# ----------------------------------------------------------------------------- exchange failover


def exchanges(*, binance_status: int = 200, calls: list | None = None):
    hours = [NOW - timedelta(hours=48 - k) for k in range(49)]

    def ms(t: datetime) -> int:
        return int(t.timestamp() * 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if calls is not None:
            calls.append(f"{host}{path}")
        if host == "fapi.binance.com":
            if binance_status != 200:
                return httpx.Response(binance_status, json={"code": 0, "msg": "Service unavailable from a restricted location"})
            if path == "/fapi/v1/premiumIndex":
                return httpx.Response(200, json=[{"symbol": "BTCUSDT", "markPrice": "60000", "lastFundingRate": "0.0001"}])
            if path == "/futures/data/openInterestHist":
                return httpx.Response(200, json=[{"timestamp": ms(t), "sumOpenInterest": str(1000 + 5 * k)} for k, t in enumerate(hours)])
            if path == "/futures/data/globalLongShortAccountRatio":
                return httpx.Response(200, json=[{"timestamp": ms(t), "longAccount": "0.55"} for t in hours])
            if path == "/futures/data/topLongShortPositionRatio":
                return httpx.Response(200, json=[{"timestamp": ms(t), "longAccount": str(0.5 + 0.002 * k)} for k, t in enumerate(hours)])
            if path == "/futures/data/takerlongshortRatio":
                return httpx.Response(200, json=[{"timestamp": ms(t), "buySellRatio": "1.2"} for t in hours])
            if path == "/fapi/v1/fundingRate":
                return httpx.Response(200, json=[{"fundingTime": ms(t), "fundingRate": "0.0001"} for t in hours[::8]])
        if host == "api.bybit.com":
            if path == "/v5/market/tickers":
                return httpx.Response(200, json={"retCode": 0, "result": {"list": [
                    {"symbol": "BTCUSDT", "fundingRate": "0.0003", "openInterestValue": "9000000", "markPrice": "60000"}]}})
            if path == "/v5/market/open-interest":
                return httpx.Response(200, json={"retCode": 0, "result": {"list": [
                    {"openInterest": str(2000 + k), "timestamp": str(ms(t))} for k, t in enumerate(hours)]}})
            if path == "/v5/market/account-ratio":
                return httpx.Response(200, json={"retCode": 0, "result": {"list": [
                    {"buyRatio": "0.7", "sellRatio": "0.3", "timestamp": str(ms(t))} for t in hours]}})
            if path == "/v5/market/funding/history":
                return httpx.Response(200, json={"retCode": 0, "result": {"list": [
                    {"fundingRate": "0.0003", "fundingRateTimestamp": str(ms(t))} for t in hours[::8]]}})
        if host == "api.hyperliquid.xyz":
            body = json.loads(request.content)
            assert body == {"type": "metaAndAssetCtxs"}
            return httpx.Response(200, json=[{"universe": [{"name": "BTC"}]}, [{"funding": "0.00001", "openInterest": "10", "markPx": "60000"}]])
        if host == "www.okx.com":
            if path == "/api/v5/public/instruments":
                return httpx.Response(200, json={"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "ctVal": "0.01", "state": "live"}]})
            if path == "/api/v5/public/liquidation-orders":
                return httpx.Response(200, json={"code": "0", "data": [{"details": [
                    {"posSide": "long", "side": "sell", "sz": "100", "bkPx": "59000", "ts": str(ms(NOW - timedelta(minutes=30)))}]}]})
            if path == "/api/v5/rubik/stat/taker-volume":
                return httpx.Response(200, json={"code": "0", "data": [[str(ms(t)), "100", "80"] for t in hours]})
            if path == "/api/v5/rubik/stat/contracts/open-interest-volume":
                return httpx.Response(200, json={"code": "0", "data": [[str(ms(t)), "6000000", "1"] for t in hours]})
            if path == "/api/v5/rubik/stat/contracts/long-short-account-ratio":
                return httpx.Response(200, json={"code": "0", "data": [[str(ms(t)), "1.5"] for t in hours]})
        return httpx.Response(404, json={})

    return handler


async def test_futures_data_uses_the_first_exchange_that_answers(tmp_path):
    settings = settings_for(tmp_path, derivatives_enabled=True)
    health = ProviderHealthRegistry()
    service = DerivativesService(settings, httpx.AsyncClient(transport=httpx.MockTransport(exchanges())), health)
    snap = await service.snapshot("btc")
    assert snap.listed == ["binance", "bybit", "okx", "hyperliquid"]
    assert snap.sources == {"funding": "binance", "oi_history": "binance", "long_share": "binance",
                            "top_long_share": "binance", "taker_ratio": "binance", "funding_history": "binance",
                            "liquidations": "okx"}
    assert snap.funding_pct == pytest.approx(0.01) and snap.oi_unit == "coin" and len(snap.oi_history) == 49
    assert snap.liquidations[0].side == "long" and snap.liquidations[0].usd == pytest.approx(59000.0)
    assert snap.open_interest_usd == 9e6  # Binance's premium index has no OI; Bybit's listing does
    assert (await service.snapshot("BTC")) is snap  # cached
    missing = await service.snapshot("NOPE")
    assert not missing.available and "no USDT perpetual" in missing.errors[-1]
    market = (await service.market()).summary(["BTC"])
    assert market["coins"] == 1 and market["avg_funding_pct"] == pytest.approx(0.01)


async def test_futures_data_falls_back_when_binance_refuses_the_region(tmp_path):
    settings = settings_for(tmp_path, derivatives_enabled=True)
    calls: list[str] = []
    health = ProviderHealthRegistry()
    service = DerivativesService(settings, httpx.AsyncClient(transport=httpx.MockTransport(exchanges(binance_status=451, calls=calls))), health)
    snap = await service.snapshot("BTC")
    assert snap.listed == ["bybit", "okx", "hyperliquid"]
    assert snap.sources["funding"] == "bybit" and snap.funding_pct == pytest.approx(0.03)
    assert snap.sources["oi_history"] == "bybit" and snap.sources["long_share"] == "bybit"
    assert snap.long_share[-1].value == 0.7 and snap.sources["taker_ratio"] == "okx"
    assert snap.taker_ratio[-1].value == pytest.approx(0.8) and "top_long_share" not in snap.sources
    assert any("HTTP 451" in e for e in snap.errors)
    assert health.get("binance_futures").status.value == "RESTRICTED"
    before = sum(1 for c in calls if c.startswith("fapi.binance.com"))
    await service.market(force=True)
    await service.snapshot("BTC", force=True)
    assert sum(1 for c in calls if c.startswith("fapi.binance.com")) == before  # skipped for an hour


async def test_okx_only_history_is_converted_from_usd(tmp_path):
    settings = settings_for(tmp_path, derivatives_enabled=True)
    base = exchanges(binance_status=451)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.bybit.com":
            return httpx.Response(403, json={})
        return base(request)

    service = DerivativesService(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)), ProviderHealthRegistry())
    snap = await service.snapshot("BTC")
    assert snap.sources["oi_history"] == "okx" and snap.oi_unit == "usd"
    assert snap.long_share[-1].value == pytest.approx(0.6)  # OKX publishes longs/shorts = 1.5
    assert snap.sources["funding"] == "hyperliquid" and snap.funding_pct == pytest.approx(0.008)
    h1 = hourly(60, lambda k: 60000.0)
    coins = ev.oi_in_coins(snap, h1)
    assert coins and coins[-1].value == pytest.approx(100.0)  # $6M / $60,000


# ----------------------------------------------------------------------------- liquidation map


def test_liquidation_map_estimates_levels_and_drops_crossed_ones():
    candles = hourly(60, lambda k: 100.0)
    oi = [Point(c.close_time, 1000 + 10 * k) for k, c in enumerate(candles[5:55])]
    m = liqmap.build(oi, candles, 100.0)
    assert m is not None and "estimated" in m.method and m.notes == ["long/short split unknown: assumed 50/50"]
    assert m.usd_between(90, 91, "long") > 0 and m.usd_between(109, 110, "short") > 0
    assert m.long_total_usd == pytest.approx(m.short_total_usd, rel=0.01)
    assert m.clusters("long")[0].mid < 100 < m.clusters("short")[0].mid
    wick = candles + [Candle("fake", "XUSDT", Timeframe.H1, NOW, NOW + timedelta(hours=1), 100, 100.2, 90.0, 99, 10.0)]
    m2 = liqmap.build(oi, wick, 100.0)
    assert m2.usd_between(90, 100, "long") == 0  # a wick to 90 liquidated those longs
    assert m2.usd_between(100, 112, "short") == pytest.approx(m.usd_between(100, 112, "short"))
    crowd = liqmap.build(oi, candles, 100.0, long_share=[Point(candles[0].open_time, 0.8)])
    assert crowd.long_total_usd > 3 * crowd.short_total_usd
    falling = [Point(p.time, 2000 - 10 * k) for k, p in enumerate(oi)]
    assert liqmap.build(falling, candles, 100.0).long_total_usd == 0  # nothing new opened
    assert liqmap.build(oi[:10], candles, 100.0) is None


# ----------------------------------------------------------------------------- evidence board


def snapshot(**kw) -> DerivativesSnapshot:
    hours = [NOW - timedelta(hours=48 - k) for k in range(49)]
    base = dict(
        symbol="SOL", fetched_at=NOW, listed=["binance"],
        sources={"funding": "binance", "oi_history": "binance", "long_share": "binance", "top_long_share": "binance",
                 "taker_ratio": "binance"},
        funding_pct=0.01, oi_history=[Point(t, 1000.0) for t in hours], long_share=[Point(t, 0.5) for t in hours],
        top_long_share=[Point(t, 0.5) for t in hours], taker_ratio=[Point(t, 1.0) for t in hours],
    )
    base.update(kw)
    return DerivativesSnapshot(**base)


def board(**kw) -> ev.Evidence:
    h1 = kw.pop("h1", hourly(200, lambda k: 100.0 * (1 + 0.0008 * k)))
    return ev.build_evidence(ev.EvidenceInputs(symbol=kw.pop("symbol", "SOL"), now=NOW, price=h1[-1].close, h1=h1, **kw))


def test_crowded_leveraged_longs_are_vetoed():
    hours = [NOW - timedelta(hours=48 - k) for k in range(49)]
    crowded = snapshot(
        funding_pct=0.12, oi_history=[Point(t, 1000.0 * (1.01 ** k)) for k, t in enumerate(hours)],
        long_share=[Point(t, 0.76) for t in hours], top_long_share=[Point(t, 0.6 - 0.004 * k) for k, t in enumerate(hours)],
        taker_ratio=[Point(t, 0.8) for t in hours],
    )
    b = board(deriv=crowded)
    keys = {f.key: f for f in b.factors}
    assert keys["funding"].veto and keys["funding"].direction == -1
    assert keys["crowd"].direction == -1 and keys["whales"].direction == -1 and keys["futures_flow"].direction == -1
    assert b.grade == "strong_against" and b.score is not None and b.score <= ev.STRONG_AGAINST
    assert ev.apply_to_label(SignalLabel.BUY, b, "filter")[0] == SignalLabel.WATCH
    assert ev.apply_to_label(SignalLabel.BUY, b, "advisory") == (SignalLabel.BUY, [])
    assert b.features()["crowd"] < 0 and set(b.features()) == set(ev.PRIOR_WEIGHTS)
    assert "against" in b.summary()


def test_supportive_positioning_keeps_the_signal():
    hours = [NOW - timedelta(hours=48 - k) for k in range(49)]
    good = snapshot(
        oi_history=[Point(t, 1000.0 * (1.002 ** k)) for k, t in enumerate(hours)],
        long_share=[Point(t, 0.42) for t in hours], top_long_share=[Point(t, 0.5 + 0.002 * k) for k, t in enumerate(hours)],
        taker_ratio=[Point(t, 1.2) for t in hours],
    )
    h1 = hourly(200, lambda k: 100.0 * (1 + 0.0008 * k), taker=0.6)
    b = board(deriv=good, h1=h1)
    keys = {f.key: f for f in b.factors}
    assert keys["oi_trend"].direction == 1 and keys["whales"].direction == 1 and keys["spot_flow"].direction == 1
    assert keys["cvd"].direction == 1 and b.score is not None and b.score >= ev.SUPPORTIVE and not b.vetoes
    assert ev.apply_to_label(SignalLabel.STRONG_BUY, b, "filter")[0] == SignalLabel.STRONG_BUY


def test_news_hype_events_and_thin_boards():
    hack = [(NOW - timedelta(hours=2), "Solana DeFi protocol hacked for $50M", "negative")]
    b = board(headlines=hack)
    assert b.vetoes and "critical news" in b.vetoes[0]
    assert ev.apply_to_label(SignalLabel.BUY, b, "filter")[0] == SignalLabel.WATCH  # a veto acts even on a thin board
    cleared = board(headlines=hack, ai_news={"impact": 0, "critical": False, "reason": "a different protocol"})
    assert not cleared.vetoes and {f.key: f for f in cleared.factors}["news_critical"].strength == 0.5
    listing = board(headlines=[(NOW - timedelta(hours=3), "Coinbase lists SOL perpetuals", "neutral")])
    assert {f.key: f for f in listing.factors}["catalyst"].direction == 1
    hype = board(mentions_24h=12, mentions_per_day=2.0, rsi=78.0, change_24h_pct=15.0)
    assert {f.key: f for f in hype.factors}["hype"].direction == -1
    early = board(mentions_24h=12, mentions_per_day=2.0, rsi=55.0, change_24h_pct=2.0)
    assert {f.key: f for f in early.factors}["hype"].direction == 1
    unlock = board(unlock={"days": 2, "pct": 3.0, "usd": 5e7})
    assert unlock.vetoes and "unlock" in unlock.vetoes[0]
    thin = board(book_imbalance=-0.6)
    assert thin.thin and thin.grade == "thin" and ev.apply_to_label(SignalLabel.BUY, thin, "filter")[0] == SignalLabel.BUY


def test_liquidation_zone_under_the_stop_gives_a_hint():
    h1 = hourly(80, lambda k: 100.0)
    oi = [Point(c.close_time, 1000.0 + 20 * k) for k, c in enumerate(h1[10:70])]
    deriv = snapshot(oi_history=oi, long_share=[], sources={"oi_history": "binance"}, funding_pct=None,
                     top_long_share=[], taker_ratio=[])
    b = board(deriv=deriv, h1=h1, entry=100.0, stop=91.0, tp1=109.0, tp2=118.0)
    assert b.liq_map is not None and b.liq_map["bands"]
    assert b.stop_hint is not None and b.stop_hint < 90.5
    assert any("estimated long liquidations" in n for n in b.notes)
    assert any("short liquidations" in n for n in b.notes)


def test_a_liquidation_cascade_in_progress_is_vetoed():
    from app.data.derivatives import Liquidation

    falling = [Candle("fake", "XUSDT", Timeframe.H1, c.open_time, c.close_time, c.close + 0.3, c.close + 0.35,
                      c.close - 0.05, c.close, c.volume) for c in hourly(60, lambda k: 100.0 - 0.3 * k)]
    cascade = snapshot(sources={"liquidations": "okx"}, open_interest_usd=50e6, funding_pct=None,
                       liquidations=[Liquidation(NOW - timedelta(minutes=20), "long", 83.0, 900_000.0)])
    b = board(deriv=cascade, h1=falling)
    assert b.vetoes and "cascade" in b.vetoes[0]
    flushed = snapshot(sources={"liquidations": "okx"}, open_interest_usd=50e6, funding_pct=None,
                       liquidations=[Liquidation(NOW - timedelta(hours=3), "long", 83.0, 900_000.0)])
    rebound = hourly(60, lambda k: 100.0 - 0.3 * k if k < 57 else 84.0 + (k - 56))
    assert {f.key: f for f in board(deriv=flushed, h1=rebound).factors}["liquidations"].direction == 1


# ----------------------------------------------------------------------------- learning


def samples(n: int = 140, seed: int = 3) -> list[el.Sample]:
    rng = random.Random(seed)
    out = []
    for k in range(n):
        win = rng.random() < 0.5
        whales = (0.8 if win else -0.8) if rng.random() < 0.8 else rng.choice([0.8, -0.8])
        features = {key: rng.uniform(-0.2, 0.2) for key in ev.PRIOR_WEIGHTS} | {"whales": whales}
        out.append(el.Sample(T0 + timedelta(hours=k), "1h", features, "neutral", 0.0, 1.2 if win else -1.0, k % 5 != 0))
    return out


def test_learning_finds_the_factor_that_predicts_outcomes():
    data = samples()
    table = {row["key"]: row for row in el.factor_table(data)}
    assert table["whales"]["verdict"] == "helps" and table["whales"]["edge_r"] > 1.0
    model = el.train(data, T0)
    assert model is not None and model.validated, model.reasons
    top = model.metrics["top_features"][0]
    assert top["name"] == "whales" and top["weight"] > 0
    assert el.train(data[:50], T0) is None  # not enough closed setups yet
    noise = [el.Sample(s.time, s.horizon, {k: random.Random(i).uniform(-1, 1) for k in ev.PRIOR_WEIGHTS}, s.grade, 0.0,
                       s.r_multiple, True) for i, s in enumerate(data)]
    assert not el.train(noise, T0).validated  # random factors never validate


async def test_learning_service_reads_outcomes_and_gates(tmp_path):
    from app.services.learning import LearningService
    from app.services.settings_store import SettingsStore

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = create_session_factory(engine)
    async with sessions() as s:
        for k, sample in enumerate(samples()):
            sig = Signal(symbol="ETH", timeframe="15m", strategy="scalp_1h", signal="BUY" if sample.shown else "WATCH",
                         signal_score=60, data_health_score=100, data_state="HEALTHY", entry_low=100, entry_high=100,
                         stop_loss=99, status=("WIN" if sample.r_multiple > 0 else "LOSS") if sample.shown else "FILTERED_WIN",
                         reasons=[], risks=[], input_features={"evidence": {"score": 0.0, "grade": "neutral", "features": sample.features}},
                         quant_output={"filtered_by": None if sample.shown else "evidence"},
                         created_at=datetime.now(UTC) - timedelta(days=30) + timedelta(hours=k))
            s.add(sig)
            await s.flush()
            s.add(SignalOutcome(signal_id=sig.id, outcome="TARGETS", r_multiple=sample.r_multiple, evaluated_at=datetime.now(UTC)))
        await s.commit()
    store = SettingsStore(sessions)
    learning = LearningService(sessions, store)
    status = await learning.refresh(force=True)
    assert status["samples"] == 140 and status["held_back"] == 28 and status["model"]["validated"]
    good = board(deriv=None)
    good.factors.append(ev.Factor("whales", "derivatives", "Top traders (whales)", 1, 0.8, 1.0, "", "", ""))
    bad = board(deriv=None)
    bad.factors.append(ev.Factor("whales", "derivatives", "Top traders (whales)", -1, 0.8, 1.0, "", "", ""))
    assert learning.gate(SignalLabel.BUY, good, "1h")[0] == SignalLabel.BUY
    capped, why = learning.gate(SignalLabel.BUY, bad, "1h")
    assert capped == SignalLabel.WATCH and "learned evidence model" in why[0]
    await learning.set_enabled(False)
    assert learning.gate(SignalLabel.BUY, bad, "1h")[0] == SignalLabel.BUY
    reloaded = LearningService(sessions, SettingsStore(sessions))
    await reloaded.load()
    assert reloaded.model is not None and reloaded.model.validated and not reloaded.enabled
    await engine.dispose()


# ----------------------------------------------------------------------------- held-back setups


async def test_held_back_setups_are_tracked_in_their_own_lane(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = create_session_factory(engine)
    tracker = OutcomeTracker(sessions)
    m15 = Timeframe.M15
    shown = await _signal(sessions, created=T0)
    held = await _signal(sessions, created=T0 + timedelta(minutes=5), label="WATCH")
    async with sessions() as s:
        row = await s.get(Signal, held)
        row.status = "FILTERED"
        row.quant_output = {"max_hold": 8, "filtered_by": "evidence", "would_be": "BUY"}
        row.input_features = {"evidence": {"grade": "strong_against", "score": -40, "features": {}}}
        shown_row = await s.get(Signal, shown)
        shown_row.input_features = {"evidence": {"grade": "for", "score": 20, "features": {}}}
        await s.commit()
    candles = [bar(0, 100, 100.4, 99.6, 100.2, m15), bar(1, 100.2, 101.2, 100.0, 101.0, m15),
               bar(2, 101.0, 102.3, 100.8, 102.0, m15)] + [bar(k, 102, 102.2, 101.8, 102, m15) for k in range(3, 12)]
    assert await tracker.update("ETH", {m15: candles}) == 2  # the held-back one does not wait for (or skip) the shown one
    async with sessions() as s:
        assert (await s.get(Signal, shown)).status == "WIN" and (await s.get(Signal, held)).status == "FILTERED_WIN"
    perf = await tracker.performance(days=3650)
    assert perf["strategies"][0]["closed"] == 1  # held-back setups never count in the strategy's record
    assert perf["held_back"][0]["filtered_by"] == "evidence" and perf["held_back"][0]["closed"] == 1
    assert perf["by_evidence"] == [{"grade": "for", "closed": 1, "win_rate": 100.0, "avg_r": perf["recent"][0]["r_multiple"],
                                    "total_r": perf["recent"][0]["r_multiple"]}]
    assert len(perf["recent"]) == 1
    await engine.dispose()


# ----------------------------------------------------------------------------- integration


async def test_scalp_setup_held_back_by_a_veto_is_stored_as_filtered(env):  # noqa: F811
    http, c, sessions, _ = env
    from app.services.scalp import _Work
    from tests.test_phase6 import _stats
    from tests.test_signal_engine import book

    asset = (await c.universe.get()).find("ETH")
    now = datetime.now(UTC)
    cand = sc.Candidate("pullback", 100, now - timedelta(minutes=5), 3000.0, 2970.0, 3030.0, 3060.0, 1.0, 20.0, ["setup"])
    vetoed = board(symbol="ETH", unlock={"days": 1, "pct": 4.0})
    work = _Work(asset, _stats([0.6, -1.0, 1.4, 0.5] * 12), cand, [], True, 3003.0, "USDT", True, [], book(3000.0), 1e9,
                 1.0, evidence=vetoed)
    r = await c.scalp._judge(work, sc.PROFILES["1h"], c.scalp.params("1h"), None)
    assert r.signal == SignalLabel.WATCH and r.filtered_by == "evidence" and r.would_be in ("BUY", "STRONG BUY")
    assert r.status == "held back by a filter" and r.reasons[0].startswith("evidence veto")
    assert r.board is not None and r.board["vetoes"]
    async with sessions() as s:
        row = (await s.execute(select(Signal).where(Signal.symbol == "ETH", Signal.strategy == "scalp_1h"))).scalar_one()
        assert row.status == "FILTERED" and row.signal == "WATCH" and row.quant_output["filtered_by"] == "evidence"
        assert row.input_features["evidence"]["vetoes"] and "unlock" in row.input_features["evidence"]["features"]
    await c.evidence.update(mode="advisory")
    work.evidence = vetoed
    shown = await c.scalp._judge(work, sc.PROFILES["1h"], c.scalp.params("1h"), None)
    assert shown.signal in (SignalLabel.BUY, SignalLabel.STRONG_BUY) and shown.filtered_by is None


async def test_swing_signal_held_back_by_critical_news(env):  # noqa: F811
    http, c, sessions, _ = env
    from app.services.analysis import AnalysisService
    from app.services.news import NewsEntry
    from tests.test_signal_engine import run

    result = run()
    assert result.signal == SignalLabel.STRONG_BUY
    news = SimpleNamespace(items=[NewsEntry("ETH bridge exploited, withdrawals paused", "https://x", "feed", NOW - timedelta(hours=1),
                                            "", ["ETH"], "negative")], trending=[], fetched_at=NOW)
    c.news._cache._values[("news")] = (0.0, 1e18, news)  # a cached digest (no fetch)
    c.evidence.mode = "filter"
    service: AnalysisService = c.analysis
    collection = SimpleNamespace(closed={}, ticker=None, book=None, quote_usd_rate=1.0)
    await service._apply_evidence(result, collection, result.market)
    assert result.signal == SignalLabel.WATCH and result.filtered_by == "evidence" and result.would_be == "STRONG BUY"
    assert result.reasons[0].startswith("evidence veto: critical news") and result.evidence["vetoes"]
    assert result.plan is not None and not result.plan.actionable
    assert await service._persist(result) == "ok"
    async with sessions() as s:
        row = (await s.execute(select(Signal).where(Signal.symbol == result.symbol))).scalars().all()[-1]
        assert row.status == "FILTERED" and row.quant_output["would_be"] == "STRONG BUY"


async def test_evidence_api_and_emergency_stop(env):  # noqa: F811
    http, c, _, _ = env
    s = (await http.get("/api/evidence/settings")).json()
    assert s["mode"] == "filter" and s["derivatives"] is False and "funding" in s["weights"]
    s = (await http.put("/api/evidence/settings", json={"mode": "advisory", "refresh_news": True})).json()
    assert s["mode"] == "advisory" and s["refresh_news"] is True
    assert (await http.put("/api/evidence/settings", json={"mode": "loud"})).status_code == 422
    learning = (await http.get("/api/evidence/learning")).json()
    assert learning["samples"] == 0 and learning["needed"] == 90 and learning["model"] is None
    assert (await http.post("/api/evidence/learning/refresh")).status_code == 200
    assert (await http.put("/api/evidence/learning", json={"enabled": False})).json()["enabled"] is False
    assert (await http.get("/api/evidence/ETH")).status_code == 404
    await c.evidence.for_coin("ETH", horizon="1h", price=3000.0, market=None, h1=hourly(200, lambda k: 3000.0))
    body = (await http.get("/api/evidence/ETH", params={"horizon": "1h"})).json()
    assert body["symbol"] == "ETH" and body["horizon"] == "1h" and "summary" in body
    assert (await http.get("/api/derivatives/market")).status_code == 503  # switched off in tests
    await http.post("/api/control/kill")
    assert (await http.get("/api/evidence/settings")).status_code == 200
    assert (await http.get("/api/derivatives/BTC")).status_code == 503
    assert (await http.get("/api/derivatives/BTC")).json().get("emergency_stop") is True
    await http.post("/api/control/resume")


@pytest.mark.skipif(not __import__("os").getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
async def test_postgres_held_back_lane_and_learning():
    import os

    from app.config import Settings
    from app.services.learning import LearningService
    from app.services.settings_store import SettingsStore

    settings = Settings(_env_file=None, database_url=os.environ["TEST_DATABASE_URL"], json_logs=False)
    engine = create_async_engine(settings.async_database_url, connect_args=settings.database_connect_args)

    async def cleanup():
        async with engine.begin() as conn:
            await conn.exec_driver_sql("DELETE FROM signals WHERE symbol = 'PGE'")
            await conn.exec_driver_sql("DELETE FROM app_settings WHERE key LIKE 'evidence_%'")

    await cleanup()
    sessions = create_session_factory(engine)
    tracker = OutcomeTracker(sessions)
    m15 = Timeframe.M15
    start = datetime.now(tz=UTC).replace(second=0, microsecond=0) - timedelta(hours=10)
    start = start - timedelta(minutes=start.minute % 15)
    shown = await _signal(sessions, symbol="PGE", created=start)
    held = await _signal(sessions, symbol="PGE", created=start + timedelta(minutes=1), label="WATCH")
    async with sessions() as s:
        row = await s.get(Signal, held)
        row.status = "FILTERED"
        row.quant_output = {"max_hold": 8, "filtered_by": "evidence", "would_be": "BUY"}
        row.input_features = {"evidence": {"grade": "against", "score": -12.5, "features": {"whales": -0.6}}}
        await s.commit()
    candles = [bar(k, 100, 100.4, 99.6, 100.2, m15, start) for k in range(1)] + [
        bar(1, 100.2, 101.2, 100.0, 101.0, m15, start), bar(2, 101.0, 102.3, 100.8, 102.0, m15, start)]
    assert await tracker.update("PGE", {m15: candles}) == 2
    async with sessions() as s:
        assert (await s.get(Signal, shown)).status == "WIN" and (await s.get(Signal, held)).status == "FILTERED_WIN"
    perf = await tracker.performance(days=2)
    assert any(r["filtered_by"] == "evidence" for r in perf["held_back"])
    status = await LearningService(sessions, SettingsStore(sessions)).refresh(force=True)
    assert status["samples"] >= 1 and status["held_back"] >= 1
    await cleanup()
    await engine.dispose()
