from datetime import timedelta

import pytest

from app.core.enums import CrossCheckStatus, DataState, Timeframe
from app.core.timeutil import utcnow
from app.services.container import build_container
from app.services.spot_router import NoMarketData, SpotMarketRouter
from app.data.health import ProviderHealthRegistry
from tests.conftest import (
    FakeAltcoinSeasonAdapter,
    FakeFearGreedAdapter,
    FakeGlobalAdapter,
    FakeListingAdapter,
    FakeSpotAdapter,
    make_listing,
)


def container(settings, *, spot=None, listing=None, fng_fail=False, global_fail=False, reference=None):
    return build_container(
        settings,
        use_database=False,
        spot_adapters=spot or [FakeSpotAdapter("fakeex")],
        listing_adapters=listing or [FakeListingAdapter("fakelist")],
        global_adapters=[FakeGlobalAdapter(fail=global_fail)],
        fear_greed_adapters=[FakeFearGreedAdapter(fail=fng_fail)],
        altcoin_season_adapters=[FakeAltcoinSeasonAdapter(fail=fng_fail)],
        reference_candle_adapter=reference,
    )


async def test_universe_excludes_stablecoins_and_wrapped_and_keeps_unsupported(test_settings):
    c = container(test_settings)
    universe = await c.universe.get()
    symbols = [a.symbol for a in universe.assets]
    assert len(symbols) == 20
    assert "USDT" not in symbols and "USDC" not in symbols and "WBTC" not in symbols
    assert symbols[:2] == ["BTC", "ETH"]  # ordered by market cap
    assert {e.symbol for e in universe.excluded} == {"USDT", "USDC", "WBTC"}
    leo = universe.find("LEO")
    assert leo is not None and not leo.supported and "no USDT/USD spot pair" in leo.unsupported_reason
    assert universe.quote_usd_prices["USDT"] == 1.0
    await c.aclose()


async def test_universe_serves_last_known_ranking_marked_stale_when_listing_fails(test_settings):
    listing = FakeListingAdapter("fakelist")
    c = container(test_settings, listing=[listing])
    first = await c.universe.get()
    listing.fail = True
    c.listing._cache.invalidate()
    stale = await c.universe.get(force=True)
    assert stale.stale is True and [a.symbol for a in stale.assets] == [a.symbol for a in first.assets]
    assert stale.errors
    await c.aclose()


async def test_market_snapshot_healthy_with_live_prices(test_settings):
    c = container(test_settings)
    snap = await c.market.snapshot()
    assert snap.data_state in (DataState.HEALTHY, DataState.DEGRADED)
    btc = next(a for a in snap.assets if a.symbol == "BTC")
    assert btc.price == 60000.0 and btc.market_source == "fakeex"
    assert btc.cross_check.status == CrossCheckStatus.CONSISTENT
    # fake exchange never reports provider health (UNKNOWN), which honestly lowers the score
    assert btc.data_state == DataState.HEALTHY and 85 <= btc.data_health_score < 100
    leo = next(a for a in snap.assets if a.symbol == "LEO")
    assert leo.price is None and leo.data_state == DataState.API_FAILURE  # never substituted
    assert leo.reference_price_usd == 6.0
    assert snap.context.fear_greed.value == 62
    assert snap.persistence == "disabled"
    assert c.state.data_state == snap.data_state
    await c.aclose()


async def test_market_snapshot_flags_price_conflict(test_settings):
    spot = FakeSpotAdapter("fakeex", price_multiplier={"SOL": 1.05})
    c = container(test_settings, spot=[spot])
    snap = await c.market.snapshot()
    sol = next(a for a in snap.assets if a.symbol == "SOL")
    assert sol.data_state == DataState.DATA_CONFLICT
    assert sol.cross_check.status == CrossCheckStatus.CONFLICT
    await c.aclose()


async def test_market_snapshot_stale_ticker(test_settings):
    c = container(test_settings, spot=[FakeSpotAdapter("fakeex", stale_ticker_seconds=600)])
    snap = await c.market.snapshot()
    assert snap.data_state == DataState.STALE_DATA
    assert all(a.data_state == DataState.STALE_DATA for a in snap.assets if a.supported)
    await c.aclose()


async def test_market_snapshot_fails_over_to_second_exchange(test_settings):
    primary = FakeSpotAdapter("primaryex", fail=True)
    fallback = FakeSpotAdapter("fallbackex", quote_asset="USD")
    c = container(test_settings, spot=[primary, fallback])
    snap = await c.market.snapshot()
    btc = next(a for a in snap.assets if a.symbol == "BTC")
    assert btc.market_source == "fallbackex" and btc.quote_asset == "USD"
    assert btc.data_state == DataState.DEGRADED
    assert any("fallback source" in r for r in btc.reasons)
    await c.aclose()


async def test_market_snapshot_all_exchanges_down_is_api_failure(test_settings):
    c = container(test_settings, spot=[FakeSpotAdapter("a", fail=True), FakeSpotAdapter("b", fail=True)])
    snap = await c.market.snapshot()
    assert snap.data_state == DataState.API_FAILURE
    assert all(a.price is None for a in snap.assets)
    await c.aclose()


async def test_market_context_unavailable_is_explicit(test_settings):
    c = container(test_settings, fng_fail=True, global_fail=True)
    snap = await c.market.snapshot()
    assert snap.context.fear_greed is None and snap.context.fear_greed_status == "UNAVAILABLE"
    assert snap.context.global_status == "UNAVAILABLE" and snap.context.errors
    await c.aclose()


async def test_market_snapshot_is_cached_single_flight(test_settings):
    spot = FakeSpotAdapter("fakeex")
    c = container(test_settings, spot=[spot])
    import asyncio

    await asyncio.gather(*(c.market.snapshot() for _ in range(5)))
    assert spot.calls["tickers"] == 1
    await c.aclose()


async def test_asset_detail_runs_full_gate(test_settings):
    c = container(test_settings)
    detail = await c.assets.detail("btc")
    assert detail.symbol == "BTC"
    assert detail.integrity.decision == "PASS", detail.integrity.reasons
    assert len(detail.timeframes) == 5 and all(tf.ok for tf in detail.timeframes)
    assert detail.order_book.valid and detail.order_book.spread_bps > 0
    assert detail.volatility.available
    assert [s.stage for s in detail.integrity.stages][0] == "DATA_HEALTH_CHECK"
    await c.aclose()


async def test_asset_detail_unsupported_asset_is_no_trade(test_settings):
    c = container(test_settings)
    detail = await c.assets.detail("LEO")
    assert detail.integrity.decision == "NO TRADE"
    assert detail.integrity.state == DataState.API_FAILURE
    await c.aclose()


async def test_asset_detail_respects_signal_pause(test_settings):
    c = container(test_settings)
    c.state.signal_paused_reason = "emergency stop"
    detail = await c.assets.detail("ETH")
    assert detail.integrity.state == DataState.SIGNAL_PAUSED
    await c.aclose()


async def test_candles_endpoint_returns_closed_candles_and_forming_separately(test_settings):
    c = container(test_settings)
    out = await c.assets.candles("ETH", Timeframe.H4, 250)
    assert len(out.candles) == 250 and out.forming is not None
    assert out.candles[-1].open_time < out.forming.open_time
    assert out.validation.ok
    await c.aclose()


async def test_router_raises_when_no_candidate_serves_candles():
    health = ProviderHealthRegistry()
    router = SpotMarketRouter([FakeSpotAdapter("a", fail=True)], health)
    refs, errors = await router.resolve(["BTC"])
    assert refs["BTC"] == [] and errors
    with pytest.raises(NoMarketData):
        await router.candles([], Timeframe.H1, 10)


async def test_listing_stale_reference_makes_prices_unverified(test_settings):
    old = utcnow() - timedelta(minutes=10)
    from dataclasses import replace

    from tests.conftest import UNIVERSE_SPEC

    entries = [replace(make_listing(s, p, m, tags=t), last_updated=old) for s, p, m, t in UNIVERSE_SPEC]
    c = container(test_settings, listing=[FakeListingAdapter("fakelist", entries=entries)])
    snap = await c.market.snapshot()
    btc = next(a for a in snap.assets if a.symbol == "BTC")
    assert btc.cross_check.status == CrossCheckStatus.UNVERIFIED
    assert btc.data_state == DataState.DEGRADED
    await c.aclose()


# ------------------------------------------------------------------ CoinMarketCap reference candles


async def test_asset_detail_candle_history_matches_reference(test_settings):
    from tests.conftest import FakeReferenceAdapter

    ref = FakeReferenceAdapter()
    c = container(test_settings, reference=ref)
    detail = await c.assets.detail("ETH")
    checks = {chk.label: chk for chk in detail.candle_cross_checks}
    assert set(checks) == {"1H", "1D"}
    assert all(chk.status == CrossCheckStatus.CONSISTENT for chk in checks.values())
    assert checks["1H"].compared >= 10
    assert detail.integrity.decision == "PASS"
    await c.assets.detail("ETH")
    assert ref.calls == 2  # cached per timeframe
    await c.aclose()


async def test_asset_detail_conflicting_candle_history_blocks_signals(test_settings):
    from tests.conftest import FakeReferenceAdapter

    c = container(test_settings, reference=FakeReferenceAdapter(scale=1.06))
    detail = await c.assets.detail("ETH")
    assert detail.integrity.decision == "NO TRADE"
    assert detail.integrity.state == DataState.DATA_CONFLICT
    assert any("history disagrees" in r for r in detail.integrity.reasons)
    await c.aclose()


async def test_asset_detail_plan_without_ohlcv_is_noted_not_blocking(test_settings):
    from app.data.http import ProviderPlanLimited
    from tests.conftest import FakeReferenceAdapter

    ref = FakeReferenceAdapter(error=ProviderPlanLimited("fakelist", "CMC error 1006: plan does not include endpoint"))
    c = container(test_settings, reference=ref)
    detail = await c.assets.detail("BTC")
    assert all(chk.status == CrossCheckStatus.UNVERIFIED for chk in detail.candle_cross_checks)
    assert "current CoinMarketCap plan" in detail.candle_cross_checks[0].reason
    assert detail.integrity.decision == "PASS"
    stage = next(s for s in detail.integrity.stages if s.stage == "SOURCE_CONSISTENCY_CHECK")
    assert stage.passed and any("not cross-verified" in r for r in stage.reasons)
    await c.aclose()


async def test_reference_skipped_when_ranking_came_from_another_source(test_settings):
    from tests.conftest import FakeReferenceAdapter

    ref = FakeReferenceAdapter(name="coinmarketcap")  # listing source is "fakelist"
    c = container(test_settings, reference=ref)
    detail = await c.assets.detail("BTC")
    assert ref.calls == 0
    assert all("CoinMarketCap id is unknown" in chk.reason for chk in detail.candle_cross_checks)
    await c.aclose()


async def test_market_context_includes_altcoin_season(test_settings):
    c = container(test_settings)
    snap = await c.market.snapshot()
    assert snap.context.altcoin_season.value == 38
    assert snap.context.altcoin_season_status == "AVAILABLE"
    await c.aclose()
