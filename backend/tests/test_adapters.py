from datetime import timedelta

import httpx
import pytest

from app.core.enums import ProviderStatus, Timeframe
from app.core.timeutil import floor_to_timeframe, from_ms, to_ms, utcnow
from app.data.adapters import binance as bn
from app.data.adapters import kraken as kr
from app.data.adapters import listings as ls
from app.data.health import ProviderHealthRegistry
from app.data.http import ProviderBadResponse, ProviderClientError, ProviderUnavailable, RetryPolicy
from app.data.providers.binance import BinanceRestClient
from app.data.providers.kraken import KrakenRestClient
from app.services.listing import ListingService, ListingUnavailable
from tests.conftest import FakeListingAdapter, make_listing


async def _no_sleep(_: float) -> None:
    return None


# ------------------------------------------------------------------ Binance


def _kline_rows(tf: Timeframe, count: int):
    now = utcnow()
    start = to_ms(floor_to_timeframe(now, tf)) - tf.ms * (count - 1)
    rows = []
    for i in range(count):
        o = start + i * tf.ms
        rows.append([o, "100", "101", "99", "100.5", "12.5", o + tf.ms - 1, "1250", 42, "6", "600", "0"])
    return rows, now


def test_parse_klines_marks_forming_candle():
    rows, now = _kline_rows(Timeframe.M15, 5)
    candles = bn.parse_klines(rows, "BTCUSDT", Timeframe.M15, now)
    assert len(candles) == 5
    assert all(c.is_closed for c in candles[:-1])
    assert candles[-1].is_closed is False
    assert candles[0].close_time - candles[0].open_time == timedelta(minutes=15)
    assert candles[0].trades == 42 and candles[0].quote_volume == 1250.0


def test_parse_klines_rejects_malformed_rows():
    with pytest.raises(ProviderBadResponse):
        bn.parse_klines([[1, 2, 3]], "BTCUSDT", Timeframe.H1, utcnow())
    with pytest.raises(ProviderBadResponse):
        bn.parse_klines({"code": -1}, "BTCUSDT", Timeframe.H1, utcnow())


def test_parse_exchange_info_filters_trading_usdt_spot_pairs():
    raw = {
        "symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "isSpotTradingAllowed": True},
            {"symbol": "ETHBTC", "status": "TRADING", "baseAsset": "ETH", "quoteAsset": "BTC"},
            {"symbol": "LUNAUSDT", "status": "BREAK", "baseAsset": "LUNA", "quoteAsset": "USDT"},
            {"symbol": "XUSDT", "status": "TRADING", "baseAsset": "X", "quoteAsset": "USDT", "isSpotTradingAllowed": False},
        ]
    }
    assert bn.parse_exchange_info(raw, "USDT") == {"BTC": "BTCUSDT"}


def test_parse_ticker_and_depth():
    t = bn.parse_ticker(
        {"symbol": "BTCUSDT", "lastPrice": "60000.1", "bidPrice": "60000", "askPrice": "60000.2",
         "priceChangePercent": "-1.25", "quoteVolume": "1000000", "volume": "20", "closeTime": 1_700_000_000_000,
         "openPrice": "60750", "highPrice": "61000", "lowPrice": "59000"},
        "BTC", "USDT", utcnow(),
    )
    assert t.last_price == 60000.1 and t.pct_change_24h == -1.25
    assert t.event_time == from_ms(1_700_000_000_000)
    book = bn.parse_depth({"lastUpdateId": 5, "bids": [["100", "1"]], "asks": [["101", "2"]]}, "BTCUSDT", utcnow())
    assert book.bids == ((100.0, 1.0),) and book.last_update_id == 5
    with pytest.raises(ProviderBadResponse):
        bn.parse_ticker({"symbol": "BTCUSDT", "lastPrice": "NaN"}, "BTC", "USDT", utcnow())


def _binance(handler, bases=("https://a.test", "https://b.test"), **kw):
    health = ProviderHealthRegistry()
    client = BinanceRestClient(
        list(bases),
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        health,
        retry=RetryPolicy(max_retries=0),
        rate_per_second=0,
        sleep=_no_sleep,
        **kw,
    )
    return client, health


async def test_binance_fails_over_from_geo_restricted_base():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "a.test":
            return httpx.Response(451, json={"code": 0, "msg": "Service unavailable from a restricted location"})
        return httpx.Response(200, json={"serverTime": 1})

    client, health = _binance(handler)
    assert await client.server_time() == {"serverTime": 1}
    assert client.active_base_url == "https://b.test"
    # the restricted base is parked: the next call goes straight to b.test
    await client.server_time()
    assert seen == ["a.test", "b.test", "b.test"]
    assert "https://a.test" in health.get("binance").details["restricted_bases"]


async def test_binance_fails_over_from_waf_403():
    def handler(request):
        if request.url.host == "a.test":
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, json={"serverTime": 2})

    client, _ = _binance(handler)
    assert await client.server_time() == {"serverTime": 2}
    assert client.active_base_url == "https://b.test"


async def test_binance_all_bases_failing_raises_unavailable():
    client, _ = _binance(lambda r: httpx.Response(503))
    with pytest.raises(ProviderUnavailable) as exc:
        await client.server_time()
    assert "a.test" in exc.value.message and "b.test" in exc.value.message


async def test_binance_invalid_symbol_is_client_error_without_failover():
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

    client, _ = _binance(handler)
    with pytest.raises(ProviderClientError):
        await client.klines("NOPEUSDT", "1h", 10)
    assert seen == ["a.test"]


async def test_binance_weight_header_triggers_pause():
    def handler(request):
        return httpx.Response(200, json={}, headers={"x-mbx-used-weight-1m": "5900"})

    client, health = _binance(handler, bases=("https://a.test",), weight_limit_1m=6000)
    await client.server_time()
    assert health.get("binance").details["used_weight_1m"] == 5900
    assert client._bases[0].http.limiter._paused_until > 0


async def test_binance_tickers_request_uses_json_symbols_param():
    captured = {}

    def handler(request):
        captured["symbols"] = request.url.params["symbols"]
        return httpx.Response(200, json=[])

    client, _ = _binance(handler, bases=("https://a.test",))
    await client.tickers_24h(["ETHUSDT", "BTCUSDT"])
    assert captured["symbols"] == '["BTCUSDT","ETHUSDT"]'


# ------------------------------------------------------------------ Kraken


def test_kraken_asset_pairs_normalise_xbt_and_xdg():
    result = {
        "XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "status": "online"},
        "XDGUSD": {"altname": "XDGUSD", "wsname": "XDG/USD", "status": "online"},
        "XETHZEUR": {"altname": "ETHEUR", "wsname": "ETH/EUR", "status": "online"},
        "SOLUSD": {"altname": "SOLUSD", "wsname": "SOL/USD", "status": "cancel_only"},
    }
    pairs, keys = kr.parse_asset_pairs(result, "USD")
    assert pairs == {"BTC": "XBTUSD", "DOGE": "XDGUSD"}
    assert keys["XBTUSD"] == "XXBTZUSD"


def test_kraken_ohlc_last_row_is_never_closed():
    tf = Timeframe.H1
    now = utcnow()
    start = int(floor_to_timeframe(now, tf).timestamp()) - 3600 * 3
    rows = [[start + i * 3600, "1", "2", "0.5", "1.5", "1.2", "10", 5] for i in range(4)]
    candles = kr.parse_ohlc({"XXBTZUSD": rows, "last": start}, "XBTUSD", tf, now)
    assert [c.is_closed for c in candles] == [True, True, True, False]
    assert candles[0].quote_volume == pytest.approx(12.0)


def test_kraken_ticker_has_no_fake_24h_change():
    info = {"a": ["101", "1", "1"], "b": ["100", "1", "1"], "c": ["100.5", "0.1"], "v": ["10", "20"],
            "p": ["100", "100.2"], "h": ["102", "103"], "l": ["99", "98"], "o": "99.5"}
    t = kr.parse_ticker(info, "XBTUSD", "BTC", "USD", utcnow())
    assert t.pct_change_24h is None and t.open_24h is None
    assert t.volume_quote_24h == pytest.approx(20 * 100.2)


async def test_kraken_error_envelope_maps_to_client_error():
    health = ProviderHealthRegistry()

    def handler(request):
        return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"], "result": {}})

    client = KrakenRestClient(
        "https://k.test", httpx.AsyncClient(transport=httpx.MockTransport(handler)), health,
        retry=RetryPolicy(max_retries=0), rate_per_second=0, sleep=_no_sleep,
    )
    with pytest.raises(ProviderClientError):
        await client.ticker(["NOPEUSD"])


# ------------------------------------------------------------------ listings / context


def test_parse_cmc_v1_listing():
    data = [
        {"id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "cmc_rank": 1, "tags": ["mineable", "pow"],
         "quote": {"USD": {"price": 60000, "market_cap": 1.2e12, "volume_24h": 3e10, "percent_change_24h": 1.1,
                           "last_updated": "2026-09-24T01:00:00.000Z"}}},
        {"id": 825, "name": "Tether USDt", "symbol": "USDT", "slug": "tether", "cmc_rank": 3,
         "tags": ["stablecoin", "usd-stablecoin"], "quote": {"USD": {"price": 1.0, "market_cap": 1.4e11}}},
        {"id": 9, "name": "Broken", "symbol": "BRK", "quote": {"USD": {"price": None}}},
    ]
    entries = ls.parse_cmc_listings(data)
    assert [e.symbol for e in entries] == ["BTC", "USDT"]
    assert entries[0].rank == 1 and entries[0].last_updated is not None
    assert "stablecoin" in entries[1].tags


def test_parse_coingecko_marks_stablecoin_category():
    rows = [{"id": "tether", "symbol": "usdt", "name": "Tether", "current_price": 1, "market_cap": 1e11,
             "market_cap_rank": 3, "last_updated": "2026-09-24T01:00:00Z"}]
    entries = ls.parse_coingecko_markets(rows, {"tether"})
    assert entries[0].symbol == "USDT" and entries[0].tags == ("stablecoin",)


def test_parse_coinpaprika_sorts_by_rank_and_skips_inactive():
    rows = [
        {"id": "eth-ethereum", "symbol": "ETH", "name": "Ethereum", "rank": 2, "quotes": {"USD": {"price": 3000, "market_cap": 4e11}}},
        {"id": "dead-coin", "symbol": "DEAD", "name": "Dead", "rank": 0, "quotes": {"USD": {"price": 1, "market_cap": 1}}},
        {"id": "btc-bitcoin", "symbol": "BTC", "name": "Bitcoin", "rank": 1, "quotes": {"USD": {"price": 60000, "market_cap": 1.2e12}}},
    ]
    assert [e.symbol for e in ls.parse_coinpaprika_tickers(rows, 10)] == ["BTC", "ETH"]


def test_parse_global_and_fear_greed():
    g = ls.parse_cmc_global({"btc_dominance": 56.2, "eth_dominance": 13.1,
                             "quote": {"USD": {"total_market_cap": 2.4e12, "total_volume_24h": 9e10,
                                               "total_market_cap_yesterday_percentage_change": -1.5}}})
    assert g.btc_dominance_pct == 56.2 and g.total_market_cap_usd == 2.4e12
    cg = ls.parse_coingecko_global({"data": {"total_market_cap": {"usd": 2.3e12}, "market_cap_percentage": {"btc": 55.0},
                                             "updated_at": 1_700_000_000}})
    assert cg.source == "coingecko" and cg.btc_dominance_pct == 55.0
    fg = ls.parse_cmc_fear_greed({"value": 64, "value_classification": "Greed", "update_time": "2026-09-24T00:00:00Z"})
    assert fg.value == 64
    alt = ls.parse_alternative_me({"data": [{"value": "40", "value_classification": "Fear", "timestamp": "1700000000"}]})
    assert alt.source == "alternative_me" and alt.value == 40
    with pytest.raises(ProviderBadResponse):
        ls.parse_cmc_fear_greed({"value": 140})


async def test_listing_service_falls_back_and_reports_errors():
    primary = FakeListingAdapter("cmc", fail=True)
    secondary = FakeListingAdapter("gecko")
    svc = ListingService([primary, secondary], fetch_limit=100, min_entries=20, max_age_seconds=900)
    result = await svc.fetch()
    assert result.source == "gecko" and result.fallback_used is True
    assert any("cmc" in e for e in result.errors)


async def test_listing_service_rejects_stale_or_short_listings():
    old = [make_listing("BTC", 1, 1)]
    svc = ListingService([FakeListingAdapter("a", entries=old)], fetch_limit=100, min_entries=20, max_age_seconds=900)
    with pytest.raises(ListingUnavailable) as exc:
        await svc.fetch()
    assert "only 1 valid listings" in exc.value.errors[0]


async def test_listing_latest_is_cached():
    adapter = FakeListingAdapter("a")
    svc = ListingService([adapter], fetch_limit=100, min_entries=20, max_age_seconds=900, refresh_seconds=60)
    await svc.latest()
    await svc.latest()
    assert adapter.calls == 1


def test_health_registry_marks_status_changes_dirty():
    h = ProviderHealthRegistry()
    h.register("x", "spot_market")
    h.record_success("x", 120)
    assert [p.provider for p in h.pop_dirty()] == ["x"]
    h.record_success("x", 100)
    assert h.pop_dirty() == []
    for _ in range(3):
        h.record_failure("x", "boom")
    assert h.status_of("x") == ProviderStatus.DOWN
