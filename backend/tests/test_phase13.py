"""Phase 13: Signova AI analyst (OpenAI decides; the dashboard checks, sizes, stores and tracks)."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.analysis import ai_analyst as aa
from app.core.enums import SignalLabel, Timeframe
from app.models import Signal
from app.services.ai_analyst import best_models
from app.services.outcomes import NOFILL, OutcomeTracker, _fill
from tests.test_phase6 import bar
from tests.test_phase7_9 import env  # noqa: F401  (fixture)

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)


def decision(**kw):
    base = {"decision": "LONG", "bias": "bullish", "conviction": 70, "setup": "pullback", "entry_type": "market",
            "entry": 100.0, "entry_zone_low": None, "entry_zone_high": None, "stop_loss": 97.0, "take_profit_1": 104.0,
            "take_profit_2": 108.0, "expected_hold_hours": 6, "entry_valid_hours": None,
            "probability_tp1_before_stop": 0.58, "thesis": "t", "market_context": "", "trend_and_structure": "",
            "key_levels": "", "positioning_and_flow": "", "news_and_catalysts": "", "confluences": ["a"],
            "risks": ["b"], "invalidation": "below 97", "what_to_watch": [], "no_trade_reason": None}
    base.update(kw)
    return aa.parse_decision(json.dumps(base))


def check(d, **kw):
    args = {"market": "futures", "horizon": "1h", "price": 100.0, "atr": 1.0, "cost_pct": 0.2, "now": NOW} | kw
    return aa.check_plan(d, **args)


def test_best_model_is_the_newest_flagship_on_the_key():
    account = ["gpt-4o", "gpt-5", "gpt-5.1", "gpt-5-mini", "gpt-5.2-chat-latest", "o3", "gpt-4.1", "gpt-5-pro"]
    assert best_models(account) == ["gpt-5.1", "gpt-5", "o3", "gpt-4.1", "gpt-4o"]
    assert best_models(["gpt-4o-mini", "gpt-4.1"]) == ["gpt-4.1"]
    assert best_models(None)[0] == "gpt-5"


def test_parse_decision_is_defensive():
    d = aa.parse_decision("the model rambled {not json")
    assert d.decision == "NO_TRADE" and not d.valid_json
    d = aa.parse_decision('{"decision": "short", "conviction": 140, "probability_tp1_before_stop": 62, "entry": "nan"}')
    assert d.decision == "SHORT" and d.conviction == 100 and d.probability_tp1_before_stop == pytest.approx(0.62)
    assert d.entry is None and d.entry_type == "market"


def test_plan_checks_never_invent_a_trade():
    c = check(decision())
    assert c.label == SignalLabel.BUY and c.side == "long" and c.status == "trade"
    assert c.plan["entry"] == 100.0 and c.plan["reward_risk_final"] > 2 and c.plan["hold_hours"] == 6
    assert check(decision(conviction=80)).label == SignalLabel.STRONG_BUY
    assert check(decision(decision="NO_TRADE", no_trade_reason="chop")).label == SignalLabel.NO_TRADE
    # spot cannot short
    assert check(decision(decision="SHORT", stop_loss=103.0, take_profit_1=96.0, take_profit_2=92.0),
                 market="spot").label == SignalLabel.NO_TRADE
    # a short on futures is fine
    s = check(decision(decision="SHORT", stop_loss=103.0, take_profit_1=96.0, take_profit_2=92.0))
    assert s.label == SignalLabel.BUY and s.side == "short" and aa.label_text("futures", s.side, s.label) == "SHORT"
    # checks that turn a plan into WATCH, with the reason
    tight = check(decision(stop_loss=99.8, take_profit_1=100.6, take_profit_2=101.2))
    assert tight.label == SignalLabel.WATCH and any("too tight" in n for n in tight.notes)
    small = check(decision(take_profit_1=101.0, take_profit_2=102.0))
    assert small.label == SignalLabel.WATCH and any("reward:risk" in n for n in small.notes)
    weak = check(decision(conviction=50))
    assert weak.label == SignalLabel.WATCH and any("conviction" in n for n in weak.notes)
    # the price is already beyond the stop
    assert check(decision(), price=96.0).label == SignalLabel.NO_TRADE
    # a limit order below the price waits for its level; a "limit" above the price is a market order
    lim = check(decision(entry_type="limit", entry=99.0, stop_loss=97.0, take_profit_1=103.0, take_profit_2=106.0))
    assert lim.status == "wait" and lim.plan["entry"] == 99.0 and lim.plan["entry_valid_until"] is not None
    assert check(decision(entry_type="limit", entry=100.5)).plan["entry_type"] == "market"
    far = check(decision(entry_type="limit", entry=94.0, stop_loss=92.0, take_profit_1=99.0, take_profit_2=104.0))
    assert far.label == SignalLabel.WATCH and any("ATR from the price" in n for n in far.notes)


def test_risk_manager_can_only_lower():
    c = check(decision(conviction=80))
    c2, conv = aa.apply_review(c, 80, {"verdict": "reduce", "adjusted_conviction": 65, "summary": "s", "issues": []})
    assert c2.label == SignalLabel.BUY and conv == 65
    c3, _ = aa.apply_review(check(decision()), 70, {"verdict": "reject", "adjusted_conviction": 20, "summary": "no", "issues": []})
    assert c3.label == SignalLabel.WATCH and "rejected" in c3.notes[0]
    c4, conv = aa.apply_review(check(decision()), 70, {"verdict": "approve", "adjusted_conviction": 95, "summary": "", "issues": []})
    assert c4.label == SignalLabel.BUY and conv == 70  # never raised


def test_limit_entries_count_only_once_filled():
    t0 = NOW
    flat = [bar(k, 100, 100.5, 99.5, 100, Timeframe.M15, t0) for k in range(3)]
    dip = [bar(3, 100, 100.2, 98.8, 99.2, Timeframe.M15, t0)]  # touches the 99 limit
    assert _fill(flat + dip, 0, 99.0, 97.0, "limit", 8, 103.0) == (4, 99.0)
    assert _fill(flat, 0, 99.0, 97.0, "limit", 8, 103.0) is None  # still waiting
    assert _fill(flat, 0, 99.0, 97.0, "limit", 3, 103.0) == NOFILL  # expired
    rally = [bar(3, 100, 103.5, 99.9, 103, Timeframe.M15, t0)]
    assert _fill(flat + rally, 0, 99.0, 97.0, "limit", 8, 103.0) == NOFILL  # the move left without the order
    brk = [bar(3, 100, 101.6, 99.8, 101.4, Timeframe.M15, t0)]
    assert _fill(flat + brk, 0, 101.0, 99.0, "stop", 8, 104.0) == (4, 101.0)


def _openai(calls: list):
    """A fake OpenAI: lists models, and answers the analyst with a plan built from the dossier's own numbers."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in ("gpt-4o", "gpt-5", "gpt-5.1", "gpt-5-mini")]})
        body = json.loads(request.content)
        calls.append(body)
        name = body["response_format"]["json_schema"]["name"]
        user = body["messages"][1]["content"]
        dossier = json.loads(user.split("DOSSIER:\n", 1)[1].split("\n\nPLAN:", 1)[0])
        if name == "risk_review":
            content = {"verdict": "approve", "adjusted_conviction": 70, "summary": "sound", "issues": []}
        else:
            price = dossier["coin"]["price"]
            atr = dossier["timeframes"][0]["indicators"]["atr14"]
            content = {"decision": "LONG", "bias": "bullish", "conviction": 72, "setup": "trend pullback",
                       "entry_type": "market", "entry": price, "entry_zone_low": None, "entry_zone_high": None,
                       "stop_loss": price - 2 * atr, "take_profit_1": price + 3 * atr, "take_profit_2": price + 6 * atr,
                       "expected_hold_hours": 5, "entry_valid_hours": None, "probability_tp1_before_stop": 0.57,
                       "thesis": "uptrend, pullback held the EMA20", "market_context": "calm", "trend_and_structure": "HH/HL",
                       "key_levels": "support below", "positioning_and_flow": "neutral", "news_and_catalysts": "none",
                       "confluences": ["trend", "level"], "risks": ["BTC weakness"], "invalidation": "close below the stop",
                       "what_to_watch": ["volume"], "no_trade_reason": None}
            if dossier["coin"]["symbol"] == "SOL":
                content |= {"decision": "SHORT", "bias": "bearish", "stop_loss": price + 2 * atr,
                            "take_profit_1": price - 3 * atr, "take_profit_2": price - 6 * atr}
        usage = {"prompt_tokens": 9000, "completion_tokens": 3000, "completion_tokens_details": {"reasoning_tokens": 2500}}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}],
                                         "usage": usage})

    return handler


@pytest.mark.parametrize("env", [{"openai_api_key": "sk-test"}], indirect=True)
async def test_single_token_ai_analysis_spot_and_futures(env):  # noqa: F811
    http, c, sessions, _ = env
    calls: list = []
    c.chat._http = httpx.AsyncClient(transport=httpx.MockTransport(_openai(calls)))
    # multi-coin AI scans are gone: the engines do those
    assert (await http.post("/api/ai/analyst/scan", params={"market": "futures", "horizon": "1h"})).status_code in (404, 405)
    r = await http.post("/api/ai/analyst/token", params={"symbol": "eth", "horizon": "1h"})
    assert r.status_code == 200 and r.json()["started"] is True
    assert (await http.post("/api/ai/analyst/token", params={"symbol": "BTC", "horizon": "1h"})).json()["started"] is False  # one at a time
    await c.analyst.wait()
    body = (await http.get("/api/ai/analyst", params={"symbol": "ETH", "horizon": "1h"})).json()
    assert body["status"]["outcome"] == "completed", body["status"]
    res = body["result"]
    assert res["symbol"] == "ETH" and res["model"] == "gpt-5.1" and res["usage"]["calls"] == 2  # one analysis + one risk review
    fut, spot = res["futures"], res["spot"]
    assert fut["label"] in ("LONG", "STRONG LONG") and spot["label"] in ("BUY", "STRONG BUY")
    assert fut["leverage"]["liquidation_price"] < fut["plan"]["stop"] < fut["plan"]["entry"]
    assert spot["sizing"]["allocation_pct"] > 0 and spot["cost_pct"] > fut["cost_pct"] - 0.2
    assert res["review"]["verdict"] == "approve"
    assert body["history"][0]["symbol"] == "ETH"
    first = calls[0]
    assert first["model"] == "gpt-5.1" and first["reasoning_effort"] == "high"
    assert first["response_format"]["json_schema"]["strict"] is True
    dossier = json.loads(first["messages"][1]["content"].split("DOSSIER:\n", 1)[1])
    assert dossier["task"]["market"] == "both" and set(dossier["task"]["round_trip_cost_pct"]) == {"spot", "futures"}
    assert [t["timeframe"] for t in dossier["timeframes"]] == ["15m", "1H", "4H", "1D"]
    assert len(dossier["timeframes"][0]["candles"]["rows"]) == 72 and "structure" in dossier["timeframes"][1]
    async with sessions() as s:
        rows = (await s.execute(select(Signal).where(Signal.symbol == "ETH", Signal.strategy.like("ai_%")))).scalars().all()
        assert {r.strategy for r in rows} == {"ai_spot_1h", "ai_futures_1h"} and all(r.status == "OPEN" for r in rows)
        fut_row = next(r for r in rows if r.strategy == "ai_futures_1h")
        assert fut_row.quant_output["entry_type"] == "market" and fut_row.model_name == "gpt-5.1"
    # the same token again does not store the open trades twice
    assert c.analyst.start_token("ETH", "1h")
    await c.analyst.wait()
    async with sessions() as s:
        again = (await s.execute(select(Signal).where(Signal.symbol == "ETH", Signal.strategy.like("ai_%")))).scalars().all()
        assert len(again) == 2
    # a short: futures only, spot has no trade
    assert c.analyst.start_token("SOL", "1h")
    await c.analyst.wait()
    sol = c.analyst.result("SOL", "1h")
    assert sol["futures"]["label"] in ("SHORT", "STRONG SHORT") and sol["spot"]["signal"] == "NO TRADE"
    assert "spot cannot be shorted" in sol["spot"]["notes"][0]
    # the track record follows the futures long: a later rally hits the targets
    tracker = OutcomeTracker(sessions)
    plan = fut["plan"]
    t = fut_row.created_at.replace(tzinfo=UTC) - timedelta(minutes=15)
    t = t - timedelta(minutes=t.minute % 15, seconds=t.second, microseconds=t.microsecond)
    e, tp2 = plan["entry"], plan["tp2"]
    candles = [bar(k, e, e * 1.001, e * 0.999, e, Timeframe.M15, t) for k in range(2)]
    candles += [bar(2, e, tp2 * 1.01, e * 0.999, tp2, Timeframe.M15, t)]
    assert await tracker.update("ETH", {Timeframe.M15: candles}) == 2  # the spot buy and the futures long
    perf = (await http.get("/api/performance")).json()
    assert "AI futures 1h" in json.dumps(perf) and "AI spot 1h" in json.dumps(perf)


@pytest.mark.parametrize("env", [{"openai_api_key": "sk-test"}], indirect=True)
async def test_ai_analyst_settings_models_and_emergency_stop(env):  # noqa: F811
    http, c, _, _ = env
    c.chat._http = httpx.AsyncClient(transport=httpx.MockTransport(_openai([])))
    s = (await http.put("/api/ai/analyst/settings", json={"effort": "medium", "min_conviction": 65, "review": False})).json()
    assert s["effort"] == "medium" and s["min_conviction"] == 65 and s["review"] is False
    assert (await http.put("/api/ai/analyst/settings", json={"effort": "extreme"})).status_code == 422
    models = (await http.get("/api/ai/analyst/models")).json()
    assert models["order"][0] == "gpt-5.1"
    await http.post("/api/control/kill")
    assert (await http.post("/api/ai/analyst/token", params={"symbol": "ETH", "horizon": "4h"})).status_code == 503
    assert (await http.get("/api/ai/analyst", params={"symbol": "ETH", "horizon": "4h"})).status_code == 200
    await http.post("/api/control/resume")


async def test_ai_analyst_needs_an_openai_key(env):  # noqa: F811
    http, _, _, _ = env
    r = await http.post("/api/ai/analyst/token", params={"symbol": "ETH", "horizon": "4h"})
    assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]
