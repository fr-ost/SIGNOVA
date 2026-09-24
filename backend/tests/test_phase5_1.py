"""Phase 5.1: CoinGecko key, CoinPaprika free plan, chat models, coin selection, unlocks, airdrops."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.data.health import ProviderHealthRegistry
from app.data.providers.coingecko import CoinGeckoClient
from app.data.providers.coinpaprika import CoinPaprikaClient
from app.models import Base
from app.services.chat import ChatService
from app.services.events import parse_airdrops, parse_release_schedule
from app.services.selection import ScanSelectionService
from tests.test_api import _make_client
from tests.test_phase5 import chain_handler
from tests.test_phase3_4 import settings_for

NOW = datetime.now(tz=UTC)


# ----------------------------------------------------------------------------- CoinGecko, CoinPaprika


def test_coingecko_key_settings():
    keyless = Settings(_env_file=None)
    assert keyless.coingecko_headers == {} and keyless.coingecko_url == "https://api.coingecko.com/api/v3"
    demo = Settings(_env_file=None, coingecko_api_key="CG-demo")
    assert demo.coingecko_headers == {"x-cg-demo-api-key": "CG-demo"} and demo.coingecko_url == "https://api.coingecko.com/api/v3"
    pro = Settings(_env_file=None, coingecko_api_key="CG-pro", coingecko_plan="pro")
    assert pro.coingecko_headers == {"x-cg-pro-api-key": "CG-pro"} and pro.coingecko_url.startswith("https://pro-api.coingecko.com")
    assert "CG-demo" in demo.secret_values()


async def test_coingecko_client_sends_the_key_as_a_header_not_in_the_url():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = CoinGeckoClient("https://api.coingecko.com/api/v3", http, ProviderHealthRegistry(),
                                 headers={"x-cg-demo-api-key": "CG-secret"})
        await client.coins_markets(per_page=5)
    assert seen[0].headers["x-cg-demo-api-key"] == "CG-secret" and "CG-secret" not in str(seen[0].url)
    assert client.keyed


async def test_coinpaprika_uses_free_plan_url_and_a_long_timeout():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = CoinPaprikaClient(Settings(_env_file=None).coinpaprika_base_url, http, ProviderHealthRegistry(), timeout_seconds=30)
        assert await client.tickers() == []
    assert str(seen[0].url).startswith("https://api.coinpaprika.com/v1/tickers?quotes=USD")
    assert seen[0].extensions["timeout"]["read"] == 30


# ----------------------------------------------------------------------------- chat models


def chat_service(handler, **extra):
    settings = Settings(_env_file=None, openai_api_key="sk-test", **extra)

    async def context(symbol):
        return {"symbols_in_context": []}

    return ChatService(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)), context)


def test_reasoning_models_get_a_reasoning_budget():
    svc = chat_service(lambda r: httpx.Response(500))
    assert svc.is_reasoning("gpt-5-mini") and svc.is_reasoning("o4-mini") and not svc.is_reasoning("gpt-4o-mini")
    p = svc._payload("gpt-5-mini", [], None)
    assert p["reasoning_effort"] == "low" and p["max_completion_tokens"] == 1500 + 6000
    assert svc._payload("o4-mini", [], "minimal")["reasoning_effort"] == "low"  # o-series has no minimal
    plain = svc._payload("gpt-4o-mini", [], "high")
    assert "reasoning_effort" not in plain and plain["max_completion_tokens"] == 1500


async def test_empty_reasoning_answer_falls_back_instead_of_showing_no_text():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if body["model"] == "gpt-5-mini":
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Real answer"}, "finish_reason": "stop"}]})

    out = await chat_service(handler).reply([{"role": "user", "content": "hi"}])
    assert out["reply"] == "Real answer" and out["model"] == "gpt-4o-mini"
    assert "reasoning" in out["fallback_from"][0]
    assert calls[0]["reasoning_effort"] == "low"


async def test_user_selected_model_and_effort_are_used_first():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    out = await chat_service(handler).reply([{"role": "user", "content": "hi"}], model="gpt-5", reasoning_effort="medium")
    assert out["model"] == "gpt-5" and calls[0]["model"] == "gpt-5" and calls[0]["reasoning_effort"] == "medium"


async def test_available_models_marks_what_the_key_can_use():
    def handler(request):
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-5-mini"}]})

    out = await chat_service(handler).available_models()
    by_id = {m["id"]: m for m in out["models"]}
    assert out["account_checked"] and by_id["gpt-5-mini"]["available"] and by_id["gpt-5"]["available"] is False
    assert out["default"] == "gpt-5-mini" and "minimal" in out["efforts"]


# ----------------------------------------------------------------------------- app fixture


def events_handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "api.mobula.io" and path == "/api/1/metadata":
        assert request.headers["Authorization"] == "mobula-secret"
        asset = request.url.params.get("asset")
        if asset == "Eth":
            return httpx.Response(200, json={"data": {"name": "Ethereum", "release_schedule": []}})
        if asset == "Sol":
            return httpx.Response(404, json={"error": "not found"})
        if request.url.params.get("symbol") == "SOL":
            soon = int((NOW + timedelta(days=5)).timestamp() * 1000)
            later = int((NOW + timedelta(days=60)).timestamp() * 1000)
            past = int((NOW - timedelta(days=5)).timestamp() * 1000)
            return httpx.Response(200, json={"data": {
                "name": "Solana", "circulating_supply": 1_000_000, "price": 100,
                "release_schedule": [
                    {"unlock_date": soon, "tokens_to_unlock": 30_000, "allocation_details": {"Team": 20_000, "Investors": 10_000}},
                    {"unlock_date": later, "tokens_to_unlock": 5_000, "allocation_details": {}},
                    {"unlock_date": past, "tokens_to_unlock": 99_000},
                ]}})
        return httpx.Response(200, json={"data": {"release_schedule": []}})
    if host == "alphadrops.net" and path == "/api/v1/airdrops":
        assert request.headers["Authorization"] == "Bearer drops-secret"
        status = request.url.params.get("status")
        if status == "upcoming":
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"data": [
            {"name": f"Project {status}", "status": status, "chains": ["Ethereum", {"name": "Base"}], "slug": f"project-{status}",
             "end_date": "2026-12-01T00:00:00Z", "estimated_value": "$500"},
            {"title": "Shared Drop", "link": "https://example.org/drop", "ecosystem": "Solana"},
            {"no_name": True},
        ]})
    return chain_handler(request)


@pytest.fixture
async def env51(tmp_path, request):
    extra = getattr(request, "param", {})
    settings = settings_for(tmp_path, **extra)
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app, sessions = await _make_client(settings, engine)
    async with app.router.lifespan_context(app):
        c = app.state.container
        c.news._trending = None
        mock = httpx.AsyncClient(transport=httpx.MockTransport(events_handler))
        c.news._http = c.onchain._http = c.sentiment._http = c.events._http = mock
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            await http.get("/api/market")
            yield http, c, sessions
    await engine.dispose()


# ----------------------------------------------------------------------------- coin selection


async def test_scan_analyses_only_the_selected_coins(env51):
    http, c, sessions = env51
    status = (await http.get("/api/control/selection")).json()
    assert status["mode"] == "all" and len(status["universe"]) == 20 and all(u["selected"] for u in status["universe"])
    bad = await http.put("/api/control/selection", json={"mode": "selected", "symbols": []})
    assert bad.status_code == 422
    assert (await http.put("/api/control/selection", json={"mode": "selected", "symbols": ["bad-symbol!"]})).status_code == 422
    r = (await http.put("/api/control/selection", json={"mode": "selected", "symbols": ["btc", "ETH", "SOL", "ETH"]})).json()
    assert r["symbols"] == ["BTC", "ETH", "SOL"] and sum(u["selected"] for u in r["universe"]) == 3
    await http.post("/api/control/analyze")
    await c.controller.wait()
    scan = (await http.get("/api/signals")).json()
    assert sorted(s["symbol"] for s in scan["signals"]) == ["BTC", "ETH", "SOL"] and not scan["errors"]
    assert (await http.get("/api/control/status")).json()["selection"]["mode"] == "selected"

    # persisted: a fresh service reads the same selection
    fresh = ScanSelectionService(sessions)
    await fresh.load()
    assert fresh.mode == "selected" and fresh.symbols == ["BTC", "ETH", "SOL"]

    # a coin added to the watchlist joins the active selection
    await http.post("/api/watchlist", json={"symbol": "pepe"})
    assert "PEPE" in (await http.get("/api/control/selection")).json()["symbols"]

    # a selection that matches nothing says so instead of silently scanning nothing
    await http.put("/api/control/selection", json={"mode": "selected", "symbols": ["NOPE"]})
    await http.post("/api/control/analyze")
    await c.controller.wait()
    empty = (await http.get("/api/signals")).json()
    assert empty["signals"] == [] and "selected coins" in empty["errors"][0]

    back = (await http.put("/api/control/selection", json={"mode": "all"})).json()
    assert back["mode"] == "all" and all(u["selected"] for u in back["universe"])


@pytest.mark.parametrize("env51", [{"admin_token": "tok"}], indirect=True)
async def test_selection_changes_need_the_admin_token(env51):
    http, _, _ = env51
    assert (await http.put("/api/control/selection", json={"mode": "all"})).status_code == 401
    assert (await http.get("/api/control/selection")).status_code == 200
    assert (await http.get("/api/chat/models")).status_code == 401


# ----------------------------------------------------------------------------- unlocks, airdrops


def test_parse_release_schedule_and_airdrops():
    soon = int((NOW + timedelta(days=3)).timestamp())  # seconds also accepted
    coin = parse_release_schedule(
        {"data": {"circulating_supply": 200, "release_schedule": [
            {"unlock_date": soon, "tokens_to_unlock": 10, "allocation_details": {"A": 6, "B": 4, "C": 0}},
            {"unlock_date": "junk", "tokens_to_unlock": 5},
        ]}},
        "ABC", "Abc", 2.0, NOW, 30,
    )
    assert coin.window_tokens == 10 and coin.window_value_usd == 20 and coin.window_pct_circulating == 5.0
    assert coin.next_unlock.allocations == {"A": 6.0, "B": 4.0} and 2.9 <= coin.next_unlock.days_until <= 3.0
    empty = parse_release_schedule({"data": {}}, "X", "X", None, NOW, 30)
    assert empty.upcoming == [] and empty.window_pct_circulating == 0.0
    drops = parse_airdrops({"airdrops": [{"name": "A", "slug": "a-b", "chains": "Base"}, {"name": "B", "url": "javascript:x"}]})
    assert drops[0].url == "https://alphadrops.net/airdrops/a-b" and drops[0].chains == ["Base"] and drops[1].url is None


async def test_events_without_keys_say_what_to_set(env51):
    http, _, _ = env51
    unlocks = (await http.get("/api/events/unlocks")).json()
    assert unlocks["configured"] is False and "MOBULA_API_KEY" in unlocks["message"]
    drops = (await http.get("/api/events/airdrops")).json()
    assert drops["configured"] is False and "ALPHADROPS_API_KEY" in drops["message"]


@pytest.mark.parametrize("env51", [{"mobula_api_key": "mobula-secret", "alphadrops_api_key": "drops-secret"}], indirect=True)
async def test_unlocks_airdrops_and_unlock_risk_notes(env51):
    http, c, _ = env51
    r = await http.get("/api/events/unlocks")
    assert "mobula-secret" not in r.text
    unlocks = r.json()
    assert unlocks["configured"] and len(unlocks["checked"]) == 20
    sol = next(x for x in unlocks["coins"] if x["symbol"] == "SOL")
    assert sol["window_tokens"] == 30_000 and sol["window_pct_circulating"] == 3.0 and len(sol["upcoming"]) == 2
    assert sol["next_unlock"]["allocations"] == {"Team": 20_000.0, "Investors": 10_000.0}
    assert c.events.notes_for("SOL") and "3.00% of circulating supply" in c.events.notes_for("SOL")[0]
    assert c.events.notes_for("ETH") == []

    analysis = (await http.get("/api/assets/SOL/analysis")).json()
    assert any(r.startswith("token unlock in") for r in analysis["risks"])

    drops_r = await http.get("/api/events/airdrops")
    assert "drops-secret" not in drops_r.text
    drops = drops_r.json()
    names = [d["name"] for d in drops["airdrops"]]
    assert names == ["Project active", "Shared Drop", "Project claimable"]  # active first, deduplicated
    assert drops["airdrops"][0]["chains"] == ["Ethereum", "Base"] and drops["airdrops"][0]["url"].endswith("/project-active")
    assert any("upcoming" in e and "429" in e for e in drops["errors"])


@pytest.mark.skipif(not __import__("os").getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
async def test_postgres_selection_persistence():
    """app_settings (migration 0003) on a real PostgreSQL."""
    import os

    from app.database import create_session_factory

    settings = Settings(_env_file=None, database_url=os.environ["TEST_DATABASE_URL"], json_logs=False)
    engine = create_async_engine(settings.async_database_url, connect_args=settings.database_connect_args)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DELETE FROM app_settings")
    sessions = create_session_factory(engine)
    first = ScanSelectionService(sessions)
    await first.set("selected", ["btc", "eth"])
    await first.set("selected", ["sol"])  # update in place
    second = ScanSelectionService(sessions)
    await second.load()
    assert second.mode == "selected" and second.symbols == ["SOL"]
    await engine.dispose()
