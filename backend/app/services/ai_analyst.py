"""Signova AI analyst service (Phase 13): OpenAI reads every coin's full data and decides like a trader.

A scan runs only on request, for one market (spot or futures) and one horizon, over the coins you selected.
For each coin it does four things:

1. Gathers everything the dashboard knows:
   - validated candles on 2-4 timeframes, with indicators, regimes, structure and levels;
   - order book, futures positioning and liquidations;
   - the evidence board;
   - news with the AI news reading, sentiment and unlocks;
   - the market backdrop and Bitcoin;
   - what the rule engines see (the futures engine's research for this horizon, run first when it is stale).
2. Asks the best OpenAI model on your key (or the one you choose) for a decision, at high reasoning effort.
3. For a proposed trade, asks the model again as an independent risk manager. The risk manager can lower
   the trade or reject it, never raise it.
4. Checks that the plan can be executed and is worth its costs (app.analysis.ai_analyst.check_plan),
   sizes it (spot allocation, or a futures leverage plan with the liquidation beyond the stop) and stores
   it. The track record then follows it like every other signal, including limit and stop entries, which
   count only once they fill.

Coins that fail the data-integrity gate, and futures coins without a perpetual, are not sent to the
model (no cost, NO TRADE). Nothing runs in the background, and the emergency stop halts a scan.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis import ai_analyst as aa
from app.analysis import futures as fu
from app.analysis.features import compute_snapshot
from app.config import Settings
from app.core.enums import SignalLabel, Timeframe
from app.core.formatting import fmt_price
from app.core.timeutil import utcnow
from app.models import Signal, SignalTarget
from app.services.chat import ChatService, ChatUnavailable
from app.services.scalp import _clean, _jsonable
from app.services.settings_store import SettingsStore
from app.services.universe import UniverseAsset

log = logging.getLogger(__name__)

ANALYST_VERSION = "ai-analyst-1.0.0"
EFFORTS = ("low", "medium", "high")
FLAGSHIP = re.compile(r"^gpt-(\d+)(?:\.(\d+))?$")  # gpt-5, gpt-5.1, ... (not -mini, -nano, -chat, -pro variants)
FALLBACK_MODELS = ("o3", "gpt-4.1", "gpt-4o")
DEFAULT_MODELS = ("gpt-5", "gpt-5-mini", "gpt-4.1")  # when the key cannot list its models
MAX_OUTPUT_TOKENS = 32000  # hidden reasoning + the answer
CALL_TIMEOUT_SECONDS = 300.0


def strategy_name(market: str, horizon: str) -> str:
    return f"ai_{market}_{horizon}"


def best_models(account: list[str] | None) -> list[str]:
    """Models to try, best first: the newest flagship GPT on this key (gpt-5.2 > gpt-5.1 > gpt-5), then o3, GPT-4.1."""
    if account is None:
        return list(DEFAULT_MODELS)
    flagships = sorted(
        ((int(m.group(1)), int(m.group(2) or 0), mid) for mid in account if (m := FLAGSHIP.match(mid))), reverse=True
    )
    ordered = [mid for major, _, mid in flagships if major >= 5] + [m for m in FALLBACK_MODELS if m in account]
    return list(dict.fromkeys(ordered)) or list(DEFAULT_MODELS)


def _key(symbol: str, horizon: str) -> str:
    return f"{symbol}:{horizon}"


@dataclass
class _Coin:
    asset: UniverseAsset
    price: float | None = None
    result: dict[str, Any] | None = None


class AIAnalystService:
    def __init__(
        self,
        settings: Settings,
        universe: Any,
        assets: Any,
        chat: ChatService,
        store: SettingsStore,
        *,
        regime: Any = None,
        context: Any = None,
        evidence: Any = None,
        news: Any = None,
        sentiment: Any = None,
        events: Any = None,
        futures: Any = None,
        risk_params: Callable[[], Any],
        equity: Callable[[], float | None] | None = None,
        selection_filter: Callable[[list[UniverseAsset]], list[UniverseAsset]] | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        on_candles: Callable[[str, dict[Any, list[Any]]], Any] | None = None,
        blocked: Callable[[], bool] | None = None,
    ) -> None:
        self._s = settings
        self._universe = universe
        self._assets = assets
        self._chat = chat
        self._store = store
        self._regime = regime
        self._context = context
        self.evidence = evidence
        self._news = news
        self._sentiment = sentiment
        self._events = events
        self._futures = futures
        self._risk = risk_params
        self._equity = equity
        self._filter = selection_filter
        self._sessions = session_factory
        self._on_candles = on_candles
        self._blocked = blocked or (lambda: False)
        self.model = settings.ai_analyst_model or "auto"
        self.effort = settings.ai_analyst_effort if settings.ai_analyst_effort in EFFORTS else "high"
        self.review = settings.ai_analyst_review
        self.min_conviction = settings.ai_analyst_min_conviction
        self.use_quant = True
        self.concurrency = max(1, min(4, settings.ai_analyst_concurrency))
        self.state: dict[str, Any] = {"running": False, "outcome": "never_run"}
        self.results: dict[str, dict[str, Any]] = {}  # "SYMBOL:horizon" -> spot + futures result
        self.latest: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._resolved: tuple[float, list[str]] | None = None

    # ------------------------------------------------------------------ settings

    @property
    def configured(self) -> bool:
        return self._chat.configured

    async def load(self) -> None:
        data = await self._store.get("ai_analyst_settings", None)
        if isinstance(data, dict):
            self._apply(data)

    def _apply(self, data: dict[str, Any]) -> None:
        if isinstance(data.get("model"), str) and data["model"].strip():
            self.model = data["model"].strip()[:64]
            self._resolved = None
        if data.get("effort") in EFFORTS:
            self.effort = data["effort"]
        if data.get("review") is not None:
            self.review = bool(data["review"])
        if data.get("min_conviction") is not None:
            self.min_conviction = max(40, min(90, int(data["min_conviction"])))
        if data.get("use_quant") is not None:
            self.use_quant = bool(data["use_quant"])

    def settings(self) -> dict[str, Any]:
        return {"model": self.model, "effort": self.effort, "efforts": list(EFFORTS), "review": self.review,
                "min_conviction": self.min_conviction, "use_quant": self.use_quant, "configured": self.configured,
                "resolved_models": self._resolved[1] if self._resolved else None, "version": ANALYST_VERSION,
                "prompt_version": aa.PROMPT_VERSION}

    async def update(self, **values: Any) -> dict[str, Any]:
        self._apply({k: v for k, v in values.items() if v is not None})
        await self._store.set("ai_analyst_settings", {"model": self.model, "effort": self.effort, "review": self.review,
                                                      "min_conviction": self.min_conviction, "use_quant": self.use_quant})
        return self.settings()

    async def models(self) -> list[str]:
        """The models a scan tries, best first (the choice in settings, or the best on this key)."""
        if self.model != "auto":
            return list(dict.fromkeys([self.model, *best_models(await self._account_models())]))
        if self._resolved is None or time.monotonic() - self._resolved[0] > 3600:
            self._resolved = (time.monotonic(), best_models(await self._account_models()))
        return self._resolved[1]

    async def _account_models(self) -> list[str] | None:
        try:
            return await self._chat.account_models()
        except Exception:
            return None

    # ------------------------------------------------------------------ control

    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        return _clean(dict(self.state))

    def start_token(self, symbol: str, horizon: str) -> bool:
        """Analyse ONE token (spot and futures together) in the background: one AI call, plus one risk review
        when it proposes a trade. One analysis at a time (they cost OpenAI credits)."""
        if horizon not in aa.HORIZONS:
            raise ValueError("unknown horizon")
        if self.running() or self._blocked() or not self.configured:
            return False
        self._stopping = False
        sym = symbol.upper()
        self.state = {"running": True, "outcome": "running", "symbol": sym, "horizon": horizon, "step": "gathering data",
                      "started_at": utcnow(), "finished_at": None, "error": None}
        self._task = asyncio.create_task(self._token(sym, horizon), name=f"ai-analyst-{sym}-{horizon}")
        return True

    async def wait(self) -> None:
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def stop(self, grace_seconds: float = 5.0) -> None:
        if not self.running():
            return
        self._stopping = True
        assert self._task is not None
        _, pending = await asyncio.wait({self._task}, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        await self.wait()

    def result(self, symbol: str | None = None, horizon: str | None = None) -> dict[str, Any] | None:
        if symbol and horizon:
            return self.results.get(_key(symbol.upper(), horizon))
        return self.results.get(self.latest) if self.latest else None

    def history(self) -> list[dict[str, Any]]:
        """The tokens analysed since the server started, newest first (one line each)."""
        rows = sorted(self.results.values(), key=lambda r: r["generated_at"], reverse=True)
        return [{"symbol": r["symbol"], "horizon": r["horizon"], "generated_at": r["generated_at"],
                 "spot": (r.get("spot") or {}).get("label"), "futures": (r.get("futures") or {}).get("label")} for r in rows[:20]]

    async def _token(self, sym: str, horizon: str) -> None:
        try:
            universe = await self._universe.get()
            asset = universe.find(sym)
            if asset is None or not asset.supported:
                raise ValueError(f"{sym} is not in the analysed universe (add it to the watchlist first)")
            result = await self.analyze(asset, horizon)
            self.results[_key(sym, horizon)] = result
            self.latest = _key(sym, horizon)
            self.state["outcome"] = "stopped" if self._stopping else "completed"
            if result.get("error"):
                self.state["error"] = result["error"]
        except asyncio.CancelledError:
            self.state["outcome"] = "stopped"
            raise
        except Exception as exc:
            log.exception("AI token analysis failed", extra={"symbol": sym})
            self.state["outcome"], self.state["error"] = "failed", (exc.message if isinstance(exc, ChatUnavailable)
                                                                    else f"{type(exc).__name__}: {exc}")[:300]
        finally:
            self.state["running"] = False
            self.state["step"] = None
            self.state["finished_at"] = utcnow()

    def _quant(self, horizon: str) -> dict[str, Any] | None:
        """The futures engine's latest result at this horizon (both sides and its research), when under 3 hours old.
        Never runs a scan: press "Find futures trades" first to give the AI the engine's view."""
        f = self._futures
        if f is None or not getattr(f, "enabled", False):
            return None
        result = f.results.get(horizon)
        if result is None or utcnow() - result["generated_at"] > timedelta(hours=3):
            return None
        return result

    async def _shared(self, universe: Any, horizon: str) -> dict[str, Any]:
        """Context shared by every coin: the market backdrop, market headlines and Bitcoin's own charts."""
        shared: dict[str, Any] = {"market": None, "btc": None, "market_news": [], "regime": None}
        if self._regime is not None:
            with contextlib.suppress(Exception):
                shared["regime"] = await self._regime.current()
        m = shared["regime"]
        ctx: dict[str, Any] = {}
        if m is not None:
            fg = m.fear_greed
            ctx = {
                "regime": getattr(m.regime, "value", m.regime), "btc_trend_1d": getattr(m.btc_trend, "value", m.btc_trend),
                "btc_trend_4h": getattr(m.btc_trend_4h, "value", m.btc_trend_4h), "btc_rsi_4h": aa._num(m.btc_rsi_4h, 3),
                "btc_vs_ema200_pct": aa._num(m.btc_vs_ema200_pct, 3), "btc_roc20_pct": aa._num(m.btc_roc20, 3),
                "breadth_pct_above_ema50": aa._num(m.breadth_pct, 3), "volatility": getattr(m.volatility, "value", m.volatility),
                "fear_greed": ({"value": getattr(fg, "value", None), "label": getattr(fg, "classification", None)} if fg else None),
                "global": (_clean({k: v for k, v in asdict(m.global_metrics).items() if not k.endswith("_at")})
                           if m.global_metrics is not None and hasattr(m.global_metrics, "__dataclass_fields__") else None),
                "flags": list(m.flags)[:4], "notes": list(m.reasons)[:3],
            }
        if self.evidence is not None:
            cached = self.evidence.derivatives.cached_market() if getattr(self.evidence, "derivatives", None) else None
            if cached is not None:
                with contextlib.suppress(Exception):
                    ctx["futures_market"] = _clean(cached.summary(None))
        cached_context = self._context.cached() if self._context is not None else None
        season = getattr(cached_context, "altcoin_season", None)
        if season is not None:
            ctx["altcoin_season_index"] = getattr(season, "value", None)
        shared["market"] = ctx or None
        digest = self._news.cached() if self._news is not None else None
        if digest is not None:
            shared["market_news"] = [self._headline(n) for n in sorted(
                digest.items, key=lambda n: n.published_at or datetime.min.replace(tzinfo=utcnow().tzinfo), reverse=True)[:8]]
            shared["trending"] = [c.symbol for c in digest.trending[:10]]
        btc = next((a for a in universe.assets if a.symbol == "BTC"), None)
        if btc is not None:
            with contextlib.suppress(Exception):
                c = await self._assets.collect("BTC")
                views = [aa.timeframe_view(tf, c.closed.get(tf) or [], 12) for tf in (Timeframe.H4, Timeframe.D1)]
                shared["btc"] = {"price": aa._num(c.ticker.last_price) if c.ticker else None,
                                 "change_24h_pct": aa._num(c.ticker.pct_change_24h, 3) if c.ticker else None,
                                 "timeframes": [v for v in views if v]}
        return shared

    @staticmethod
    def _headline(n: Any) -> dict[str, Any]:
        return {"time": aa._t(n.published_at), "source": n.source, "title": n.title[:200], "tone": n.sentiment}

    def _blank(self, asset: UniverseAsset, horizon: str, reason: str, price: float | None = None) -> dict[str, Any]:
        side = {"signal": SignalLabel.NO_TRADE.value, "label": "NO TRADE", "side": None, "status": "no_trade",
                "conviction": None, "plan": None, "leverage": None, "sizing": None, "notes": [reason]}
        return {"symbol": asset.symbol, "name": asset.name, "horizon": horizon, "price": price, "generated_at": utcnow(),
                "spot": dict(side, market="spot"), "futures": dict(side, market="futures"), "analysis": None,
                "review": None, "probability": None, "model": None, "usage": None, "seconds": None, "error": None,
                "notes": [reason]}

    # ------------------------------------------------------------------ one token

    async def analyze(self, asset: UniverseAsset, horizon: str, *, models: list[str] | None = None) -> dict[str, Any]:
        """One token, spot and futures in a single AI decision (a short applies to futures only)."""
        started = time.monotonic()
        step = self.state.__setitem__
        models = models or await self.models()
        sym = asset.symbol
        if self.evidence is not None:
            with contextlib.suppress(Exception):
                await self.evidence.prepare([sym], ai=True)
        shared = await self._shared(await self._universe.get(), horizon)
        c = await self._assets.collect(sym)
        ticker = c.ticker
        price = ticker.last_price if ticker else None
        if not c.integrity.passed or not price:
            reasons = list(c.integrity.reasons)[:2] or ["no live price"]
            return _clean(self._blank(asset, horizon, "data integrity: " + "; ".join(reasons), price))
        candles = {tf: list(c.closed.get(tf) or []) for tf, _ in aa.VIEWS[horizon]}
        setup_tf = aa.setup_timeframe(horizon)
        if len(candles.get(setup_tf) or []) < 60:
            return _clean(self._blank(asset, horizon, f"not enough {setup_tf.label} history", price))
        regime = shared.get("regime")
        board = snap = None
        setup_snap = compute_snapshot(setup_tf, candles[setup_tf])
        if self.evidence is not None:
            try:
                ev_board = await self.evidence.for_coin(
                    sym, horizon=horizon, side="long", remember=False, price=price, market=regime,
                    h1=c.closed.get(Timeframe.H1, []), setup=candles[setup_tf], d1=c.closed.get(Timeframe.D1, []),
                    volume_24h_quote=ticker.volume_quote_24h, change_24h_pct=ticker.pct_change_24h,
                    rsi=setup_snap.rsi14 if setup_snap else None, atr_pct=setup_snap.atr_pct if setup_snap else None,
                    book_imbalance=c.book.imbalance if c.book is not None and c.book.valid else None,
                )
                board = ev_board.as_dict() if ev_board is not None else None
                if self.evidence.derivatives.enabled:
                    snap = await self.evidence.derivatives.snapshot(sym)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("AI analyst: evidence data failed", extra={"symbol": sym}, exc_info=True)
        no_perp = snap is not None and snap.available and not snap.listed
        costs = {"spot": self._cost("spot", horizon, snap), "futures": self._cost("futures", horizon, snap)}
        rate = c.quote_usd_rate or 1.0
        book = c.book
        dossier = aa.build_dossier(aa.DossierInputs(
            symbol=sym, name=asset.name, market="spot" if no_perp or self._futures is None else "both", horizon=horizon,
            now=utcnow(), price=price, candles=candles, change_24h_pct=ticker.pct_change_24h,
            volume_24h_usd=(ticker.volume_quote_24h or 0) * rate if ticker.volume_quote_24h else None,
            spread_bps=book.spread_bps if book is not None else None,
            depth_usd=({"bids": aa._num((book.bid_depth_quote or 0) * rate, 3), "asks": aa._num((book.ask_depth_quote or 0) * rate, 3)}
                       if book is not None and book.valid else None),
            book_imbalance=book.imbalance if book is not None and book.valid else None,
            data_notes=[r for r in c.integrity.reasons][:3], derivatives=snap,
            mark_price=self._mark(sym), board=board, news=self._coin_news(sym), ai_news=self._ai_news(sym),
            market_news=shared.get("market_news") or [], sentiment=self._coin_sentiment(sym),
            events=self._events.notes_for(sym) if self._events is not None else [],
            market_context=shared.get("market"), btc=shared.get("btc") if sym != "BTC" else None,
            quant=self._quant_for(sym, self._quant(horizon) if self.use_quant else None),
            cost_pct=costs["futures"], spot_cost_pct=costs["spot"], min_reward_risk=self._s.ai_analyst_min_reward_risk,
            risk_per_trade_pct=self._risk().max_risk_per_signal_pct,
            max_leverage=self._futures.max_leverage if self._futures is not None else None,
        ))
        text = aa.dossier_text(dossier)
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "calls": 0}
        step("step", "the AI is analysing")
        answer, model = await self._ask(models, aa.ANALYST_PROMPT, f"DOSSIER:\n{text}", "trade_decision",
                                        aa.ANALYST_SCHEMA, usage)
        decision = aa.parse_decision(answer)
        atr = setup_snap.atr14 if setup_snap else None
        now = utcnow()
        checks = {m: aa.check_plan(decision, market=m, horizon=horizon, price=price, atr=atr, cost_pct=costs[m], now=now,
                                   min_reward_risk=self._s.ai_analyst_min_reward_risk, min_conviction=self.min_conviction)
                  for m in aa.MARKETS}
        if no_perp or self._futures is None:
            checks["futures"] = aa.PlanCheck(SignalLabel.NO_TRADE, None, "no_trade",
                                             ["no USDT perpetual on the checked exchanges" if no_perp else "futures are off"])
        conviction = decision.conviction
        review = None
        lead = next((checks[m] for m in ("futures", "spot") if checks[m].label.rank >= SignalLabel.BUY.rank), None)
        if self.review and lead is not None and not self._stopping:
            step("step", "the risk manager is reviewing")
            plan_text = json.dumps(_jsonable({"decision": decision.as_dict(), "executable_plan": lead.plan}), separators=(",", ":"))
            try:
                answer2, _ = await self._ask([model], aa.REVIEW_PROMPT, f"DOSSIER:\n{text}\n\nPLAN:\n{plan_text}",
                                             "risk_review", aa.REVIEW_SCHEMA, usage)
                review = aa.parse_review(answer2)
            except ChatUnavailable as exc:
                review = {"verdict": "reduce", "adjusted_conviction": min(conviction, self.min_conviction),
                          "summary": f"risk review unavailable ({exc.message[:120]}): treated as unconfirmed",
                          "issues": [], "valid_json": False}
            reviewed = conviction
            for m in aa.MARKETS:
                checks[m], reviewed = aa.apply_review(checks[m], conviction, review, min_conviction=self.min_conviction)
            conviction = reviewed
        result: dict[str, Any] = {
            "symbol": sym, "name": asset.name, "horizon": horizon, "price": price, "generated_at": now,
            "holding": aa.HOLD_TEXT[horizon], "timeframes": [tf.label for tf, _ in aa.VIEWS[horizon]],
            "conviction": conviction, "analyst_conviction": decision.conviction,
            "probability": decision.probability_tp1_before_stop, "analysis": decision.as_dict(), "review": review,
            "model": model, "effort": self.effort, "usage": usage, "seconds": round(time.monotonic() - started, 1),
            "error": None, "board_score": (board or {}).get("score"), "dossier_chars": len(text),
            "engine_research_used": bool(dossier.get("rule_engines")), "notes": [],
        }
        for m in aa.MARKETS:
            check = checks[m]
            out: dict[str, Any] = {"market": m, "signal": check.label.value, "label": aa.label_text(m, check.side, check.label),
                                   "side": check.side, "status": check.status, "conviction": conviction, "plan": check.plan,
                                   "leverage": None, "sizing": None, "notes": check.notes, "cost_pct": costs[m]}
            if check.plan is not None:
                plan = check.plan
                if m == "futures" and self._futures is not None:
                    out["leverage"] = fu.leverage_plan(
                        check.side or "long", plan["entry"], plan["stop"], cost_pct=costs[m],
                        funding_rate_pct_8h=snap.funding_pct if snap is not None else None, hold_hours=plan["hold_hours"],
                        risk_pct_equity=self._risk().max_risk_per_signal_pct, max_leverage=self._futures.max_leverage,
                        mmr_pct=self._futures.mmr_pct, equity=self._equity() if self._equity is not None else None).as_dict()
                elif m == "spot":
                    out["sizing"] = self._spot_size(plan["risk_pct"], costs[m])
            result[m] = out
        if self._on_candles is not None and candles.get(setup_tf):
            with contextlib.suppress(Exception):
                await self._on_candles(sym, {setup_tf: candles[setup_tf]})
        result = _clean(result)
        for m in aa.MARKETS:
            if checks[m].label.rank >= SignalLabel.BUY.rank:
                await self._persist(result, m)
        return result

    async def _ask(self, models: list[str], system: str, user: str, name: str, schema: dict[str, Any],
                   usage: dict[str, int]) -> tuple[str, str]:
        if self._blocked():
            raise ChatUnavailable("emergency stop is engaged", 503)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        errors: list[str] = []
        for model in models:
            try:
                text, used = await self._chat.structured(model, messages, name=name, schema=schema, effort=self.effort,
                                                         max_tokens=MAX_OUTPUT_TOKENS, timeout=CALL_TIMEOUT_SECONDS)
            except ChatUnavailable as exc:
                if exc.status_code in (404, 204) or "model" in exc.message.lower():
                    errors.append(f"{model}: {exc.message}")
                    continue  # the next model
                raise
            usage["calls"] += 1
            usage["prompt_tokens"] += int(used.get("prompt_tokens") or 0)
            usage["completion_tokens"] += int(used.get("completion_tokens") or 0)
            details = used.get("completion_tokens_details") or {}
            usage["reasoning_tokens"] += int(details.get("reasoning_tokens") or 0) if isinstance(details, dict) else 0
            return text, model
        raise ChatUnavailable("; ".join(errors) or "no OpenAI model available", 502)

    def _cost(self, market: str, horizon: str, snap: Any) -> float:
        if market == "futures" and self._futures is not None:
            hold = aa.MAX_HOLD_HOURS[horizon] / 2.0
            funding = abs(snap.funding_pct) if snap is not None and snap.funding_pct is not None else fu.BASE_FUNDING_PCT_8H
            return 2.0 * (self._futures.fee_pct + self._futures.slippage_pct) + funding * hold / 8.0
        risk = self._risk()
        return 2.0 * (risk.fee_pct + self._s.scalp_slippage_pct)

    def _spot_size(self, stop_pct: float, cost: float) -> dict[str, Any]:
        risk = self._risk()
        loss_pct = stop_pct + cost
        allocation = min(risk.max_allocation_pct, risk.max_risk_per_signal_pct / loss_pct * 100.0) if loss_pct > 0 else 0.0
        equity = self._equity() if self._equity is not None else None
        return {"allocation_pct": allocation, "risk_pct": allocation * loss_pct / 100.0,
                "allocation_usd": equity * allocation / 100.0 if equity else None,
                "loss_at_stop_usd": equity * allocation * loss_pct / 10000.0 if equity else None}

    def _mark(self, sym: str) -> float | None:
        cached = self.evidence.derivatives.cached_market() if self.evidence is not None and getattr(self.evidence, "derivatives", None) else None
        quote = cached.best(sym) if cached is not None else None
        return quote.mark if quote is not None else None

    def _coin_news(self, sym: str) -> list[dict[str, Any]]:
        digest = self._news.cached() if self._news is not None else None
        if digest is None:
            return []
        items = [n for n in digest.items if sym in (a.upper() for a in n.assets)]
        items.sort(key=lambda n: n.published_at or datetime.min.replace(tzinfo=utcnow().tzinfo), reverse=True)
        return [self._headline(n) for n in items[:12]]

    def _ai_news(self, sym: str) -> dict[str, Any] | None:
        reader = getattr(self.evidence, "ai_news", None) if self.evidence is not None else None
        if reader is None:
            return None
        with contextlib.suppress(Exception):
            return reader.cached().get(sym)
        return None

    def _coin_sentiment(self, sym: str) -> dict[str, Any] | None:
        s = self._sentiment.for_asset(sym) if self._sentiment is not None else None
        if s is None:
            return None
        return {"state": s.state, "news_score": aa._num(s.news_score, 3), "headlines": s.headlines,
                "positive": s.positive, "negative": s.negative, "notes": list(s.notes)[:3]}

    @staticmethod
    def _quant_for(sym: str, quant: dict[str, Any] | None) -> dict[str, Any] | None:
        if not quant:
            return None
        row = next((s for s in quant.get("signals", []) if s.get("symbol") == sym), None)
        research = [
            {"strategy": r.get("name"), "side": r.get("side"), "validated": r.get("validated"),
             "older_r": aa._num((r.get("train") or {}).get("expectancy_r"), 3), "older_trades": (r.get("train") or {}).get("trades"),
             "newer_r": aa._num((r.get("test") or {}).get("expectancy_r"), 3), "newer_trades": (r.get("test") or {}).get("trades")}
            for r in (quant.get("research") or [])[:6]
        ]
        out: dict[str, Any] = {
            "about": "Signova's rule engine: 8 published strategies for longs and shorts, walk-forward backtested on the "
                     "selected coins with futures costs; R = profit per trade in units of risk",
            "research_best_first": research,
        }
        if row is not None:
            strategy = row.get("strategy") or {}
            out["this_coin"] = {
                "label": row.get("label"), "side": row.get("side"), "strategy": strategy.get("name"),
                "rule": strategy.get("rule"), "status": row.get("status"), "reasons": (row.get("reasons") or [])[:3],
                "record_on_this_coin": {k: aa._num(v, 3) for k, v in (row.get("record") or {}).items()
                                        if k in ("trades", "win_rate", "expectancy_r")} or None,
                "pending": (row.get("pending") or {}).get("text"),
            }
        return out

    # ------------------------------------------------------------------ storage

    async def _persist(self, token: dict[str, Any], market: str) -> None:
        r = {**token[market], "symbol": token["symbol"], "horizon": token["horizon"], "generated_at": token["generated_at"],
             "analysis": token.get("analysis"), "review": token.get("review"), "model": token.get("model"),
             "probability": token.get("probability"), "price": token.get("price"), "board_score": token.get("board_score")}
        if self._sessions is None or r.get("plan") is None:
            return
        plan = r["plan"]
        strategy = strategy_name(market, r["horizon"])
        setup_tf = aa.setup_timeframe(r["horizon"])
        hold_candles = max(1, int(round(plan["hold_hours"] * 60 / setup_tf.minutes)))
        wait_candles = 0
        if plan.get("entry_valid_until") is not None:
            wait_candles = max(1, int(round((plan["entry_valid_until"] - r["generated_at"]).total_seconds() / setup_tf.seconds)))
        try:
            async with self._sessions() as session:
                recent = (await session.execute(
                    select(Signal.id).where(Signal.symbol == r["symbol"], Signal.strategy == strategy, Signal.status == "OPEN",
                                            Signal.created_at >= utcnow() - timedelta(hours=plan["hold_hours"])).limit(1)
                )).scalar_one_or_none()
                if recent is not None:
                    token[market]["notes"] = [*token[market]["notes"], "an earlier AI signal on this coin is still open: not stored twice"]
                    return
                targets = [(plan["tp1"], 50.0 if plan.get("tp2") else 100.0)] + ([(plan["tp2"], 50.0)] if plan.get("tp2") else [])
                analysis = r.get("analysis") or {}
                signal = Signal(
                    symbol=r["symbol"], timeframe=setup_tf.value, strategy=strategy, signal=r["label"][:16],
                    signal_score=int(r.get("conviction") or 0), data_health_score=100, data_state="HEALTHY",
                    entry_low=plan["entry_low"], entry_high=plan["entry"], stop_loss=plan["stop"],
                    risk_reward=round(plan["reward_risk_final"], 4), trend="UP" if r["side"] == "long" else "DOWN",
                    market_regime=None, reasons=[analysis.get("thesis", "")][:1] + list(analysis.get("confluences") or [])[:4],
                    risks=list(analysis.get("risks") or [])[:5], invalidation=analysis.get("invalidation") or
                    f"a close beyond {fmt_price(plan['stop'])}", summary=f"{r['label']} ({r['conviction']}/100): {analysis.get('setup', '')}"[:500],
                    status="OPEN", engine_version=ANALYST_VERSION, model_name=str(r.get("model"))[:64],
                    prompt_version=aa.PROMPT_VERSION,
                    input_features=_jsonable({"horizon": r["horizon"], "market": market, "price": r["price"],
                                              "board_score": r.get("board_score")}),
                    quant_output=_jsonable({"side": r["side"], "max_hold": hold_candles, "entry_type": plan["entry_type"],
                                            "entry_wait": wait_candles, "conviction": r.get("conviction"),
                                            "probability": r.get("probability"), "leverage": r.get("leverage")}),
                    ai_output=_jsonable({"analysis": analysis, "review": r.get("review")}),
                )
                session.add(signal)
                await session.flush()
                session.add_all([SignalTarget(signal_id=signal.id, kind="TP", level_index=k + 1, price=p, allocation_pct=a)
                                 for k, (p, a) in enumerate(targets)])
                session.add(SignalTarget(signal_id=signal.id, kind="SL", level_index=1, price=plan["stop"], allocation_pct=100.0))
                await session.commit()
        except Exception:
            log.exception("AI signal could not be stored", extra={"symbol": r["symbol"]})
