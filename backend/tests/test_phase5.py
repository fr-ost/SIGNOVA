"""Phase 5: on-chain / whale activity, market and per-coin sentiment."""

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from app.data.health import ProviderHealthRegistry
from app.models import Base, SentimentReading, WhaleEvent
from app.services.analysis import analysis_out
from app.services.news import NewsDigest, NewsEntry
from app.services.onchain import (
    SourceError,
    classify_transfer,
    fetch_json,
    parse_btc_whales,
    parse_eth_whales,
    parse_mempool,
    parse_stablecoins,
    parse_whale_alert,
)
from app.services.sentiment import fear_greed_component, funding_rates, mood_state, news_by_asset, tone
from tests.test_api import _make_client
from tests.test_phase3_4 import news_handler, settings_for

NOW_TS = int(datetime.now(tz=UTC).timestamp())
DAY = 86_400

FEES = {"fastestFee": 12, "halfHourFee": 9, "hourFee": 6, "economyFee": 3, "minimumFee": 1}
MEMPOOL = {"count": 45_000, "vsize": 23_500_000, "total_fee": 1}
DIFFICULTY = {"progressPercent": 40.5, "difficultyChange": 2.1, "remainingBlocks": 1200}
HASHRATE = {"currentHashrate": 7.1e20, "currentDifficulty": 1}
BTC_TXS = {"txs": [
    {"hash": "b1", "time": NOW_TS, "out": [{"value": 150 * 10**8}, {"value": 5 * 10**8}]},
    {"hash": "b2", "time": NOW_TS, "out": [{"value": 2 * 10**8}]},  # below the whale threshold
]}
ETH_STATS = {"gas_prices": {"slow": 1.1, "average": {"price": 2.5}, "fast": 4.0},
             "transactions_today": "1200000", "network_utilization_percentage": 51.2}
ETH_TXS = {"items": [
    {"hash": "e1", "value": str(5000 * 10**18), "timestamp": "2026-09-24T10:00:00Z",
     "from": {"hash": "0x1", "name": None}, "to": {"hash": "0x2", "name": "Binance 14"}},
    {"hash": "e2", "value": str(2000 * 10**18), "timestamp": "2026-09-24T10:01:00Z",
     "from": {"hash": "0x3", "metadata": {"tags": [{"name": "Coinbase 10"}]}}, "to": {"hash": "0x4"}},
    {"hash": "e3", "value": str(10**18), "timestamp": "2026-09-24T10:02:00Z", "from": {}, "to": {}},
]}
STABLES = {"peggedAssets": [
    {"pegType": "peggedUSD", "circulating": {"peggedUSD": 110.0}, "circulatingPrevDay": {"peggedUSD": 109.0},
     "circulatingPrevWeek": {"peggedUSD": 100.0}, "circulatingPrevMonth": {"peggedUSD": 100.0}},
    {"pegType": "peggedUSD", "circulating": {"peggedUSD": 90.0}, "circulatingPrevDay": {"peggedUSD": 90.0},
     "circulatingPrevWeek": {"peggedUSD": 100.0}, "circulatingPrevMonth": {"peggedUSD": 80.0}},
    {"pegType": "peggedEUR", "circulating": {"peggedEUR": 999.0}},
]}
FNG = {"data": [{"value": str(v), "value_classification": "Greed", "timestamp": str(NOW_TS - i * DAY)}
                for i, v in enumerate([70, 68, 66, 64, 62, 60, 60, 60] + [50] * 22)]}
PREMIUM = [
    {"symbol": "BTCUSDT", "lastFundingRate": "0.0001"},
    {"symbol": "ETHUSDT", "lastFundingRate": "0.0006"},  # 0.06% per 8h: crowded longs
    {"symbol": "SOLUSDT", "lastFundingRate": "-0.0004"},
    {"symbol": "DOGEUSDC", "lastFundingRate": "0.01"},  # not a USDT perpetual of the universe
]
WHALE_ALERT = {"result": "success", "transactions": [
    {"id": "1", "blockchain": "ripple", "symbol": "xrp", "hash": "x1", "amount": 5e7, "amount_usd": 2.5e7,
     "timestamp": NOW_TS, "from": {"owner_type": "unknown"}, "to": {"owner": "bitstamp", "owner_type": "exchange"}},
]}

CALLS: list[str] = []


def chain_handler(request: httpx.Request) -> httpx.Response:
    CALLS.append(f"{request.url.host}{request.url.path}")
    host, path = request.url.host, request.url.path
    routes = {
        ("mempool.space", "/api/v1/fees/recommended"): FEES,
        ("mempool.space", "/api/mempool"): MEMPOOL,
        ("mempool.space", "/api/v1/difficulty-adjustment"): DIFFICULTY,
        ("mempool.space", "/api/v1/mining/hashrate/3d"): HASHRATE,
        ("blockchain.info", "/unconfirmed-transactions"): BTC_TXS,
        ("eth.blockscout.com", "/api/v2/stats"): ETH_STATS,
        ("eth.blockscout.com", "/api/v2/main-page/transactions"): ETH_TXS,
        ("stablecoins.llama.fi", "/stablecoins"): STABLES,
        ("api.alternative.me", "/fng/"): FNG,
        ("fapi.binance.com", "/fapi/v1/premiumIndex"): PREMIUM,
        ("api.whale-alert.io", "/v1/transactions"): WHALE_ALERT,
    }
    if (host, path) in routes:
        return httpx.Response(200, json=routes[(host, path)])
    return news_handler(request)


def mock_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(chain_handler))


# ----------------------------------------------------------------------------- parsers


def test_classify_transfer():
    assert classify_transfer(None, "Binance 14") == "exchange_inflow"
    assert classify_transfer("Coinbase 10", "0xabc") == "exchange_outflow"
    assert classify_transfer("Kraken", "OKX 3") == "inter_exchange"
    assert classify_transfer(None, None) == "unknown"
    assert all(len(c) <= 16 for c in ("exchange_inflow", "exchange_outflow", "inter_exchange"))  # column width


def test_parse_mempool_and_stablecoins():
    btc = parse_mempool(FEES, MEMPOOL, DIFFICULTY, HASHRATE)
    assert btc["fees_sat_vb"]["fastestFee"] == 12 and "minimumFee" not in btc["fees_sat_vb"]
    assert btc["mempool_vsize_mb"] == 23.5 and btc["hashrate_ehs"] == 710.0 and btc["blocks_to_retarget"] == 1200
    assert parse_mempool(None, None, None, None) == {}
    stables = parse_stablecoins(STABLES)
    assert stables["total_usd"] == 200.0 and stables["stablecoins_counted"] == 2
    assert stables["change_7d_pct"] == 0.0 and stables["change_1d_pct"] == pytest.approx(0.503, abs=1e-3)
    assert stables["change_30d_pct"] == pytest.approx(11.111, abs=1e-3)
    assert parse_stablecoins({"peggedAssets": []}) == {} and parse_stablecoins("junk") == {}


def test_parse_whales():
    btc = parse_btc_whales(BTC_TXS, 100, 60_000.0)
    assert [w.tx_hash for w in btc] == ["b1"] and btc[0].amount == 155 and btc[0].amount_usd == 155 * 60_000
    assert btc[0].url == "https://mempool.space/tx/b1" and btc[0].classification == "unknown"
    assert parse_btc_whales(BTC_TXS, 100, None)[0].amount_usd is None
    eth = parse_eth_whales(ETH_TXS, 1000, 2000.0)
    assert [(w.tx_hash, w.classification) for w in eth] == [("e1", "exchange_inflow"), ("e2", "exchange_outflow")]
    assert eth[0].to_label == "Binance 14" and eth[1].from_label == "Coinbase 10"
    wa = parse_whale_alert(WHALE_ALERT)
    assert wa[0].symbol == "XRP" and wa[0].classification == "exchange_inflow" and wa[0].to_label == "bitstamp"
    assert parse_eth_whales("junk", 1, None) == [] and parse_whale_alert(None) == []


def test_sentiment_helpers():
    assert [mood_state(x) for x in (-0.9, -0.3, 0.0, 0.3, 0.9)] == ["EXTREME_FEAR", "FEAR", "NEUTRAL", "GREED", "EXTREME_GREED"]
    fg = fear_greed_component(FNG)
    assert fg["value"] == 70 and fg["change_7d"] == 10 and fg["score"] == pytest.approx(0.4)
    assert fg["avg_30d"] == pytest.approx(53.7, abs=0.1)
    assert fear_greed_component({"data": []}) is None
    rates = funding_rates(PREMIUM, {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL"})
    assert rates == pytest.approx({"BTC": 0.01, "ETH": 0.06, "SOL": -0.04})
    now = datetime(2026, 9, 24, 12, tzinfo=UTC)
    items = [
        NewsEntry("a", "u1", "s", datetime(2026, 9, 24, tzinfo=UTC), "", ["BTC"], "positive"),
        NewsEntry("b", "u2", "s", datetime(2026, 9, 20, tzinfo=UTC), "", ["BTC"], "negative"),  # outside 48h
        NewsEntry("c", "u3", "s", None, "", ["ETH", "BTC"], "negative"),
    ]
    digest = NewsDigest(now, items, [], {}, {}, [], [])
    grouped, recent = news_by_asset(digest, now)
    assert len(recent) == 2 and [i.title for i in grouped["BTC"]] == ["a", "c"]
    assert tone(grouped["BTC"]) == (0.0, 1, 1) and tone([]) == (None, 0, 0)


async def test_fetch_json_records_health_and_hides_url():
    health = ProviderHealthRegistry()

    def handler(request):
        return httpx.Response(403) if "blocked" in request.url.path else httpx.Response(200, content=b"not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SourceError) as blocked:
            await fetch_json(http, health, "src", "onchain", "https://secret.example/blocked?api_key=abc")
        assert "secret" not in str(blocked.value) and "abc" not in str(blocked.value) and "403" in str(blocked.value)
        with pytest.raises(SourceError, match="invalid JSON"):
            await fetch_json(http, health, "src", "onchain", "https://secret.example/ok")


# ----------------------------------------------------------------------------- service and API


@pytest.fixture
async def env5(tmp_path, request):
    extra = getattr(request, "param", {})
    settings = settings_for(tmp_path, **extra)
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app, sessions = await _make_client(settings, engine)
    async with app.router.lifespan_context(app):
        c = app.state.container
        c.news._trending = "https://cg.example/trending"
        c.news._http = c.onchain._http = c.sentiment._http = mock_http()
        CALLS.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            await http.get("/api/market")  # universe and listing prices for USD values
            yield http, c, sessions
    await engine.dispose()


async def test_onchain_endpoint_and_whale_persistence(env5):
    http, c, sessions = env5
    body = (await http.get("/api/onchain")).json()
    assert body["btc"]["fees_sat_vb"]["halfHourFee"] == 9
    assert body["eth"]["gas_gwei"] == {"slow": 1.1, "average": 2.5, "fast": 4.0}
    assert body["eth"]["network_utilization_pct"] == 51.2
    assert body["stablecoins"]["total_usd"] == 200.0
    assert {w["tx_hash"] for w in body["whales"]} == {"b1", "e1", "e2"}  # whale-alert needs a key
    assert body["sources_ok"] == ["blockchain.com", "blockscout", "defillama", "mempool.space"] and body["errors"] == []
    assert set(body["flows"]) <= {"ETH"}
    usd = [w["amount_usd"] for w in body["whales"] if w["amount_usd"] is not None]
    assert usd == sorted(usd, reverse=True)
    assert not any("whale-alert" in call for call in CALLS)
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(WhaleEvent)) == 3
    # a forced refresh right away is served from the cache (min refresh guard); no new source calls
    calls = len(CALLS)
    assert (await http.get("/api/onchain", params={"refresh": "true"})).json()["fetched_at"] == body["fetched_at"]
    assert len(CALLS) == calls
    health = (await http.get("/api/provider-health")).json()
    assert {"mempool_space", "blockscout", "defillama", "blockchain_com"} <= {p["provider"] if isinstance(p, dict) else p for p in health["providers"]}


@pytest.mark.parametrize("env5", [{"whale_alert_api_key": "wa-secret-key"}], indirect=True)
async def test_whale_alert_optional_source(env5):
    http, c, sessions = env5
    body = (await http.get("/api/onchain")).json()
    assert "whale-alert" in body["sources_ok"]
    xrp = next(w for w in body["whales"] if w["symbol"] == "XRP")
    assert xrp["classification"] == "exchange_inflow" and body["flows"]["XRP"]["exchange_inflow"] == 2.5e7
    assert "wa-secret-key" not in (await http.get("/api/onchain")).text


async def test_sentiment_endpoint_components_and_notes(env5):
    http, c, sessions = env5
    body = (await http.get("/api/sentiment")).json()
    comps = body["components"]
    assert comps["fear_greed"]["value"] == 70 and body["trend"] == "RISING"
    assert comps["funding"]["crowded_longs"] == ["ETH"] and comps["funding"]["crowded_shorts"] == ["SOL"]
    assert comps["stablecoins"]["change_7d_pct"] == 0.0 and "news" in comps
    weights = {"fear_greed": 0.5, "news": 0.3, "funding": 0.2}
    expected = sum(weights[k] * comps[k]["score"] for k in weights) / sum(weights.values())
    assert body["score"] == pytest.approx(expected, abs=1e-3) and body["state"] == mood_state(body["score"])
    eth = body["assets"]["ETH"]
    assert eth["funding_rate_pct"] == pytest.approx(0.06) and any("crowded longs" in n for n in eth["notes"])
    assert any("negative funding" in n for n in body["assets"]["SOL"]["notes"])
    assert body["assets"]["BTC"]["headlines"] >= 1 and body["assets"]["BTC"]["recent_titles"]
    assert c.sentiment.for_asset("eth").symbol == "ETH"
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(SentimentReading).where(SentimentReading.scope == "market")) == 1
        assert await s.scalar(select(func.count()).select_from(SentimentReading).where(SentimentReading.scope == "asset")) >= 2


async def test_sentiment_survives_failing_sources(env5):
    http, c, _ = env5
    c.sentiment._http = c.onchain._http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    c.news._http = c.sentiment._http
    body = (await http.get("/api/sentiment")).json()
    assert body["state"] == "UNKNOWN" and body["score"] is None and body["components"] == {}
    assert any("alternative_me" in e for e in body["errors"]) and any("binance_futures" in e for e in body["errors"])
    chain = (await http.get("/api/onchain")).json()
    assert chain["whales"] == [] and chain["sources_ok"] == [] and chain["errors"]


async def test_scan_refreshes_sentiment_and_adds_risk_notes_without_changing_labels(env5):
    http, c, _ = env5
    await http.post("/api/control/analyze")
    await c.controller.wait()
    scan = (await http.get("/api/signals")).json()
    assert len(scan["signals"]) == 20 and not scan["errors"]
    assert c.sentiment.cached() is not None  # refreshed before the scan
    eth = (await http.get("/api/assets/ETH/analysis")).json()
    assert eth["sentiment"]["symbol"] == "ETH"
    assert any(r.startswith("sentiment: perpetual funding") for r in eth["risks"])

    # the same analysis without sentiment has the same label and score
    c.analysis.sentiment_for = lambda symbol: None
    c.analysis._cache.invalidate()
    plain = (await http.get("/api/assets/ETH/analysis")).json()
    assert plain["signal"] == eth["signal"] and plain["score"] == eth["score"]
    assert plain["sentiment"] is None and not any(r.startswith("sentiment:") for r in plain["risks"])


async def test_scan_survives_a_failing_sentiment_hook(env5):
    http, c, _ = env5

    async def broken():
        raise RuntimeError("boom")

    c.analysis.before_scan = broken
    await http.post("/api/control/analyze")
    await c.controller.wait()
    assert len((await http.get("/api/signals")).json()["signals"]) == 20


async def test_chat_context_includes_sentiment_and_onchain(env5):
    http, c, _ = env5
    await http.get("/api/sentiment")
    from app.services.assistant import chat_context

    ctx = await chat_context(c.analysis, c.controller, c.news, c.portfolio, c.watchlist, "ETH", c.sentiment, c.onchain)
    assert ctx["market_sentiment"]["state"] and ctx["onchain"]["btc_network"]["fees_sat_vb"]
    assert "ETH" in ctx["market_sentiment"]["crowded_longs_funding"] and "ETH" in ctx["market_sentiment"]["coins_with_notes"]


def test_analysis_out_accepts_sentiment_argument():
    assert "sentiment" in analysis_out.__code__.co_varnames


@pytest.mark.skipif(not __import__("os").getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
async def test_postgres_phase5_persistence():
    """Whale events and sentiment readings on a real PostgreSQL migrated with `alembic upgrade head`."""
    import os

    from app.config import Settings

    settings = Settings(_env_file=None, database_url=os.environ["TEST_DATABASE_URL"], json_logs=False,
                        whale_alert_api_key="k", news_feeds=["https://feed.example/rss"],
                        cryptocompare_news_url="https://cc.example/news")
    engine = create_async_engine(settings.async_database_url, connect_args=settings.database_connect_args)
    async with engine.begin() as conn:
        for table in ("whale_events", "sentiment"):
            await conn.exec_driver_sql(f"DELETE FROM {table}")
    app, sessions = await _make_client(settings, engine)
    async with app.router.lifespan_context(app):
        c = app.state.container
        c.news._trending = None
        c.news._http = c.onchain._http = c.sentiment._http = mock_http()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            await http.get("/api/market")
            assert len((await http.get("/api/onchain")).json()["whales"]) == 4
            assert (await http.get("/api/sentiment")).json()["state"] != "UNKNOWN"
            c.onchain._cache.invalidate()
            await http.get("/api/onchain")  # the same transfers again: no duplicates
    async with sessions() as s:
        assert await s.scalar(select(func.count()).select_from(WhaleEvent)) == 4
        assert await s.scalar(select(func.count()).select_from(SentimentReading).where(SentimentReading.scope == "market")) == 1
    await engine.dispose()
