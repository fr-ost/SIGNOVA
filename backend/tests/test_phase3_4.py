"""Phase 3/4: manual control, watchlist, news, chat, portfolio."""

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.models import Base
from app.services.analysis import analysis_out
from app.services.news import headline_sentiment, parse_cryptocompare, parse_feed, parse_trending, tag_assets
from tests.test_api import _make_client

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Bitcoin surges to record high as ETF inflows jump</title><link>https://news.example/a</link>
<pubDate>Wed, 24 Sep 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;BTC and Solana rally&lt;/p&gt;</description></item>
<item><title>Exchange hack drains funds</title><link>https://news.example/b</link></item>
<item><title>No link here</title></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Ethereum upgrade launches</title>
<link href="https://news.example/c"/><updated>2026-09-24T09:00:00Z</updated><summary>ETH</summary></entry></feed>"""


def news_handler(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "feed.example":
        return httpx.Response(200, content=RSS)
    if host == "atom.example":
        return httpx.Response(200, content=ATOM)
    if host == "cc.example":
        return httpx.Response(200, json={"Data": [{"title": "XRP lawsuit ends", "url": "https://news.example/d",
                                                   "published_on": 1790000000, "source_info": {"name": "CC"}, "body": "Ripple"}]})
    if host == "cg.example":
        return httpx.Response(200, json={"coins": [{"item": {"symbol": "pepe", "name": "Pepe", "market_cap_rank": 30,
                                                             "data": {"price_change_percentage_24h": {"usd": 12.5}}}}]})
    if host == "broken.example":
        return httpx.Response(503)
    return httpx.Response(404)


def settings_for(tmp_path, **extra) -> Settings:
    return Settings(
        _env_file=None, environment="test", database_url=f"sqlite+aiosqlite:///{tmp_path}/t.db", json_logs=False,
        log_level="WARNING", candle_fetch_limit=300, candle_fetch_limit_long=300, candle_min_history=210,
        news_feeds=["https://feed.example/rss", "https://atom.example/feed", "https://broken.example/rss"],
        cryptocompare_news_url="https://cc.example/news", **extra,
    )


@pytest.fixture
async def app_env(tmp_path, request):
    extra = getattr(request, "param", {})
    settings = settings_for(tmp_path, **extra)
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app, sessions = await _make_client(settings, engine)
    async with app.router.lifespan_context(app):
        c = app.state.container
        c.news._trending = "https://cg.example/trending"
        c.news._http = httpx.AsyncClient(transport=httpx.MockTransport(news_handler))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            yield http, c
    await engine.dispose()


# ----------------------------------------------------------------------------- control


async def test_nothing_runs_until_analyze_now_and_status_reports_progress(app_env):
    http, c = app_env
    status = (await http.get("/api/control/status")).json()
    assert status["scan"]["outcome"] == "never_run" and status["auto_minutes"] == 0 and status["last_scan_at"] is None
    assert (await http.get("/api/signals")).json() is None
    started = (await http.post("/api/control/analyze")).json()
    assert started["started"] is True and started["processing_state"] == "ANALYZING"
    again = (await http.post("/api/control/analyze")).json()
    assert again["started"] is False  # one scan at a time
    await c.controller.wait()
    status = (await http.get("/api/control/status")).json()
    assert status["scan"]["outcome"] == "completed" and status["scan"]["done"] == status["scan"]["total"] == 20
    assert status["processing_state"] == "IDLE" and len((await http.get("/api/signals")).json()["signals"]) == 20


async def test_stop_cancels_a_running_scan_and_the_schedule(app_env):
    http, c = app_env
    gate = asyncio.Event()

    async def slow_scan(**_):
        await gate.wait()

    c.analysis.run_scan = slow_scan
    assert (await http.post("/api/control/auto", json={"minutes": 1})).json()["auto_minutes"] == 5  # minimum 5
    await http.post("/api/control/analyze")
    await asyncio.sleep(0)
    status = (await http.post("/api/control/stop")).json()
    assert status["scan"]["outcome"] == "stopped" and not status["scan"]["running"]
    assert status["auto_minutes"] == 0 and status["processing_state"] == "IDLE"
    assert (await http.post("/api/control/auto", json={"minutes": 0})).json()["next_auto_at"] is None


@pytest.mark.parametrize("app_env", [{"admin_token": "s3cret"}], indirect=True)
async def test_admin_token_protects_controls_chat_and_portfolio(app_env):
    http, _ = app_env
    assert (await http.get("/api/control/status")).json()["auth_required"] is True
    for method, path, body in [
        ("post", "/api/control/analyze", None), ("post", "/api/control/stop", None),
        ("post", "/api/watchlist", {"symbol": "APT"}), ("get", "/api/portfolio", None),
        ("post", "/api/chat", {"messages": [{"role": "user", "content": "hi"}]}),
    ]:
        r = await getattr(http, method)(path, **({"json": body} if body else {}))
        assert r.status_code == 401, path
    ok = await http.post("/api/watchlist", json={"symbol": "APT"}, headers={"X-Admin-Token": "s3cret"})
    assert ok.status_code == 200
    assert (await http.get("/api/news")).status_code == 200  # public data stays open


# ----------------------------------------------------------------------------- watchlist


async def test_watchlist_adds_coins_to_the_universe_and_persists(app_env):
    http, c = app_env
    assert (await http.post("/api/watchlist", json={"symbol": "apt", "note": "L1"})).json()["added"] == "APT"
    assert (await http.post("/api/watchlist", json={"symbol": "bad symbol"})).status_code == 422
    await http.post("/api/watchlist", json={"symbol": "NOTACOIN"})
    universe = await c.universe.get()
    apt = universe.find("APT")
    assert apt is not None and apt.watchlist and apt.universe_rank == 21
    assert universe.find("NOTACOIN") is None
    assert any("NOTACOIN" in e for e in universe.errors)
    c.watchlist._items = {}
    await c.watchlist.load(force=True)  # survives a restart
    assert c.watchlist.symbols() == ["APT", "NOTACOIN"]
    assert (await http.delete("/api/watchlist/notacoin")).json()["removed"] is True
    analysis = (await http.get("/api/assets/APT/analysis")).json()
    assert analysis["symbol"] == "APT"


# ----------------------------------------------------------------------------- news


def test_feed_parsers_and_sentiment():
    items = parse_feed(RSS, "feed.example")
    assert [i.title for i in items] == ["Bitcoin surges to record high as ETF inflows jump", "Exchange hack drains funds"]
    assert items[0].published_at == datetime(2026, 9, 24, 10, tzinfo=UTC) and items[0].summary == "BTC and Solana rally"
    atom = parse_feed(ATOM, "atom.example")
    assert atom[0].url == "https://news.example/c" and atom[0].published_at is not None
    assert parse_cryptocompare({"Data": [{"title": "t", "url": "ftp://x"}]}) == []
    assert parse_trending({"coins": [{"item": {"symbol": "wif", "name": "dogwifhat"}}]})[0].symbol == "WIF"
    assert headline_sentiment("Bitcoin surges to record") == "positive"
    assert headline_sentiment("Exchange hack drains funds") == "negative"
    assert headline_sentiment("Markets open") == "neutral"
    tag_assets(items, {"BTC": "Bitcoin", "SOL": "Solana", "ONE": "Harmony"})
    assert items[0].assets == ["BTC", "SOL"] and items[1].assets == []


async def test_news_endpoint_merges_sources_and_reports_failures(app_env):
    http, _ = app_env
    body = (await http.get("/api/news")).json()
    titles = [i["title"] for i in body["items"]]
    assert "XRP lawsuit ends" in titles and "Ethereum upgrade launches" in titles and len(titles) == 4
    assert body["trending"][0]["symbol"] == "PEPE"
    assert any(e.startswith("broken.example") for e in body["errors"])
    assert body["sentiment"]["positive"] >= 1 and body["sentiment"]["negative"] >= 1


# ----------------------------------------------------------------------------- chat


def openai_handler(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "missing-model":
            return httpx.Response(404, json={"error": {"message": "The model does not exist", "code": "model_not_found"}})
        assert request.headers["Authorization"] == "Bearer sk-test"
        return httpx.Response(200, json={"choices": [{"message": {"content": "Answer"}}], "usage": {"total_tokens": 9}})
    return handler


@pytest.mark.parametrize(
    "app_env", [{"openai_api_key": "sk-test", "openai_analysis_model": "missing-model", "chat_rate_limit_per_minute": 1}],
    indirect=True,
)
async def test_chat_uses_openai_with_context_and_falls_back_to_the_second_model(app_env):
    http, c = app_env
    calls: list = []
    c.chat._http = httpx.AsyncClient(transport=httpx.MockTransport(openai_handler(calls)))
    r = await http.post("/api/chat", json={"messages": [{"role": "user", "content": "Is BTC a buy?"}], "symbol": "btc"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"] == "Answer" and body["model"] == "gpt-4o-mini" and "BTC" in body["context_symbols"]
    sent = calls[-1]["messages"]
    assert sent[0]["role"] == "system" and "authoritative" in sent[0]["content"]
    context = json.loads(sent[1]["content"].split("\n", 1)[1])
    assert context["selected_coin"]["symbol"] == "BTC" and "news" in context and "portfolio" in context
    assert sent[-1] == {"role": "user", "content": "Is BTC a buy?"}
    limited = await http.post("/api/chat", json={"messages": [{"role": "user", "content": "again"}]})
    assert limited.status_code == 429  # one per minute in this test


async def test_chat_without_key_is_explicitly_unavailable(app_env):
    http, _ = app_env
    r = await http.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]
    bad = await http.post("/api/chat", json={"messages": [{"role": "system", "content": "x"}]})
    assert bad.status_code == 422


# ----------------------------------------------------------------------------- portfolio


async def test_portfolio_positions_pnl_and_risk_settings(app_env):
    http, c = app_env
    body = (await http.put("/api/portfolio/cash", json={"cash": 10000})).json()
    assert body["cash"] == 10000 and body["equity"] == 10000
    body = (await http.post("/api/portfolio/positions", json={"symbol": "btc", "quantity": 0.1, "average_entry": 50000})).json()
    btc = body["positions"][0]
    assert btc["symbol"] == "BTC" and btc["price"] == pytest.approx(60000) and btc["pnl"] == pytest.approx(1000)
    assert body["equity"] == pytest.approx(16000) and btc["allocation_pct"] == pytest.approx(37.5)
    assert any(s["label"] == "-10%" for s in btc["scenarios"])
    assert body["warnings"]  # 37.5% in one coin is flagged
    assert (await http.put("/api/portfolio/risk", json={"fee_pct": 5})).status_code == 422
    body = (await http.put("/api/portfolio/risk", json={"max_risk_per_signal_pct": 0.5, "fee_pct": 0.075})).json()
    assert body["risk_settings"]["max_risk_per_signal_pct"] == 0.5
    assert c.analysis.risk_params.max_risk_per_signal_pct == 0.5 and c.analysis.risk_params.fee_pct == 0.075
    c.portfolio.positions = {}
    await c.portfolio.load(force=True)  # persisted
    assert c.portfolio.positions["BTC"].quantity == pytest.approx(0.1) and c.portfolio.risk["fee_pct"] == 0.075
    assert (await http.delete("/api/portfolio/positions/BTC")).json()["positions"] == []


async def test_trade_plan_dca_ladder_and_scenarios(app_env):
    from tests.test_signal_engine import run

    http, c = app_env
    await http.put("/api/portfolio/cash", json={"cash": 20000})
    planned = analysis_out(run(), "ok")

    async def fake_analyze(symbol, force=False):
        return planned

    c.analysis.analyze = fake_analyze
    body = (await http.post("/api/portfolio/plan", json={"symbol": "ETH"})).json()
    plan = planned.plan
    assert body["signal"] == "STRONG BUY" and len(body["tranches"]) == 3
    assert body["budget"] == pytest.approx(20000 * plan.suggested_allocation_pct / 100)
    assert sum(t["amount"] for t in body["tranches"]) == pytest.approx(body["budget"])
    assert plan.entry_low <= body["average_entry"] <= plan.entry_high
    stop, *_, everything = body["scenarios"]
    assert stop["label"] == "stop hit" and stop["pnl"] < 0 and everything["pnl"] > 0
    assert body["loss_at_stop_pct_of_equity"] <= 1.05  # the per-signal risk limit (plus DCA averaging)
    custom = (await http.post("/api/portfolio/plan", json={"symbol": "ETH", "budget": 50000})).json()
    assert any("cash balance" in w for w in custom["warnings"]), custom


async def test_trade_plan_without_setup_is_rejected(app_env):
    http, _ = app_env
    r = await http.post("/api/portfolio/plan", json={"symbol": "LEO"})
    assert r.status_code == 422 and "no trade plan" in r.json()["detail"]
