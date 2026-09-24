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


# ----------------------------------------------------------------------------- strategy lab (phase 8)


def test_lab_grid_and_choice_rules():
    from app.analysis import lab

    assert len(lab.FILTERS) * len(lab.EXITS) == 36 and lab.BASELINE == ("base", "x1")
    p = lab.params_for(sc.ScalpParams(), "cost", "x5")
    assert p.min_risk_cost_multiple == 3.5 and p.tp1_share == 1.0 and p.tp1_r == 1.5 and p.variant == "cost/x5"
    c = sc.Candidate("pullback", 10, datetime(2026, 1, 1, tzinfo=UTC), 100.0, 99.0, 101.0, 102.0, 1.0, 0.8, [], level=101.2)
    r = sc.retarget(c, lab.params_for(sc.ScalpParams(), "base", "x3"))
    assert r.tp1 == pytest.approx(101.2 - 0.08) and r.tp2 == pytest.approx(103.0)  # TP1 stays under the swing high


def _lab_coins(n=5, horizon="1h"):
    from app.analysis import lab

    coins = []
    for seed in range(n):
        m = _market(seed=seed + 20)
        prof = sc.PROFILES[horizon]
        coins.append(lab.CoinSeries(f"C{seed}", sc.build_series(prof, m[prof.setup][-4000:], m[prof.trend], m[prof.filter]), False))
    return coins


def test_lab_splits_by_time_and_never_chooses_on_test_data():
    from app.analysis import lab

    coins = _lab_coins()
    r = lab.run_lab(coins, sc.ScalpParams(), "1h", datetime.now(UTC))
    assert len(r.combos) == 36 and r.split_time is not None and r.period_start < r.split_time < r.period_end
    min_trades = max(lab.MIN_TRAIN_TRADES, int(0.25 * next(c for c in r.combos if (c.filter, c.exit) == lab.BASELINE).train.trades))
    eligible = [c for c in r.combos if c.train.trades >= min_trades]
    if eligible:  # the choice is the best TRAINING expectancy, whatever the test data says
        best = max(eligible, key=lambda c: (c.train.expectancy_r, c.train.trades))
        assert r.chosen == (best.filter, best.exit)
    assert r.recommendation == (r.chosen if r.accepted else lab.BASELINE)
    chosen = next(c for c in r.combos if (c.filter, c.exit) == r.chosen)
    if r.accepted:  # accepted only with an edge on BOTH parts of the history
        assert chosen.train.expectancy_r >= lab.MIN_TRAIN_EXPECTANCY_R and chosen.test.expectancy_r > 0
        assert chosen.test.trades >= lab.MIN_TEST_TRADES
    elif (chosen.train.expectancy_r or 0) < lab.MIN_TRAIN_EXPECTANCY_R:
        assert any("older data" in reason for reason in r.reasons)
    assert r.reasons and (r.accepted or r.recommendation == lab.BASELINE)
    st = r.stats
    assert st["all"]["trades"] == st["train"]["trades"] + st["test"]["trades"]
    assert sum(v["trades"] for v in st["by_coin"].values()) == st["all"]["trades"]
    assert len(st["equity"]) <= 301 and set(st["by_hour_utc"]) <= {f"{h:02d}-{h + 4:02d}" for h in range(0, 24, 4)}
    assert r.model is not None and r.model.metrics["train_trades"] + r.model.metrics["test_trades"] == st["all"]["trades"]
    import json
    json.dumps(r.as_dict(), default=str)  # storable


async def test_lab_api_apply_reset_and_model_toggle(env):
    http, c, sessions, _ = env
    await http.put("/api/control/selection", json={"mode": "selected", "symbols": ["BTC", "ETH", "SOL", "ADA"]})
    view = (await http.get("/api/lab", params={"horizon": "1h"})).json()
    assert view["result"] is None and view["applied"]["label"] == "published rules" and len(view["filters"]) == 6
    started = (await http.post("/api/lab/run", params={"horizon": "1h"})).json()
    assert started["started"] is True
    await c.lab.wait("1h")
    view = (await http.get("/api/lab", params={"horizon": "1h"})).json()
    assert view["status"]["outcome"] == "completed", view["status"]
    res = view["result"]
    assert len(res["combos"]) == 36 and set(res["coins"]) == {"BTC", "ETH", "SOL", "ADA"} and res["applied"]
    # the scalp engine uses exactly what the lab applied
    if res["accepted"] and tuple(res["recommendation"]) != ("base", "x1"):
        assert c.scalp.params("1h").variant == "/".join(res["recommendation"])
    else:
        assert c.scalp.params("1h").variant == "default"
    # manual apply and reset
    applied = (await http.post("/api/lab/apply", params={"horizon": "1h"}, json={"filter": "adx", "exit": "x2"})).json()
    assert applied["applied"]["source"] == "manual" and c.scalp.params("1h").min_adx == 20.0 and c.scalp.params("1h").tp2_r == 3.0
    assert c.scalp.params("4h").min_adx == 0.0  # per horizon
    assert (await http.post("/api/lab/apply", params={"horizon": "1h"}, json={"filter": "nope", "exit": "x1"})).status_code == 422
    # restored after a restart
    from app.services.scalp import ScalpService
    from app.services.settings_store import SettingsStore
    fresh = ScalpService(c.settings, c.universe, c.assets, c.router, risk_params=lambda: c.analysis.risk_params,
                         store=SettingsStore(sessions))
    await fresh.load()
    assert fresh.variants["1h"] == ("adx", "x2") and ("1h" in fresh.models) == (res["model"] is not None)
    reset = (await http.post("/api/lab/reset", params={"horizon": "1h"})).json()
    assert reset["applied"]["label"] == "published rules" and c.scalp.params("1h").variant == "default"
    off = (await http.put("/api/ml", params={"horizon": "1h"}, json={"enabled": False})).json()
    assert off["enabled"] is False and c.scalp.active_model("1h") is None
    status = (await http.get("/api/control/status")).json()
    assert status["lab"]["1h"]["outcome"] == "completed"


async def test_lab_is_blocked_by_the_emergency_stop(env):
    http, c, _, _ = env
    await http.post("/api/control/kill")
    assert (await http.post("/api/lab/run", params={"horizon": "1h"})).status_code == 503
    assert c.lab.start("1h") is False
    assert (await http.get("/api/lab", params={"horizon": "1h"})).status_code == 200


# ----------------------------------------------------------------------------- statistical filter (phase 9)


def _rows(n, signal, seed, offset=0):
    import random

    from app.analysis.ml import Row

    rng = random.Random(seed)
    out = []
    for k in range(n):
        f = {name: rng.gauss(0, 1) for name in sc.FEATURES}
        edge = signal * f["rsi_change"]
        win = rng.random() < 1 / (1 + 2.718 ** (-edge))
        out.append(Row(f, 1.4 if win else -1.1, datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=k + offset)))
    return out


def test_ml_validates_a_real_pattern_and_rejects_noise():
    from app.analysis import ml

    now = datetime.now(UTC)
    good = ml.train_and_validate(_rows(900, 1.5, 1), _rows(400, 1.5, 2, 900), "1h", now)
    assert good.validated and good.metrics["test_auc"] > 0.7 and good.metrics["top_features"][0]["name"] == "rsi_change"
    assert good.metrics["test_kept_expectancy_r"] > good.metrics["test_expectancy_r"]
    noise = ml.train_and_validate(_rows(900, 0.0, 3), _rows(400, 0.0, 4, 900), "1h", now)
    assert not noise.validated and any("AUC" in r or "improvement" in r for r in noise.reasons)
    small = ml.train_and_validate(_rows(40, 1.5, 5), _rows(20, 1.5, 6, 40), "1h", now)
    assert not small.validated and any("training trades" in r for r in small.reasons)
    again = ml.Model.from_dict(good.as_dict())
    probe = _rows(1, 0, 9)[0].features
    assert again.probability(probe) == pytest.approx(good.probability(probe)) and again.validated
    assert ml.auc([0, 0, 1, 1], [0.1, 0.4, 0.35, 0.8]) == 0.75 and ml.auc([1, 1], [0.2, 0.3]) is None


def test_features_use_only_the_signal_candle_and_before(market):
    s = _series(market, "1h")
    i = len(s) - 200
    f = sc.features_at(s, i, "pullback", 1.0, 0.24)
    assert set(f) == set(sc.FEATURES) and all(isinstance(v, float) for v in f.values())
    prof = sc.PROFILES["1h"]
    cut_time = s.candles[i].close_time
    cut = sc.build_series(prof, [c for c in market[prof.setup] if c.close_time <= cut_time],
                          [c for c in market[prof.trend] if c.close_time <= cut_time],
                          [c for c in market[prof.filter] if c.close_time <= cut_time])
    assert sc.features_at(cut, len(cut) - 1, "pullback", 1.0, 0.24) == pytest.approx(f)


async def test_validated_model_filters_live_scalps(env):
    """A validated model caps a low-probability setup at WATCH; an unvalidated one never acts."""
    http, c, _, _ = env
    from app.analysis import ml
    from app.services.scalp import _Work
    from tests.test_phase6 import _stats
    from tests.test_signal_engine import book

    asset = (await c.universe.get()).find("ETH")
    now = datetime.now(UTC)
    cand = sc.Candidate("pullback", 100, now - timedelta(minutes=5), 3000.0, 2970.0, 3030.0, 3060.0, 1.0, 20.0, ["setup"])
    feats = {name: 0.0 for name in sc.FEATURES}
    work = _Work(asset, _stats([0.6, -1.0, 1.4, 0.5] * 12), cand, [], True, 3003.0, "USDT", True, [], book(3000.0), 1e9,
                 1.0, features=feats)
    model = ml.Model("1h", sorted(sc.FEATURES), [0.0] * len(sc.FEATURES), [1.0] * len(sc.FEATURES),
                     [-3.0] + [0.0] * len(sc.FEATURES), 0.5, True, {"test_auc": 0.6}, now, ["validated"])
    await c.scalp.set_model("1h", model)
    p = c.scalp.params("1h")
    low = await c.scalp._judge(work, sc.PROFILES["1h"], p, None)
    assert low.signal.value == "WATCH" and low.ml["used"] and low.ml["probability"] < 0.1
    assert any("statistical filter" in r for r in low.reasons)
    model.validated = False
    advisory = await c.scalp._judge(work, sc.PROFILES["1h"], p, None)
    assert advisory.signal.value in ("BUY", "STRONG BUY") and advisory.ml["used"] is False


# ----------------------------------------------------------------------------- AI review (phase 7)


def test_parse_review_is_strict():
    from app.services.ai_review import parse_review

    ok = parse_review('{"verdict": "Reject", "confidence": 3, "summary": "extended", "risks": ["a","b","c","d","e","f"]}')
    assert ok["verdict"] == "reject" and ok["confidence"] == 1.0 and len(ok["risks"]) == 5 and ok["valid_json"]
    odd = parse_review('Sure! {"verdict": "buy now", "summary": ""} thanks')
    assert odd["verdict"] == "caution" and not odd["valid_json"] and odd["summary"] == "no summary"
    assert parse_review("not json at all")["verdict"] == "caution"


def openai_review(verdict: str, calls: list):
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        calls.append(body)
        assert body["response_format"] == {"type": "json_object"} and "SIGNAL" in body["messages"][1]["content"]
        content = _json.dumps({"verdict": verdict, "confidence": 0.8, "summary": f"{verdict}: test",
                               "risks": ["resistance close"], "checks_before_entry": ["volume"]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    return handler


@pytest.mark.parametrize("env", [{"openai_api_key": "sk-test"}], indirect=True)
async def test_ai_review_advisory_and_filter_modes(env):
    http, c, sessions, _ = env
    calls: list = []
    c.chat._http = httpx.AsyncClient(transport=httpx.MockTransport(openai_review("reject", calls)))
    # a scalp buy to review
    c.scalp.results["1h"] = {"signals": [{"symbol": "ETH", "signal": "BUY", "reasons": ["setup"], "plan": {"entry": 1}}]}
    assert (await http.post("/api/ai/review", json={"kind": "scalp", "symbol": "SOL", "horizon": "1h"})).status_code == 404
    advisory = (await http.post("/api/ai/review", json={"kind": "scalp", "symbol": "eth", "horizon": "1h"})).json()
    assert advisory["verdict"] == "reject" and advisory["effect"] == "advisory only" and advisory["model"] == "gpt-5-mini"
    sig = c.scalp.results["1h"]["signals"][0]
    assert sig["signal"] == "BUY" and sig["ai_review"]["verdict"] == "reject"  # advisory: shown, not applied
    settings = (await http.put("/api/ai/settings", json={"mode": "filter"})).json()
    assert settings["mode"] == "filter"
    filtered = (await http.post("/api/ai/review", json={"kind": "scalp", "symbol": "ETH", "horizon": "1h"})).json()
    assert filtered["effect"].startswith("capped at WATCH")
    assert sig["signal"] == "WATCH" and sig["reasons"][0].startswith("AI reviewer rejected it")
    # swing review of an analysed coin
    swing = (await http.post("/api/ai/review", json={"kind": "swing", "symbol": "BTC"})).json()
    assert swing["kind"] == "swing" and c.analysis.cached("BTC").ai_review["verdict"] == "reject"
    assert (await http.get("/api/ai/settings")).json()["available"] is True
    assert (await http.put("/api/ai/settings", json={"mode": "yolo"})).status_code == 422


@pytest.mark.parametrize("env", [{"openai_api_key": "sk-test"}], indirect=True)
async def test_ai_review_is_stored_with_the_signal_and_counted_in_the_track_record(env):
    http, c, sessions, _ = env
    from app.models import Signal, SignalOutcome
    from tests.test_phase6 import _signal

    calls: list = []
    c.chat._http = httpx.AsyncClient(transport=httpx.MockTransport(openai_review("agree", calls)))
    sid = await _signal(sessions, symbol="ETH", strategy="scalp_1h", created=datetime.now(UTC) - timedelta(hours=1))
    c.scalp.results["1h"] = {"signals": [{"symbol": "ETH", "signal": "BUY", "reasons": [], "plan": None}]}
    await http.post("/api/ai/review", json={"kind": "scalp", "symbol": "ETH", "horizon": "1h"})
    async with sessions() as s:
        row = await s.get(Signal, sid)
        assert row.ai_output["verdict"] == "agree" and row.ai_confirmed and row.prompt_version == "review-1"
        s.add(SignalOutcome(signal_id=sid, outcome="TARGETS", exit_price=1, return_pct=1, r_multiple=1.2, mfe_pct=1,
                            mae_pct=0, time_to_outcome_seconds=600, hit_targets=[1, 2], evaluated_at=datetime.now(UTC)))
        row.status = "WIN"
        await s.commit()
    perf = (await http.get("/api/performance")).json()
    assert perf["by_ai"] == [{"verdict": "agree", "closed": 1, "win_rate": 100.0, "avg_r": 1.2, "total_r": 1.2}]
    assert perf["recent"][0]["ai_verdict"] == "agree"


@pytest.mark.parametrize("env", [{"openai_api_key": "sk-test"}], indirect=True)
async def test_auto_review_after_scans_and_the_emergency_stop(env):
    http, c, _, _ = env
    calls: list = []
    c.chat._http = httpx.AsyncClient(transport=httpx.MockTransport(openai_review("caution", calls)))
    result = {"signals": [{"symbol": s, "signal": "BUY", "reasons": [], "plan": None} for s in ("ETH", "SOL", "ADA", "XRP")]
              + [{"symbol": "DOGE", "signal": "WATCH", "reasons": [], "plan": None}]}
    c.scalp.results["1h"] = result
    assert c.ai_review.schedule_auto([("scalp", "ETH", "1h", result["signals"][0])]) is False  # auto is off
    await http.put("/api/ai/settings", json={"auto": True, "auto_max": 2})
    c.scalp.after_scan("1h", result)
    await c.ai_review._auto_task
    assert len(calls) == 2 and [s.get("ai_review", {}).get("verdict") for s in result["signals"]][:3] == ["caution", "caution", None]
    await http.post("/api/control/kill")
    assert (await http.post("/api/ai/review", json={"kind": "scalp", "symbol": "ETH", "horizon": "1h"})).status_code == 503
    assert c.ai_review.schedule_auto([("scalp", "ETH", "1h", result["signals"][0])]) is False


async def test_ai_review_without_a_key(env):
    http, c, _, _ = env
    c.scalp.results["1h"] = {"signals": [{"symbol": "ETH", "signal": "BUY", "reasons": [], "plan": None}]}
    r = await http.post("/api/ai/review", json={"kind": "scalp", "symbol": "ETH", "horizon": "1h"})
    assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]


def test_lab_decision_rule():
    from app.analysis.lab import ComboResult, SplitStats, choose

    def combo(f, e, train_exp, test_exp, train_n=100, test_n=40):
        return ComboResult(f, e, f"{f}/{e}", SplitStats(train_n, 50.0, train_exp, 1.2, train_exp * train_n),
                           SplitStats(test_n, 50.0, test_exp, 1.2, test_exp * test_n))

    # lost on the older data, won on the newer: luck, not accepted
    chosen, ok, reasons = choose([combo("base", "x1", -0.20, 0.05), combo("adx", "x2", -0.14, 0.11)])
    assert (chosen.filter, ok) == ("adx", False) and any("older data" in r for r in reasons)
    # an edge on both: accepted
    chosen, ok, reasons = choose([combo("base", "x1", 0.02, 0.01), combo("vol", "x3", 0.20, 0.12)])
    assert (chosen.filter, chosen.exit, ok) == ("vol", "x3", True) and reasons[0].startswith("chosen on the older 70%")
    # good in training, lost on newer data: not accepted
    assert choose([combo("base", "x1", 0.02, 0.01), combo("vol", "x3", 0.30, -0.10)])[1] is False
    # worse than the published rules on newer data: not accepted
    assert choose([combo("base", "x1", 0.10, 0.20), combo("vol", "x3", 0.30, 0.12)])[1] is False
    # too few newer trades: not accepted; tiny training samples are never chosen over the baseline group
    assert choose([combo("base", "x1", 0.10, 0.20), combo("vol", "x3", 0.30, 0.25, test_n=5)])[1] is False
    chosen, _, _ = choose([combo("base", "x1", 0.10, 0.20, train_n=200), combo("brk", "x5", 2.0, 2.0, train_n=3)])
    assert chosen.filter == "base"


@pytest.mark.skipif(not __import__("os").getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
async def test_postgres_settings_kill_switch_and_ai_output():
    """app_settings JSON (lab results, models), the kill switch and ai_output on a real PostgreSQL."""
    import os

    from app.config import Settings
    from app.models import Signal
    from app.services.killswitch import KEY
    from app.services.settings_store import SettingsStore

    settings = Settings(_env_file=None, database_url=os.environ["TEST_DATABASE_URL"], json_logs=False)
    engine = create_async_engine(settings.async_database_url, connect_args=settings.database_connect_args)

    async def cleanup():
        async with engine.begin() as conn:
            await conn.exec_driver_sql("DELETE FROM app_settings WHERE key IN ('pgt_lab', 'emergency_stop')")
            await conn.exec_driver_sql("DELETE FROM signals WHERE symbol = 'PGA'")

    await cleanup()
    sessions = create_session_factory(engine)
    lab = {"accepted": True, "chosen": {"filter": "adx", "exit": "x3"}, "test": {"expectancy_r": 0.12, "pf": None},
           "equity": [[1, 0.5], [2, -0.25]], "reasons": ["ok"]}
    await SettingsStore(sessions).set("pgt_lab", lab)
    assert await SettingsStore(sessions).get("pgt_lab") == lab  # fresh store: read back from the database

    kill = KillSwitch(SystemStateStore(), sessions)
    await kill.engage("pg test")
    state = SystemStateStore()
    restored = KillSwitch(state, sessions)
    await restored.load()
    assert restored.active and restored.reason == "pg test" and state.emergency_stop
    await restored.release()
    again = KillSwitch(SystemStateStore(), sessions)
    await again.load()
    assert not again.active and KEY == "emergency_stop"

    async with sessions() as s:
        s.add(Signal(symbol="PGA", timeframe="1h", strategy="scalp_1h", signal="BUY", signal_score=60,
                     data_health_score=100, data_state="HEALTHY", status="OPEN", reasons=[], risks=[],
                     input_features={}, quant_output={}, ai_output={"verdict": "reject", "risks": ["thin book"]},
                     created_at=datetime.now(tz=UTC)))
        await s.commit()
    async with sessions() as s:
        from sqlalchemy import select

        row = (await s.execute(select(Signal).where(Signal.symbol == "PGA"))).scalar_one()
        assert row.ai_output == {"verdict": "reject", "risks": ["thin book"]}
    await cleanup()
    await engine.dispose()
