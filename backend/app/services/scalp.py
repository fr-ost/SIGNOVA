"""Scalp signals (15m / 1h / 4h horizons): scan, single-coin analysis, persistence.

For each coin: the integrity-gated collection (the same data the swing engine uses), a
longer history on the setup and trend timeframes for the backtest, the live setup from
app.analysis.scalp, then live checks the history cannot contain (current price versus the
entry, spread, depth, order-book pressure). A scan first backtests every coin, pools the
trades into a larger sample, then judges each coin, so a coin with few trades of its own
can lean on the same rules' record across the scanned coins (capped at BUY).

Scans run only on request, one at a time per horizon, and can be stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis import scalp as sc
from app.config import Settings
from app.core.enums import SignalLabel, Timeframe
from app.core.formatting import fmt_price
from app.core.timeutil import utcnow
from app.data.normalization.schemas import Candle
from app.models import Signal, SignalTarget
from app.services.assets import AssetService
from app.services.spot_router import NoMarketData, SpotMarketRouter
from app.services.universe import UniverseAsset, UniverseService

log = logging.getLogger(__name__)

SCALP_VERSION = "scalp-1.0.0"
TREND_WARMUP = 250


def strategy_name(horizon: str) -> str:
    return f"scalp_{horizon}"


@dataclass
class ScalpPlan:
    entry: float
    entry_low: float
    entry_high: float
    stop: float
    tp1: float
    tp2: float
    risk_pct: float
    cost_pct: float
    reward_risk_tp1: float  # net of costs
    reward_risk_tp2: float
    suggested_allocation_pct: float
    risk_at_allocation_pct: float
    valid_until: datetime
    time_exit: str


@dataclass
class ScalpResult:
    symbol: str
    name: str
    horizon: str
    horizon_label: str
    setup_timeframe: str
    generated_at: datetime
    signal: SignalLabel
    setup: str | None
    setup_time: datetime | None
    price: float | None
    quote_asset: str | None
    plan: ScalpPlan | None
    evidence: str  # coin | pooled | none
    backtest: dict[str, Any] | None
    expected: dict[str, Any] | None  # measured expectancy turned into money at the user's risk settings
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    summary: str = ""
    data_ok: bool = True
    status: str = ""  # short state for the table: setup now, waiting for trigger, trend not up...
    pending: dict[str, Any] | None = None  # conditional levels while waiting for a trigger
    if_triggered: str | None = None  # the label the evidence would allow if the pending setup triggers


@dataclass
class _Work:
    """Per-coin intermediate result between the backtest pass and the judging pass."""

    asset: UniverseAsset
    stats: sc.BacktestStats | None
    candidate: sc.Candidate | None
    why: list[str]
    context_ok: bool
    price: float | None
    quote_asset: str | None
    integrity_ok: bool
    integrity_reasons: list[str]
    book: Any
    volume_24h_quote: float | None
    quote_usd_rate: float | None
    error: str | None = None
    pending: sc.Pending | None = None


@dataclass
class ScanState:
    horizon: str
    running: bool = False
    outcome: str = "never_run"  # never_run | running | completed | stopped | failed
    total: int = 0
    done: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


def _clean(value: Any) -> Any:
    """JSON-safe: datetimes stay (FastAPI encodes them), non-finite floats become None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_clean(v) for v in value]
    return value


def _jsonable(value: Any) -> Any:
    """For JSON columns: datetimes as ISO strings, non-finite floats as None."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return _clean(value)


def stats_out(stats: sc.BacktestStats | None) -> dict[str, Any] | None:
    if stats is None:
        return None
    data = asdict(stats)
    data.pop("records", None)
    data["no_losses"] = stats.profit_factor is not None and math.isinf(stats.profit_factor)
    days = (stats.period_end - stats.period_start).total_seconds() / 86400 if stats.period_start and stats.period_end else 0
    data["days"] = round(days, 1)
    data["trades_per_day"] = round(stats.trades / days, 2) if days > 0 else None
    return _clean(data)


class ScalpService:
    def __init__(
        self,
        settings: Settings,
        universe: UniverseService,
        assets: AssetService,
        router: SpotMarketRouter,
        *,
        risk_params: Callable[[], Any],
        selection_filter: Callable[[list[UniverseAsset]], list[UniverseAsset]] | None = None,
        notes_for: Callable[[str], list[str]] | None = None,
        equity: Callable[[], float | None] | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        on_candles: Callable[[str, dict[Timeframe, list[Candle]]], Any] | None = None,
        blocked: Callable[[], bool] | None = None,
    ) -> None:
        self._blocked = blocked or (lambda: False)
        self._s = settings
        self._universe = universe
        self._assets = assets
        self._router = router
        self._risk = risk_params
        self._filter = selection_filter
        self._notes_for = notes_for
        self._equity = equity
        self._sessions = session_factory
        self._on_candles = on_candles  # the track record evaluates open scalp signals with these
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        self.states: dict[str, ScanState] = {h: ScanState(h) for h in sc.PROFILES}
        self.results: dict[str, dict[str, Any]] = {}
        self._pooled: dict[str, sc.BacktestStats] = {}
        self._stored: dict[tuple[str, str], datetime] = {}

    # ------------------------------------------------------------------ params

    def params(self) -> sc.ScalpParams:
        r = self._risk()
        return sc.ScalpParams(
            fee_pct=r.fee_pct,
            slippage_pct=self._s.scalp_slippage_pct,
            min_risk_cost_multiple=self._s.scalp_min_risk_cost_multiple,
            min_trades=self._s.scalp_min_trades,
        )

    @staticmethod
    def profile(horizon: str) -> sc.ScalpProfile:
        key = (horizon or "").lower().replace("min", "m").replace("hour", "h")
        if key not in sc.PROFILES:
            raise ValueError("horizon must be one of 15m, 1h, 4h")
        return sc.PROFILES[key]

    # ------------------------------------------------------------------ control

    def running(self, horizon: str | None = None) -> bool:
        tasks = [self._tasks.get(horizon)] if horizon else list(self._tasks.values())
        return any(t is not None and not t.done() for t in tasks)

    def status(self) -> dict[str, Any]:
        return {h: asdict(s) for h, s in self.states.items()}

    def start_scan(self, horizon: str) -> bool:
        prof = self.profile(horizon)
        if self.running(prof.key) or self._blocked():
            return False
        self._stopping = False
        self.states[prof.key] = ScanState(prof.key, running=True, outcome="running", started_at=utcnow())
        self._tasks[prof.key] = asyncio.create_task(self._scan(prof), name=f"scalp-scan-{prof.key}")
        return True

    async def wait(self, horizon: str | None = None) -> None:
        for key, task in list(self._tasks.items()):
            if horizon is None or key == horizon:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def stop(self, grace_seconds: float = 20.0) -> None:
        """Graceful: no new coins start, the ones in progress finish; cancel only after the grace time."""
        running = [t for t in self._tasks.values() if not t.done()]
        if not running:
            return
        self._stopping = True
        _, pending = await asyncio.wait(running, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        await self.wait()

    # ------------------------------------------------------------------ scanning

    async def _btc_trend(self, prof: sc.ScalpProfile, assets: list[UniverseAsset]) -> list[Candle] | None:
        btc = next((a for a in assets if a.symbol == "BTC" and a.supported), None)
        if btc is None:
            btc = (await self._universe.get()).find("BTC")
        if btc is None or not btc.supported:
            return None
        count = math.ceil(prof.history * prof.setup.seconds / prof.trend.seconds) + TREND_WARMUP
        try:
            raw, _ = await self._router.history(btc.markets, prof.trend, count)
        except NoMarketData:
            return None
        closed, _ = self._assets.validate(prof.trend, raw)
        return closed

    async def _scan(self, prof: sc.ScalpProfile) -> None:
        state = self.states[prof.key]
        try:
            universe = await self._universe.get()
            assets = [a for a in universe.assets if a.supported]
            if self._filter is not None:
                assets = self._filter(assets)
            state.total = len(assets)
            p = self.params()
            btc = await self._btc_trend(prof, universe.assets)
            semaphore = asyncio.Semaphore(max(1, self._s.signal_scan_concurrency))

            async def one(asset: UniverseAsset) -> _Work:
                async with semaphore:
                    if self._stopping:
                        return _Work(asset, None, None, [], False, None, None, False, [], None, None, None,
                                     error="scan stopped")
                    try:
                        return await self._work(asset, prof, p, btc)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # one coin never takes the scan down
                        log.exception("scalp analysis failed", extra={"symbol": asset.symbol})
                        return _Work(asset, None, None, [], False, None, None, False, [], None, None, None,
                                     error=f"{type(exc).__name__}: {exc}"[:200])
                    finally:
                        state.done += 1

            works = await asyncio.gather(*(one(a) for a in assets))
            if self._stopping:
                state.outcome = "stopped"
                return
            pooled = sc.pool([w.stats for w in works if w.stats is not None])
            if pooled is not None:
                self._pooled[prof.key] = pooled
            results = [await self._judge(w, prof, p, pooled) for w in works]
            rank = {SignalLabel.STRONG_BUY: 0, SignalLabel.BUY: 1, SignalLabel.WATCH: 2, SignalLabel.NO_TRADE: 3}
            results.sort(key=lambda r: (rank[r.signal], -((r.backtest or {}).get("expectancy_r") or -9)))
            self.results[prof.key] = {
                "horizon": prof.key,
                "label": prof.label,
                "setup_timeframe": prof.setup.label,
                "trend_timeframe": prof.trend.label,
                "filter_timeframe": prof.filter.label,
                "max_hold_minutes": prof.hold_minutes,
                "generated_at": utcnow(),
                "engine_version": SCALP_VERSION,
                "cost_pct": p.cost_pct,
                "pooled": stats_out(pooled),
                "counts": {label.value: sum(1 for r in results if r.signal == label) for label in SignalLabel},
                "signals": [self.result_out(r) for r in results],
                "errors": [f"{w.asset.symbol}: {w.error}" for w in works if w.error],
            }
            state.outcome = "completed"
        except asyncio.CancelledError:
            state.outcome = "stopped"
            raise
        except Exception as exc:
            log.exception("scalp scan failed")
            state.outcome = "failed"
            state.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            state.running = False
            state.finished_at = utcnow()

    async def analyze(self, symbol: str, horizon: str) -> dict[str, Any]:
        """One coin now (uses the pooled record of the last scan at this horizon, if any)."""
        prof = self.profile(horizon)
        universe = await self._universe.get()
        asset = universe.find(symbol)
        if asset is None:
            raise LookupError(f"{symbol.upper()} is not in the current universe")
        p = self.params()
        if not asset.supported:
            reason = asset.unsupported_reason or "no supported spot market"
            return self.result_out(ScalpResult(
                symbol=asset.symbol, name=asset.name, horizon=prof.key, horizon_label=prof.label,
                setup_timeframe=prof.setup.label, generated_at=utcnow(), signal=SignalLabel.NO_TRADE, setup=None,
                setup_time=None, price=None, quote_asset=None, plan=None, evidence="none", backtest=None, expected=None,
                reasons=[reason], summary=f"NO TRADE: {reason}", data_ok=False,
            ))
        btc = None if asset.symbol == "BTC" else await self._btc_trend(prof, universe.assets)
        work = await self._work(asset, prof, p, btc)
        result = await self._judge(work, prof, p, self._pooled.get(prof.key))
        return self.result_out(result)

    async def _work(self, asset: UniverseAsset, prof: sc.ScalpProfile, p: sc.ScalpParams, btc: list[Candle] | None) -> _Work:
        collection = await self._assets.collect(asset.symbol)
        integrity = collection.integrity
        ticker = collection.ticker
        base = _Work(
            asset=asset, stats=None, candidate=None, why=[], context_ok=False,
            price=ticker.last_price if ticker else None,
            quote_asset=collection.ticker_market.quote_asset if collection.ticker_market else None,
            integrity_ok=integrity.passed, integrity_reasons=list(integrity.reasons),
            book=collection.book, volume_24h_quote=ticker.volume_quote_24h if ticker else None,
            quote_usd_rate=collection.quote_usd_rate,
        )
        filter_candles = collection.closed.get(prof.filter, [])
        raw_setup, _ = await self._router.history(asset.markets, prof.setup, prof.history)
        setup, setup_report = self._assets.validate(prof.setup, raw_setup)
        count = math.ceil(len(setup) * prof.setup.seconds / prof.trend.seconds) + TREND_WARMUP
        if prof.trend in collection.closed and len(collection.closed[prof.trend]) >= count:
            trend = collection.closed[prof.trend]
        else:
            raw_trend, _ = await self._router.history(asset.markets, prof.trend, count)
            trend, _ = self._assets.validate(prof.trend, raw_trend)
        if not setup_report.ok:
            base.error = None
            base.why = [f"{prof.setup.label} history: {setup_report.critical_issues[0]}"]
            base.integrity_ok = False
            base.integrity_reasons = base.why + base.integrity_reasons
        if len(setup) <= sc.WARMUP + 1 or not trend or not filter_candles:
            base.why = base.why or [f"not enough {prof.setup.label}/{prof.trend.label}/{prof.filter.label} history"]
            return base
        is_btc = asset.symbol == "BTC"
        series = sc.build_series(prof, setup, trend, filter_candles, None if is_btc else btc)
        base.stats = sc.backtest(series, p, is_btc=is_btc)
        last = len(series) - 1
        for k in range(last, max(sc.WARMUP - 1, last - p.fresh_candles), -1):
            cand, _ = sc.evaluate_at(series, k, p, is_btc=is_btc)
            if cand is not None:
                base.candidate = cand
                break
        _, why = sc.evaluate_at(series, last, p, explain=True, is_btc=is_btc)
        base.why = base.why or why
        if base.candidate is None:
            base.pending = sc.pending_plan(series, last, p, is_btc=is_btc)
        base.context_ok = series.trend_up[last] and not series.filter_down[last] and (is_btc or not series.btc_down[last])
        if self._on_candles is not None:
            with contextlib.suppress(Exception):
                await self._on_candles(asset.symbol, {prof.setup: setup})
        return base

    # ------------------------------------------------------------------ judging

    async def _judge(self, w: _Work, prof: sc.ScalpProfile, p: sc.ScalpParams, pooled: sc.BacktestStats | None) -> ScalpResult:
        risk = self._risk()
        now = utcnow()
        result = ScalpResult(
            symbol=w.asset.symbol, name=w.asset.name, horizon=prof.key, horizon_label=prof.label,
            setup_timeframe=prof.setup.label, generated_at=now, signal=SignalLabel.NO_TRADE, setup=None, setup_time=None,
            price=w.price, quote_asset=w.quote_asset, plan=None, evidence="none", backtest=stats_out(w.stats),
            expected=None, data_ok=w.integrity_ok,
        )
        if w.error:
            result.reasons = [f"analysis failed: {w.error}"]
            result.summary = "NO TRADE: analysis failed"
            result.status = "analysis failed"
            return result
        if not w.integrity_ok:
            result.reasons = [f"data integrity: {r}" for r in (w.integrity_reasons or ["integrity gate failed"])[:3]]
            result.summary = f"NO TRADE: {result.reasons[0]}"
            result.status = "data check failed"
            return result
        cand = w.candidate
        if cand is None or w.stats is None:
            result.signal = SignalLabel.WATCH if w.context_ok else SignalLabel.NO_TRADE
            pend = w.pending
            if pend is not None and w.stats is not None:
                kind = sc.PULLBACK if pend.kind == "dip" else pend.kind
                verdict, evidence_reasons, source = sc.evidence(w.stats, kind, p, pooled)
                if not pend.fees_ok:
                    verdict = SignalLabel.NO_TRADE
                result.pending = _clean(asdict(pend))
                result.if_triggered = verdict.value
                result.setup = f"{pend.kind} (waiting)"
                result.evidence = source
                result.status = {"pullback": "waiting for trigger", "dip": "waiting for a dip",
                                 "breakout": "waiting for breakout"}[pend.kind]
                result.reasons = [f"no setup yet: {pend.text}",
                                  f"if it triggers: {verdict.value} ({evidence_reasons[0]})"] + w.why[:1]
            else:
                result.status = "trend not right" if not w.context_ok else "waiting"
                result.reasons = (["trend context is right; waiting for an entry trigger"] if w.context_ok else []) + w.why[:3]
            result.summary = f"{result.signal.value}: {result.reasons[0] if result.reasons else 'no setup'}"
            return result

        label, evidence_reasons, source = sc.evidence(w.stats, cand.kind, p, pooled)
        result.evidence = source
        result.setup, result.setup_time = cand.kind, cand.time
        reasons = list(cand.reasons) + evidence_reasons
        risks: list[str] = []
        price = w.price
        r_unit = cand.entry - cand.stop
        if price is None:
            label = SignalLabel.NO_TRADE
            reasons.insert(0, "live price unavailable")
        elif price <= cand.stop:
            label = SignalLabel.NO_TRADE
            reasons.insert(0, f"invalidated: price {fmt_price(price)} is at or below the stop {fmt_price(cand.stop)}")
        elif price >= cand.tp1:
            label = label.cap(SignalLabel.WATCH)
            reasons.insert(0, f"missed: price {fmt_price(price)} already reached TP1 {fmt_price(cand.tp1)}")
        elif price > cand.entry + p.max_chase_r * r_unit:
            label = label.cap(SignalLabel.WATCH)
            reasons.insert(0, f"price ran {(price - cand.entry) / r_unit:.2f}R above the signal entry: "
                              f"wait for a retest of {fmt_price(cand.entry)} or skip")
        book = w.book
        if book is None or not book.valid:
            label = SignalLabel.NO_TRADE
            reasons.insert(0, "order book unavailable")
        else:
            if book.spread_bps is not None and book.spread_bps > risk.max_spread_bps:
                label = SignalLabel.NO_TRADE
                reasons.insert(0, f"spread {book.spread_bps:.1f} bps above the {risk.max_spread_bps:g} bps limit")
            depth = min(book.bid_depth_quote or 0.0, book.ask_depth_quote or 0.0) * (w.quote_usd_rate or 0.0)
            if depth < risk.min_depth_usd:
                label = SignalLabel.NO_TRADE
                reasons.insert(0, f"order book too thin (${depth:,.0f} on the thinner side)")
            if book.imbalance is not None and book.imbalance <= risk.min_book_imbalance_strong:
                if label == SignalLabel.STRONG_BUY:
                    label = SignalLabel.BUY
                risks.append(f"sellers dominate the order book (imbalance {book.imbalance:+.2f})")
        volume_usd = (w.volume_24h_quote or 0.0) * (w.quote_usd_rate or 0.0)
        if volume_usd < risk.min_volume_24h_usd:
            label = SignalLabel.NO_TRADE
            reasons.insert(0, f"24h volume ${volume_usd:,.0f} below the ${risk.min_volume_24h_usd:,.0f} minimum")
        if self._notes_for is not None:
            risks.extend(self._notes_for(w.asset.symbol))

        loss_pct = cand.risk_pct + p.cost_pct
        allocation = min(risk.max_risk_per_signal_pct / loss_pct * 100.0, risk.max_allocation_pct)
        valid_until = cand.time + timedelta(seconds=prof.setup.seconds * p.fresh_candles)
        result.plan = ScalpPlan(
            entry=cand.entry,
            entry_low=cand.entry - 0.3 * r_unit,
            entry_high=cand.entry + p.max_chase_r * r_unit,
            stop=cand.stop,
            tp1=cand.tp1,
            tp2=cand.tp2,
            risk_pct=cand.risk_pct,
            cost_pct=p.cost_pct,
            reward_risk_tp1=sc.net_reward_risk(cand.entry, cand.stop, cand.tp1, p.cost_pct),
            reward_risk_tp2=sc.net_reward_risk(cand.entry, cand.stop, cand.tp2, p.cost_pct),
            suggested_allocation_pct=allocation,
            risk_at_allocation_pct=allocation * loss_pct / 100.0,
            valid_until=valid_until,
            time_exit=f"close the rest after {prof.hold_minutes} minutes ({prof.max_hold} x {prof.setup.label} candles)",
        )
        if now > valid_until + timedelta(seconds=prof.setup.seconds) and label.rank > SignalLabel.WATCH.rank:
            label = SignalLabel.WATCH
            reasons.insert(0, "signal is older than its entry window")
        stats = pooled if source == "pooled" else w.stats
        result.expected = self._expected(stats, risk, p)
        result.status = {SignalLabel.STRONG_BUY: "setup now", SignalLabel.BUY: "setup now"}.get(label, "setup, not taken")
        result.signal = label
        result.reasons = reasons
        result.risks = risks
        result.summary = self._summary(result)
        if label in (SignalLabel.BUY, SignalLabel.STRONG_BUY):
            await self._persist(result, prof)
        return result

    def _expected(self, stats: sc.BacktestStats | None, risk: Any, p: sc.ScalpParams) -> dict[str, Any] | None:
        if stats is None or stats.expectancy_r is None:
            return None
        out = stats_out(stats) or {}
        equity = self._equity() if self._equity is not None else None
        risk_amount = equity * risk.max_risk_per_signal_pct / 100.0 if equity else None
        per_trade = stats.expectancy_r * risk_amount if risk_amount else None
        per_day = per_trade * out["trades_per_day"] if per_trade is not None and out.get("trades_per_day") else None
        return _clean({
            "expectancy_r": stats.expectancy_r,
            "equity": equity,
            "risk_per_trade": risk_amount,
            "expected_per_trade": per_trade,
            "trades_per_day": out.get("trades_per_day"),
            "expected_per_day": per_day,
            "note": "past average, not a promise: real results vary widely from trade to trade",
        })

    @staticmethod
    def _summary(r: ScalpResult) -> str:
        if r.plan is not None and r.signal in (SignalLabel.BUY, SignalLabel.STRONG_BUY):
            pl = r.plan
            return (f"{r.signal.value} ({r.setup}, {r.horizon_label}): entry {fmt_price(pl.entry)}, stop {fmt_price(pl.stop)} "
                    f"(-{pl.risk_pct:.2f}%), TP1 {fmt_price(pl.tp1)}, TP2 {fmt_price(pl.tp2)}")
        return f"{r.signal.value}: {r.reasons[0]}" if r.reasons else r.signal.value

    def result_out(self, r: ScalpResult) -> dict[str, Any]:
        return _clean(asdict(r))

    # ------------------------------------------------------------------ persistence

    async def _persist(self, r: ScalpResult, prof: sc.ScalpProfile) -> None:
        if self._sessions is None or r.plan is None or r.setup_time is None:
            return
        key = (r.symbol, prof.key)
        if self._stored.get(key) == r.setup_time:
            return
        strategy = strategy_name(prof.key)
        try:
            async with self._sessions() as session:
                exists = (
                    await session.execute(
                        select(Signal.id).where(
                            Signal.symbol == r.symbol, Signal.strategy == strategy, Signal.created_at >= r.setup_time
                        ).limit(1)
                    )
                ).scalar_one_or_none()
                if exists is None:
                    pl = r.plan
                    signal = Signal(
                        symbol=r.symbol, timeframe=prof.setup.value, strategy=strategy, signal=r.signal.value,
                        signal_score=int(round((r.backtest or {}).get("win_rate") or 0)),
                        data_health_score=100 if r.data_ok else 0, data_state="HEALTHY" if r.data_ok else "DEGRADED",
                        entry_low=pl.entry_low, entry_high=pl.entry, stop_loss=pl.stop,
                        risk_reward=round(pl.reward_risk_tp2, 4), trend="UP", market_regime=None,
                        reasons=list(r.reasons), risks=list(r.risks),
                        invalidation=f"a {prof.setup.label} close below {fmt_price(pl.stop)}; {pl.time_exit}",
                        summary=r.summary, status="OPEN", engine_version=SCALP_VERSION,
                        input_features=_jsonable({"setup": r.setup, "setup_time": r.setup_time,
                                                  "evidence": r.evidence, "horizon": prof.key}),
                        quant_output=_jsonable({"backtest": {k: v for k, v in (r.backtest or {}).items() if k != "recent"},
                                                "max_hold": prof.max_hold}),
                    )
                    session.add(signal)
                    await session.flush()
                    session.add_all([
                        SignalTarget(signal_id=signal.id, kind="TP", level_index=1, price=pl.tp1, allocation_pct=50.0),
                        SignalTarget(signal_id=signal.id, kind="TP", level_index=2, price=pl.tp2, allocation_pct=50.0),
                        SignalTarget(signal_id=signal.id, kind="SL", level_index=1, price=pl.stop, allocation_pct=100.0),
                    ])
                    await session.commit()
        except Exception:
            log.exception("scalp signal persistence failed", extra={"symbol": r.symbol})
            return
        self._stored[key] = r.setup_time

