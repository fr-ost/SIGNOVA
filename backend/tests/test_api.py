import os

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.database import create_session_factory
from app.main import create_app
from app.core.enums import Timeframe
from app.models import (
    Asset,
    Base,
    Candle,
    MarketRegime,
    MarketSnapshot,
    OrderbookSnapshot,
    ProviderHealthRecord,
    Signal,
    SignalTarget,
    SystemEvent,
    TechnicalFeature,
)
from app.services.container import build_container
from tests.conftest import (
    FakeAltcoinSeasonAdapter,
    FakeFearGreedAdapter,
    FakeGlobalAdapter,
    FakeListingAdapter,
    FakeReferenceAdapter,
    FakeSpotAdapter,
)


async def _make_client(settings: Settings, engine, spot=None):
    session_factory = create_session_factory(engine)

    def factory(s: Settings):
        c = build_container(
            s,
            engine=engine,
            session_factory=session_factory,
            spot_adapters=spot or [FakeSpotAdapter("fakeex")],
            listing_adapters=[FakeListingAdapter("fakelist")],
            global_adapters=[FakeGlobalAdapter()],
            fear_greed_adapters=[FakeFearGreedAdapter()],
            altcoin_season_adapters=[FakeAltcoinSeasonAdapter()],
            reference_candle_adapter=FakeReferenceAdapter(),
        )
        c.engine = engine
        return c

    app = create_app(settings, container_factory=factory)
    return app, session_factory


@pytest.fixture
async def sqlite_env(test_settings):
    engine = create_async_engine(test_settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield test_settings, engine
    await engine.dispose()


@pytest.fixture
async def client(sqlite_env):
    settings, engine = sqlite_env
    app, sessions = await _make_client(settings, engine)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            yield http, sessions


async def test_health_reports_database_and_no_secret(client):
    http, _ = client
    r = await http.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["database"]["ok"] is True
    assert body["processing_state"] == "IDLE"
    assert body["openai"]["availability_check"] == "not_run"
    assert "sk-" not in r.text
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_market_endpoint_and_persistence(client):
    http, sessions = client
    r = await http.get("/api/market")
    assert r.status_code == 200
    body = r.json()
    assert len(body["assets"]) == 20
    assert body["persistence"] == "ok"
    assert body["universe"]["listing_source"] == "fakelist"
    assert {e["symbol"] for e in body["universe"]["excluded"]} == {"USDT", "USDC", "WBTC"}
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(MarketSnapshot)) == 20
        assert await s.scalar(select(func.count()).select_from(Asset).where(Asset.in_universe)) == 20
        assert await s.scalar(select(func.count()).select_from(SystemEvent)) == 1
    state = (await http.get("/api/system/state")).json()
    assert state["data_state"] == body["data_state"]


async def test_assets_list_and_detail_and_candle_storage(client):
    http, sessions = client
    listing = (await http.get("/api/assets")).json()
    assert listing["assets"][0]["symbol"] == "BTC"
    detail = await http.get("/api/assets/eth")
    assert detail.status_code == 200
    body = detail.json()
    assert body["integrity"]["decision"] == "PASS"
    assert body["persistence"] == "ok"
    async with sessions() as s:
        stored = await s.scalar(select(func.count()).select_from(Candle).where(Candle.base_asset == "ETH"))
        assert stored == 5 * 300
        assert await s.scalar(select(func.count()).select_from(OrderbookSnapshot)) == 1
    # upsert: a second (forced) load does not duplicate rows
    await http.get("/api/assets/eth/candles", params={"timeframe": "1H", "limit": 300})
    async with sessions() as s:
        stored_again = await s.scalar(select(func.count()).select_from(Candle).where(Candle.base_asset == "ETH"))
    assert stored_again == stored


async def test_candles_endpoint_validation(client):
    http, _ = client
    ok = await http.get("/api/assets/BTC/candles", params={"timeframe": "4H", "limit": 250})
    assert ok.status_code == 200 and ok.json()["label"] == "4H" and len(ok.json()["candles"]) == 250
    bad_tf = await http.get("/api/assets/BTC/candles", params={"timeframe": "2h"})
    assert bad_tf.status_code == 422
    bad_limit = await http.get("/api/assets/BTC/candles", params={"limit": 5000})
    assert bad_limit.status_code == 422


async def test_error_mapping(client):
    http, _ = client
    assert (await http.get("/api/assets/DOESNOTEXIST")).status_code == 404
    assert (await http.get("/api/assets/bad-symbol!")).status_code in (404, 422)
    unsupported = await http.get("/api/assets/LEO/candles")
    assert unsupported.status_code == 409 and unsupported.json()["data_state"] == "API_FAILURE"
    leo = (await http.get("/api/assets/LEO")).json()
    assert leo["integrity"]["decision"] == "NO TRADE"


async def test_provider_health_endpoint(client):
    http, _ = client
    await http.get("/api/market")
    r = (await http.get("/api/provider-health")).json()
    assert "providers" in r and r["live_stream"]["running"] is False


async def test_dashboard_page_and_assets(client):
    http, _ = client
    r = await http.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "__VERSION__" not in r.text
    assert "/static/dashboard.js?v=" in r.text and "/static/dashboard.css?v=" in r.text
    assert (await http.head("/")).status_code == 200
    js = await http.get("/static/dashboard.js")
    assert js.status_code == 200 and "/api/market" in js.text
    assert (await http.get("/static/dashboard.css")).status_code == 200
    assert (await http.get("/static/missing.js")).status_code == 404


async def test_api_index(client):
    http, _ = client
    body = (await http.get("/api")).json()
    assert body["dashboard"] == "/" and body["health"] == "/health" and "/api/market" in body["endpoints"]


async def test_all_exchanges_down_returns_explicit_api_failure(sqlite_env):
    settings, engine = sqlite_env
    app, _ = await _make_client(settings, engine, spot=[FakeSpotAdapter("a", fail=True)])
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            market = (await http.get("/api/market")).json()
            assert market["data_state"] == "API_FAILURE"
            assert all(a["price"] is None for a in market["assets"])


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
async def test_postgres_end_to_end(tmp_path):
    """Runs against a real PostgreSQL migrated with `alembic upgrade head`."""
    settings = Settings(_env_file=None, database_url=os.environ["TEST_DATABASE_URL"], json_logs=False)
    engine = create_async_engine(settings.async_database_url, connect_args=settings.database_connect_args)
    async with engine.begin() as conn:
        for table in ("candles", "market_snapshots", "orderbook_snapshots", "system_events", "provider_health", "assets",
                      "signal_targets", "signals", "technical_features", "market_regimes"):
            await conn.exec_driver_sql(f"DELETE FROM {table}")
    app, sessions = await _make_client(settings, engine)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            assert (await http.get("/health")).json()["database"]["ok"] is True
            assert (await http.get("/api/market")).json()["persistence"] == "ok"
            assert (await http.get("/api/assets/SOL")).json()["persistence"] == "ok"
            # upsert on the real unique constraint
            assert (await http.get("/api/assets/SOL/candles", params={"timeframe": "5m"})).status_code == 200
            # Phase 2: JSONB features/signals, target foreign keys, feature upsert on its unique key
            assert (await http.get("/api/assets/SOL/analysis")).json()["persistence"] == "ok"
            scan = (await http.get("/api/signals")).json()
            assert len(scan["signals"]) == 20 and not scan["errors"]
            assert (await http.get("/api/market/regime")).status_code == 200
            history = (await http.get("/api/signals/history", params={"symbol": "SOL"})).json()
            assert history["persistence"] == "ok" and history["signals"][0]["symbol"] == "SOL"
    async with sessions() as s:
        # 5m, 15m, 1H at the base limit; 4H and 1D at the long limit. The candles call must not duplicate.
        expected = 3 * settings.candle_fetch_limit + 2 * settings.fetch_limit(Timeframe.D1)
        assert await s.scalar(select(func.count()).select_from(Candle).where(Candle.base_asset == "SOL")) == expected
        assert await s.scalar(select(func.count()).select_from(MarketSnapshot)) == 20
        assert await s.scalar(select(func.count()).select_from(Signal)) == 20
        assert await s.scalar(select(func.count()).select_from(TechnicalFeature).where(TechnicalFeature.symbol == "SOL")) == 3
        assert await s.scalar(select(func.count()).select_from(MarketRegime)) == 1
        planned = await s.scalar(select(func.count()).select_from(Signal).where(Signal.entry_low.is_not(None)))
        assert await s.scalar(select(func.count()).select_from(SignalTarget)) == 4 * planned
    await engine.dispose()


def test_provider_health_record_model_has_expected_columns():
    cols = set(ProviderHealthRecord.__table__.columns.keys())
    assert {"provider", "status", "circuit_state", "rate_limit_hits", "recorded_at"} <= cols
