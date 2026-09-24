"""Phase 2 endpoints with fake providers and an SQLite database."""

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.enums import SignalLabel
from app.models import Base, MarketRegime, Signal, SignalTarget, TechnicalFeature
from app.services.container import build_container
from tests.conftest import (
    FakeAltcoinSeasonAdapter,
    FakeFearGreedAdapter,
    FakeGlobalAdapter,
    FakeListingAdapter,
    FakeReferenceAdapter,
    FakeSpotAdapter,
)
from tests.test_api import _make_client

LABELS = {label.value for label in SignalLabel}


@pytest.fixture
async def env(test_settings):
    engine = create_async_engine(test_settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app, sessions = await _make_client(test_settings, engine)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            yield http, sessions, app.state.container
    await engine.dispose()


async def run_scan(http, container):
    assert (await http.post("/api/control/analyze")).json()["started"] is True
    await container.controller.wait()
    return await http.get("/api/signals")


async def count(sessions, model, *where):
    async with sessions() as s:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return await s.scalar(stmt)


async def test_market_regime_endpoint_and_persistence(env):
    http, sessions, _ = env
    r = await http.get("/api/market/regime")
    assert r.status_code == 200
    body = r.json()
    assert body["regime"] in {"BULL", "BEAR", "NEUTRAL", "UNKNOWN"}
    assert body["max_signal"] in LABELS
    assert body["breadth_sample"] >= 8 and body["btc_ema200"] is not None
    assert body["fear_greed"]["value"] == 62
    assert await count(sessions, MarketRegime) == 1
    await http.get("/api/market/regime")  # cached, and persisted at most hourly
    assert await count(sessions, MarketRegime) == 1


async def test_asset_analysis_runs_the_full_pipeline_and_persists(env):
    http, sessions, _ = env
    r = await http.get("/api/assets/btc/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "BTC" and body["signal"] in LABELS and 0 <= body["score"] <= 100
    stages = [s["stage"] for s in body["pipeline"]]
    assert stages[:6] == [
        "DATA_HEALTH_CHECK", "MARKET_DATA_CHECK", "TIMESTAMP_CHECK", "CANDLE_COMPLETENESS_CHECK",
        "SOURCE_CONSISTENCY_CHECK", "VOLATILITY_CHECK",
    ]
    assert stages[6:] == ["ANALYSIS_CHECK", "RISK_CHECK", "FINAL_VALIDATION"]
    assert [i["label"] for i in body["indicators"]] == ["5m", "15m", "1H", "4H", "1D"]
    assert {s["label"] for s in body["structure"]} == {"1H", "4H", "1D"}
    assert len(body["factors"]) == 6 and body["risk_checks"]
    assert body["setup_timeframe"] == "4H" and body["engine_version"].startswith("quant-")
    assert body["disclaimer"] and body["summary"].startswith(body["signal"])
    if body["signal"] in ("BUY", "STRONG BUY"):
        assert body["plan"]["actionable"] is True
    assert body["persistence"] == "ok"
    assert await count(sessions, TechnicalFeature, TechnicalFeature.symbol == "BTC") == 3  # 1H, 4H, 1D
    assert await count(sessions, Signal, Signal.symbol == "BTC") == 1


async def test_signal_rows_are_deduplicated_per_label_and_setup_candle(env):
    http, sessions, container = env
    await http.get("/api/assets/ETH/analysis")
    container.analysis._cache.invalidate()
    container.assets._collections.invalidate()
    await http.get("/api/assets/ETH/analysis")  # same label, same 4H candle
    assert await count(sessions, Signal, Signal.symbol == "ETH") == 1
    container.analysis._last_signal.clear()  # as after a restart: falls back to the database
    container.analysis._cache.invalidate()
    await http.get("/api/assets/ETH/analysis")
    assert await count(sessions, Signal, Signal.symbol == "ETH") == 1
    assert await count(sessions, TechnicalFeature, TechnicalFeature.symbol == "ETH") == 3


async def test_price_conflict_and_unsupported_assets_are_no_trade(test_settings):
    engine = create_async_engine(test_settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    spot = [FakeSpotAdapter("fakeex", price_multiplier={"SOL": 1.03})]
    app, _ = await _make_client(test_settings, engine, spot=spot)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            sol = (await http.get("/api/assets/SOL/analysis")).json()
            assert sol["signal"] == "NO TRADE" and sol["plan"] is None
            assert sol["integrity_passed"] is False and sol["reasons"][0].startswith("data integrity:")
            leo = (await http.get("/api/assets/LEO/analysis")).json()
            assert leo["signal"] == "NO TRADE" and "no USDT/USD spot pair" in leo["reasons"][0]
            assert (await http.get("/api/assets/NOPE/analysis")).status_code == 404
    await engine.dispose()


async def test_signal_scan_covers_the_universe_and_history_reads_it_back(env):
    http, sessions, container = env
    assert (await http.get("/api/signals")).json() is None  # nothing runs until asked
    r = await run_scan(http, container)
    assert r.status_code == 200
    body = r.json()
    assert len(body["signals"]) == 20 and not body["errors"]
    assert sum(body["counts"].values()) == 20 and set(body["counts"]) == LABELS
    ranks = [SignalLabel(s["signal"]).rank for s in body["signals"]]
    assert ranks == sorted(ranks, reverse=True)
    assert body["market_regime"]["regime"] in {"BULL", "BEAR", "NEUTRAL", "UNKNOWN"}
    for row in body["signals"]:
        if row["signal"] in ("BUY", "STRONG BUY"):
            assert row["suggested_allocation_pct"] is not None and row["stop_loss"] < row["entry_low"]
        else:
            assert row["suggested_allocation_pct"] is None
    assert await count(sessions, Signal) == 20

    history = (await http.get("/api/signals/history", params={"limit": 5})).json()
    assert history["persistence"] == "ok" and len(history["signals"]) == 5
    eth = (await http.get("/api/signals/history", params={"symbol": "eth"})).json()
    assert eth["symbol"] == "ETH" and [s["symbol"] for s in eth["signals"]] == ["ETH"]
    for stored in history["signals"]:
        if stored["status"] == "OPEN":
            assert {t["kind"] for t in stored["targets"]} == {"TP", "SL"}
    assert (await http.get("/api/signals/history", params={"limit": 0})).status_code == 422
    assert (await http.get("/api/signals/history", params={"symbol": "b@d"})).status_code == 422


async def test_targets_are_stored_for_signals_with_a_plan(env):
    http, sessions, container = env
    await run_scan(http, container)
    async with sessions() as s:
        with_plan = (await s.execute(select(Signal).where(Signal.entry_low.is_not(None)))).scalars().all()
        for signal in with_plan:
            kinds = (await s.execute(select(SignalTarget.kind).where(SignalTarget.signal_id == signal.id))).scalars().all()
            assert sorted(kinds) == ["SL", "TP", "TP", "TP"]


async def test_analysis_without_database_reports_disabled(test_settings):
    c = build_container(
        test_settings,
        use_database=False,
        spot_adapters=[FakeSpotAdapter("fakeex")],
        listing_adapters=[FakeListingAdapter("fakelist")],
        global_adapters=[FakeGlobalAdapter()],
        fear_greed_adapters=[FakeFearGreedAdapter()],
        altcoin_season_adapters=[FakeAltcoinSeasonAdapter()],
        reference_candle_adapter=FakeReferenceAdapter(),
    )
    result = await c.analysis.analyze("BTC")
    assert result.persistence == "disabled"
    history = await c.analysis.history(None, 10)
    assert history.persistence == "disabled" and history.signals == []
    await c.aclose()
