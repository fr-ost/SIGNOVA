"""Phases 7-9: emergency stop, watchlist removal, pending scalp plans, strategy lab, ML filter, AI review."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.analysis import scalp as sc
from app.core.enums import Timeframe
from app.database import create_session_factory
from app.models import Base
from app.services.killswitch import KillSwitch, allowed_during_stop
from app.services.system_state import SystemStateStore
from tests.test_phase3_4 import settings_for
from tests.walk_market import WalkSpotAdapter, aggregate, walk


@pytest.fixture
async def env(tmp_path, request):
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
        c.news._http = c.onchain._http = c.sentiment._http = c.events._http = offline
        return c

    app = create_app(settings, container_factory=factory)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            await http.get("/api/market")
            yield http, app.state.container, sessions, app
    await engine.dispose()


# ----------------------------------------------------------------------------- emergency stop


def test_emergency_allowlist():
    assert allowed_during_stop("GET", "/api/control/status") and allowed_during_stop("POST", "/api/control/resume")
    assert allowed_during_stop("GET", "/api/signals") and allowed_during_stop("DELETE", "/api/watchlist/PEPE")
    assert allowed_during_stop("GET", "/static/dashboard.js") and allowed_during_stop("GET", "/")
    for method, path in (("GET", "/api/market"), ("GET", "/api/news"), ("POST", "/api/chat"),
                         ("POST", "/api/control/analyze"), ("GET", "/api/assets/BTC/analysis"),
                         ("POST", "/api/scalp/scan"), ("GET", "/api/sentiment"), ("GET", "/api/portfolio")):
        assert not allowed_during_stop(method, path), path


async def test_emergency_stop_halts_everything_and_survives_restart(env):
    http, c, sessions, _ = env
    await http.post("/api/control/auto", json={"minutes": 15})
    await http.post("/api/control/analyze")
    status = (await http.post("/api/control/kill", json={"reason": "test"})).json()
    assert status["emergency_stop"]["active"] is True and status["processing_state"] == "EMERGENCY_STOP"
    assert status["auto_minutes"] == 0 and status["scan"]["running"] is False
    blocked = await http.get("/api/market")
    assert blocked.status_code == 503 and blocked.json()["emergency_stop"] is True
    for method, path in (("get", "/api/news"), ("post", "/api/control/analyze"), ("get", "/api/assets/ETH/analysis"),
                         ("post", "/api/scalp/scan?horizon=1h"), ("get", "/api/onchain")):
        assert (await http.request(method.upper(), path)).status_code == 503, path
    assert c.controller.start_scan("auto") is False and c.scalp.start_scan("1h") is False
    assert (await http.get("/api/signals")).status_code == 200  # stored results stay readable
    assert (await http.get("/api/control/status")).json()["emergency_stop"]["active"] is True
    assert c.state.signal_paused_reason is not None
    # pressing Stop does not pretend the system is idle
    assert (await http.post("/api/control/stop")).json()["processing_state"] == "EMERGENCY_STOP"
    # a new process reads the switch back
    restored = KillSwitch(SystemStateStore(), sessions)
    await restored.load()
    assert restored.active and restored.reason == "test"
    resumed = (await http.post("/api/control/resume")).json()
    assert resumed["emergency_stop"]["active"] is False and resumed["processing_state"] == "IDLE"
    assert c.state.signal_paused_reason is None
    assert (await http.get("/api/market")).status_code == 200
    assert resumed["auto_minutes"] == 0  # nothing restarts on its own


@pytest.mark.parametrize("env", [{"admin_token": "tok"}], indirect=True)
async def test_emergency_stop_needs_the_token(env):
    http, _, _, _ = env
    assert (await http.post("/api/control/kill")).status_code == 401
    assert (await http.post("/api/control/kill", headers={"X-Admin-Token": "tok"})).json()["emergency_stop"]["active"]
    assert (await http.post("/api/control/resume")).status_code == 401


async def test_removing_a_watchlist_coin_also_leaves_the_selection(env):
    http, c, _, _ = env
    await http.post("/api/watchlist", json={"symbol": "PEPE"})
    await http.put("/api/control/selection", json={"mode": "selected", "symbols": ["BTC", "PEPE"]})
    removed = (await http.delete("/api/watchlist/PEPE")).json()
    assert removed["removed"] is True and removed["items"] == []
    sel = (await http.get("/api/control/selection")).json()
    assert sel["symbols"] == ["BTC"] and sel["mode"] == "selected"
    await http.post("/api/watchlist", json={"symbol": "PEPE"})
    await http.put("/api/control/selection", json={"mode": "selected", "symbols": ["PEPE"]})
    await http.delete("/api/watchlist/PEPE")
    assert (await http.get("/api/control/selection")).json()["mode"] == "all"  # never an empty selection
    assert (await http.delete("/api/watchlist/NOPE")).json()["removed"] is False


# ----------------------------------------------------------------------------- pending scalp plans


def _market(seed=3, n5=24000):
    c5 = walk(n5, seed=seed, end=datetime(2026, 3, 2, 9, 40, tzinfo=UTC))
    return {tf: aggregate(c5, tf) for tf in (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)}


@pytest.fixture(scope="module")
def market():
    return _market()


def _series(market, horizon):
    prof = sc.PROFILES[horizon]
    return sc.build_series(prof, market[prof.setup], market[prof.trend], market[prof.filter])


def test_pending_plans_exist_only_in_the_right_context(market):
    s = _series(market, "1h")
    p = sc.ScalpParams()
    seen = set()
    for i in range(sc.WARMUP, len(s)):
        pend = sc.pending_plan(s, i, p)
        context = s.trend_up[i] and not s.filter_down[i]
        if pend is None:
            continue
        assert context
        seen.add(pend.kind)
        assert pend.stop < pend.trigger < pend.tp1 < pend.tp2 and pend.risk_pct > 0 and pend.text
        assert pend.fees_ok == (pend.risk_pct >= p.min_risk_cost_multiple * p.cost_pct)
    assert "pullback" in seen and len(seen) >= 2


async def test_waiting_coins_show_conditional_levels(env):
    http, c, _, _ = env
    from app.services.scalp import _Work
    from tests.test_phase6 import _stats

    await http.put("/api/control/selection", json={"mode": "selected", "symbols": ["BTC", "ETH", "SOL", "ADA", "XRP", "DOGE"]})
    for horizon in ("4h", "1h"):
        await http.post("/api/scalp/scan", params={"horizon": horizon})
        await c.scalp.wait(horizon)
        res = (await http.get("/api/scalp", params={"horizon": horizon})).json()["result"]
        for sig in res["signals"]:
            assert sig["status"], sig["symbol"]
            if sig["plan"] is None and sig["pending"] is not None:
                w = sig["pending"]
                assert w["stop"] < w["trigger"] < w["tp1"] < w["tp2"] and "waiting" in sig["setup"]
                assert sig["if_triggered"] in ("STRONG BUY", "BUY", "WATCH", "NO TRADE")
    # the judging path for a waiting coin, deterministically
    asset = (await c.universe.get()).find("ETH")
    pend = sc.Pending("pullback", 3010.0, 2980.0, 3040.0, 3070.0, 1.0, True, "buy only if a 15m candle closes above 3010")
    work = _Work(asset, _stats([0.6, -1.0, 1.4, 0.5] * 6), None, ["no 15m trigger yet"], True, 3000.0, "USDT", True, [],
                 None, 1e9, 1.0, pending=pend)
    r = await c.scalp._judge(work, sc.PROFILES["1h"], c.scalp.params(), None)
    assert r.signal.value == "WATCH" and r.status == "waiting for trigger" and r.setup == "pullback (waiting)"
    assert r.pending["trigger"] == 3010.0 and r.if_triggered == "BUY" and r.reasons[0].startswith("no setup yet")
    expensive = sc.Pending("pullback", 3010.0, 3005.0, 3015.0, 3020.0, 0.1, False, "tight")
    work.pending = expensive
    assert (await c.scalp._judge(work, sc.PROFILES["1h"], c.scalp.params(), None)).if_triggered == "NO TRADE"
