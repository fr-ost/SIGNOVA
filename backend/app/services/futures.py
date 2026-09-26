"""Futures signals (Phase 12): long and short trades on USDT perpetuals, researched like the spot scalps.

A scan (on request only) does, for every selected coin and the chosen horizon:
1. the validated candles the spot engine uses (the spot price is the index the perpetual's mark
   price follows), and the same candles mirrored for shorts (app.analysis.futures);
2. every library strategy on both sides, backtested with futures costs (taker fees, slippage and a
   funding allowance) and a fresh signal on the last candles if there is one;
3. the walk-forward research pooled over the coins, for each of the 16 strategy-and-side pairs:
   only a pair that made money on the older 70% AND the newer 30% of the histories may signal;
4. per coin: the best validated setup (long or short), the side-aware evidence board (futures
   positioning, liquidations, flow, news, market) which can only hold a trade back, the live
   checks (price ran, stop hit, spread, depth, volume), a perpetual listing check, and a leverage
   plan sized so the stop costs your risk per trade with the liquidation price far beyond it.

Signals are stored and followed by the track record (shorts included) exactly like spot signals.
Nothing is traded: you place every order yourself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis import evidence as ev
from app.analysis import futures as fu
from app.analysis import scalp as sc
from app.analysis import strategies as st
from app.config import Settings
from app.core.enums import SignalLabel
from app.core.formatting import fmt_duration, fmt_price
from app.core.timeutil import utcnow
from app.data.normalization.schemas import Candle
from app.data.validation.prices import USD_STABLE_QUOTES
from app.models import Signal, SignalTarget
from app.services.analysis import evidence_record
from app.services.scalp import ScanState, _clean, _jsonable
from app.services.settings_store import SettingsStore
from app.services.universe import UniverseAsset

log = logging.getLogger(__name__)

FUTURES_VERSION = "futures-1.0.0"
MAX_LEVERAGE_CAP = 20


def strategy_name(horizon: str) -> str:
    return f"fut_{horizon}"


@dataclass
class _FWork:
    asset: UniverseAsset
    price: float | None = None
    quote_asset: str | None = None
    integrity_ok: bool = True
    integrity_reasons: list[str] = field(default_factory=list)
    book: Any = None
    volume_24h_quote: float | None = None
    quote_usd_rate: float | None = None
    long: fu.SideAnalysis | None = None
    short: fu.SideAnalysis | None = None
    split: datetime | None = None
    listed: bool | None = None  # a USDT perpetual exists (None: could not check)
    mark: float | None = None
    funding_pct: float | None = None
    boards: dict[str, Any] = field(default_factory=dict)  # side -> Evidence
    why: list[str] = field(default_factory=list)
    error: str | None = None


class FuturesService:
    def __init__(
        self,
        settings: Settings,
        universe: Any,
        assets: Any,
        scalp: Any,
        store: SettingsStore,
        *,
        risk_params: Callable[[], Any],
        equity: Callable[[], float | None] | None = None,
        derivatives: Any = None,
        evidence: Any = None,
        selection_filter: Callable[[list[UniverseAsset]], list[UniverseAsset]] | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        on_candles: Callable[[str, dict[Any, list[Candle]]], Any] | None = None,
        blocked: Callable[[], bool] | None = None,
    ) -> None:
        self._s = settings
        self._universe = universe
        self._assets = assets
        self._scalp = scalp
        self._store = store
        self._risk = risk_params
        self._equity = equity
        self.derivatives = derivatives
        self.evidence = evidence
        self._filter = selection_filter
        self._sessions = session_factory
        self._on_candles = on_candles
        self._blocked = blocked or (lambda: False)
        self.enabled = settings.futures_enabled
        self.fee_pct = settings.futures_fee_pct
        self.slippage_pct = settings.futures_slippage_pct
        self.max_leverage = max(1, min(MAX_LEVERAGE_CAP, settings.futures_max_leverage))
        self.mmr_pct = settings.futures_maintenance_margin_pct
        self.states: dict[str, ScanState] = {h: ScanState(h) for h in sc.PROFILES}
        self.results: dict[str, dict[str, Any]] = {}
        self._research: dict[str, dict[tuple[str, str], st.Research]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False
        self._stored: dict[tuple[str, str, str], datetime] = {}

    # ------------------------------------------------------------------ settings

    async def load(self) -> None:
        data = await self._store.get("futures_settings", None)
        if isinstance(data, dict):
            self._apply(data)

    def _apply(self, data: dict[str, Any]) -> None:
        if data.get("max_leverage") is not None:
            self.max_leverage = max(1, min(MAX_LEVERAGE_CAP, int(data["max_leverage"])))
        if data.get("fee_pct") is not None:
            self.fee_pct = max(0.0, min(0.2, float(data["fee_pct"])))
        if data.get("slippage_pct") is not None:
            self.slippage_pct = max(0.0, min(0.5, float(data["slippage_pct"])))
        if data.get("mmr_pct") is not None:
            self.mmr_pct = max(0.1, min(5.0, float(data["mmr_pct"])))

    def settings(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "max_leverage": self.max_leverage, "max_leverage_cap": MAX_LEVERAGE_CAP,
                "fee_pct": self.fee_pct, "slippage_pct": self.slippage_pct, "mmr_pct": self.mmr_pct,
                "cost_pct": 2.0 * (self.fee_pct + self.slippage_pct), "risk_pct": self._risk().max_risk_per_signal_pct}

    async def update(self, **values: Any) -> dict[str, Any]:
        self._apply({k: v for k, v in values.items() if v is not None})
        await self._store.set("futures_settings", {"max_leverage": self.max_leverage, "fee_pct": self.fee_pct,
                                                   "slippage_pct": self.slippage_pct, "mmr_pct": self.mmr_pct})
        return self.settings()

    def params(self, horizon: str) -> sc.ScalpParams:
        """The published rules with futures costs; funding for the typical holding time is added to the costs."""
        prof = self._scalp.profile(horizon)
        hold_hours = prof.hold_minutes / 60.0 * 2.0  # trend exits hold longer than the base limit
        allowance = fu.BASE_FUNDING_PCT_8H * hold_hours / 8.0
        base = self._scalp.base_params()
        return replace(base, fee_pct=self.fee_pct, slippage_pct=self.slippage_pct + allowance / 2.0, variant="futures")

    # ------------------------------------------------------------------ control

    def running(self, horizon: str | None = None) -> bool:
        tasks = [self._tasks.get(horizon)] if horizon else list(self._tasks.values())
        return any(t is not None and not t.done() for t in tasks)

    def status(self) -> dict[str, Any]:
        return {h: asdict(s) for h, s in self.states.items()}

    def start_scan(self, horizon: str) -> bool:
        prof = self._scalp.profile(horizon)
        if self.running(prof.key) or self._blocked() or not self.enabled:
            return False
        self._stopping = False
        self.states[prof.key] = ScanState(prof.key, running=True, outcome="running", started_at=utcnow())
        self._tasks[prof.key] = asyncio.create_task(self._scan(prof), name=f"futures-scan-{prof.key}")
        return True

    async def wait(self, horizon: str | None = None) -> None:
        for key, task in list(self._tasks.items()):
            if horizon is None or key == horizon:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def stop(self, grace_seconds: float = 20.0) -> None:
        running = [t for t in self._tasks.values() if not t.done()]
        if not running:
            return
        self._stopping = True
        _, pending = await asyncio.wait(running, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        await self.wait()

    # ------------------------------------------------------------------ scan

    async def _scan(self, prof: sc.ScalpProfile) -> None:
        state = self.states[prof.key]
        try:
            universe = await self._universe.get()
            assets = [a for a in universe.assets if a.supported]
            if self._filter is not None:
                assets = self._filter(assets)
            state.total = len(assets)
            p = self.params(prof.key)
            btc = await self._scalp.btc_trend(prof, universe.assets)
            if self.evidence is not None:
                with contextlib.suppress(Exception):
                    await self.evidence.prepare([a.symbol for a in assets])
            market = None
            if self.derivatives is not None and self.derivatives.enabled:
                try:
                    market = await self.derivatives.market()
                except Exception:
                    log.warning("futures listings unavailable", exc_info=True)
            semaphore = asyncio.Semaphore(max(1, self._s.signal_scan_concurrency))

            async def one(asset: UniverseAsset) -> _FWork:
                async with semaphore:
                    if self._stopping:
                        return _FWork(asset, error="scan stopped")
                    try:
                        return await self._work(asset, prof, p, btc, market)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        log.exception("futures analysis failed", extra={"symbol": asset.symbol})
                        return _FWork(asset, error=f"{type(exc).__name__}: {exc}"[:200])
                    finally:
                        state.done += 1

            works = await asyncio.gather(*(one(a) for a in assets))
            if self._stopping:
                state.outcome = "stopped"
                return
            self._research[prof.key] = self.research(works)
            results = [await self._judge(w, prof, p) for w in works]
            order = {SignalLabel.STRONG_BUY: 0, SignalLabel.BUY: 1, SignalLabel.WATCH: 2, SignalLabel.NO_TRADE: 3}
            results.sort(key=lambda r: (order[SignalLabel(r["signal"])], -(r.get("expectancy_r") or -9)))
            research = self._research[prof.key]
            self.results[prof.key] = {
                "horizon": prof.key, "label": prof.label.replace("trade", "futures trade").replace("scalp", "futures scalp"),
                "setup_timeframe": prof.setup.label, "trend_timeframe": prof.trend.label,
                "filter_timeframe": prof.filter.label, "generated_at": utcnow(), "engine_version": FUTURES_VERSION,
                "cost_pct": p.cost_pct, "settings": self.settings(),
                "research": [_clean(r.as_dict() | {"side": side}) for (side, _), r in sorted(
                    research.items(), key=lambda kv: (kv[0][0], list(st.STRATEGIES).index(kv[0][1])))],
                "counts": {
                    "LONG": sum(1 for r in results if r["side"] == "long" and r["signal"] in ("BUY", "STRONG BUY")),
                    "SHORT": sum(1 for r in results if r["side"] == "short" and r["signal"] in ("BUY", "STRONG BUY")),
                    "WATCH": sum(1 for r in results if r["signal"] == "WATCH"),
                    "NO TRADE": sum(1 for r in results if r["signal"] == "NO TRADE"),
                },
                "no_trade_reason": self._no_trade_reason(prof, results),
                "signals": results,
                "errors": [f"{w.asset.symbol}: {w.error}" for w in works if w.error],
            }
            state.outcome = "completed"
        except asyncio.CancelledError:
            state.outcome = "stopped"
            raise
        except Exception as exc:
            log.exception("futures scan failed")
            state.outcome = "failed"
            state.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            state.running = False
            state.finished_at = utcnow()

    async def _work(self, asset: UniverseAsset, prof: sc.ScalpProfile, p: sc.ScalpParams, btc: list[Candle] | None,
                    market: Any) -> _FWork:
        collection = await self._assets.collect(asset.symbol)
        ticker = collection.ticker
        w = _FWork(
            asset=asset, price=ticker.last_price if ticker else None,
            quote_asset=collection.ticker_market.quote_asset if collection.ticker_market else None,
            integrity_ok=collection.integrity.passed, integrity_reasons=list(collection.integrity.reasons),
            book=collection.book, volume_24h_quote=ticker.volume_quote_24h if ticker else None,
            quote_usd_rate=collection.quote_usd_rate,
        )
        base = asset.symbol.upper()
        if market is not None:
            quote = market.best(base)
            w.listed = bool(quote is not None or any(base in q for q in market.quotes.values())
                            or f"{base}-USDT-SWAP" in market.okx_contracts)
            if not market.quotes and not market.okx_contracts:
                w.listed = None  # no exchange answered: cannot tell
            if quote is not None:
                w.mark, w.funding_pct = quote.mark, quote.funding_pct_8h
        setup, trend, filt, problem = await self._scalp.load_candles(asset, prof, collection)
        if problem is not None:
            w.why = [problem]
            if problem.startswith(f"{prof.setup.label} history:"):
                w.integrity_ok = False
                w.integrity_reasons = [problem, *w.integrity_reasons]
        if len(setup) <= sc.WARMUP + 1 or not trend or not filt:
            return w
        is_btc = base == "BTC"
        k = fu.mirror_k(setup)

        def compute() -> tuple[fu.SideAnalysis, fu.SideAnalysis]:
            long_series = sc.build_series(prof, setup, trend, filt, None if is_btc else btc)
            inv = [fu.invert_candles(c, k) for c in (setup, trend, filt)]
            btc_inv = fu.invert_candles(btc, fu.mirror_k(btc)) if btc and not is_btc else None
            short_series = sc.build_series(prof, inv[0], inv[1], inv[2], btc_inv)
            return (fu.analyze_side("long", long_series, 1.0, p, is_btc=is_btc, symbol=base),
                    fu.analyze_side("short", short_series, k, p, is_btc=is_btc, symbol=base))

        w.long, w.short = await asyncio.to_thread(compute)
        w.split = setup[int(len(setup) * st.TRAIN_FRACTION)].open_time
        if self.evidence is not None:
            for a in (w.long, w.short):
                levels = self._levels(a)
                if levels is None and not a.context_ok:
                    continue
                try:
                    w.boards[a.side] = await self._board(asset, prof, a, levels, collection, w)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("futures evidence board failed", extra={"symbol": base})
        if self._on_candles is not None:
            with contextlib.suppress(Exception):
                await self._on_candles(base, {prof.setup: setup})
        return w

    @staticmethod
    def _levels(a: fu.SideAnalysis) -> tuple[float, float, float, float] | None:
        if a.now:
            c = next(iter(a.now.values()))
            return c.entry, c.stop, c.tp1, c.tp2
        if a.pending:
            q = a.pending
            return q["trigger"], q["stop"], q["tp1"], q["tp2"]
        return None

    async def _board(self, asset: UniverseAsset, prof: sc.ScalpProfile, a: fu.SideAnalysis,
                     levels: tuple[float, float, float, float] | None, collection: Any, w: _FWork) -> Any:
        from app.core.enums import Timeframe

        ticker = collection.ticker
        last = len(a.series) - 1
        atr = a.series.atr[last]
        book = collection.book
        entry, stop, tp1, tp2 = levels if levels else (None, None, None, None)
        return await self.evidence.for_coin(
            asset.symbol, horizon=prof.key, side=a.side, remember=False, price=w.price,
            market=await self._scalp._market(),  # noqa: SLF001
            h1=collection.closed.get(Timeframe.H1, []), setup=a.series.candles if a.side == "long" else [],
            d1=collection.closed.get(Timeframe.D1, []), entry=entry, stop=stop, tp1=tp1, tp2=tp2,
            volume_24h_quote=ticker.volume_quote_24h if ticker else None,
            change_24h_pct=ticker.pct_change_24h if ticker else None,
            rsi=a.series.rsi[last] if a.side == "long" else (100.0 - a.series.rsi[last]) if a.series.rsi[last] is not None else None,
            atr_pct=(atr / a.series.close[last] * 100.0) if atr and a.series.close[last] else None,
            book_imbalance=book.imbalance if book is not None and book.valid else None,
        )

    # ------------------------------------------------------------------ research

    @staticmethod
    def research(works: list[_FWork]) -> dict[tuple[str, str], st.Research]:
        out: dict[tuple[str, str], st.Research] = {}
        for side in fu.SIDES:
            for key in st.STRATEGIES:
                per_coin: dict[str, tuple[list[sc.TradeRecord], datetime | None]] = {}
                for w in works:
                    a = w.long if side == "long" else w.short
                    if a is None or w.split is None:
                        continue
                    per_coin[w.asset.symbol] = (a.records.get(key, []), w.split)
                res = st.research(key, per_coin)
                if side == "short":
                    res.rule = fu.SHORT_RULES.get(key, res.rule)
                out[(side, key)] = res
        return out

    # ------------------------------------------------------------------ judging

    async def _judge(self, w: _FWork, prof: sc.ScalpProfile, p: sc.ScalpParams) -> dict[str, Any]:
        research = self._research.get(prof.key, {})
        result: dict[str, Any] = {
            "symbol": w.asset.symbol, "name": w.asset.name, "horizon": prof.key, "side": None,
            "signal": SignalLabel.NO_TRADE.value, "label": "NO TRADE", "status": "", "strategy": None, "setup": None,
            "setup_time": None, "price": w.price, "mark": w.mark, "funding_pct": w.funding_pct, "listed": w.listed,
            "plan": None, "leverage": None, "pending": None, "reasons": [], "risks": [], "board": None,
            "record": None, "expectancy_r": None, "filtered_by": None, "would_be": None, "also": [],
            "generated_at": utcnow(), "data_ok": w.integrity_ok,
        }
        if w.error:
            result.update(reasons=[f"analysis failed: {w.error}"], status="analysis failed")
            return result
        if not w.integrity_ok:
            result.update(reasons=[f"data integrity: {r}" for r in (w.integrity_reasons or ["integrity gate failed"])[:3]],
                          status="data check failed")
            return result
        if w.long is None or w.short is None:
            result.update(reasons=w.why[:2] or ["not enough history"], status="not enough history", signal="WATCH", label="WATCH")
            return result

        def rank(side: str, key: str) -> float:
            r = research.get((side, key))
            return r.test.expectancy_r if r is not None and r.validated and r.test.expectancy_r is not None else -9.0

        options = []
        for a in (w.long, w.short):
            for key, cand in a.now.items():
                label, why = st.verdict(key, a.records.get(key, []), research.get((a.side, key)))
                options.append((label, rank(a.side, key), a, key, cand, why))
        options.sort(key=lambda o: (o[0].rank, o[1]), reverse=True)
        validated = {side: [research[(side, k)].name for k in st.STRATEGIES if research.get((side, k)) and research[(side, k)].validated]
                     for side in fu.SIDES}
        if not options:
            return self._waiting(result, w, validated)

        label, _, a, key, cand, why = options[0]
        side = a.side
        if (label.rank >= SignalLabel.BUY.rank and len(options) > 1 and options[1][2].side != side
                and options[1][0].rank >= SignalLabel.BUY.rank):
            label, why = SignalLabel.WATCH, ["long and short setups at the same time: no clear direction"] + why
        strategy = st.STRATEGIES[key]
        spec = strategy.exit if key in st.LIBRARY else st.CLASSIC_EXIT
        hold = max(1, int(round(prof.max_hold * (spec.hold_mult if key in st.LIBRARY else 1.0))))
        own = st.Split.of([t.r_multiple for t in a.records.get(key, [])])
        result.update(
            side=side, setup=key, setup_time=cand.time, expectancy_r=own.expectancy_r,
            strategy={"key": key, "name": strategy.name, "family": strategy.family, "source": strategy.source,
                      "rule": strategy.rule if side == "long" else fu.SHORT_RULES.get(key, strategy.rule), "hold": hold,
                      "spec": spec.as_dict() if key in st.LIBRARY else None, "target": cand.level},
            record=_clean(asdict(own)),
            also=[f"{st.STRATEGIES[o[3]].name} ({o[2].side}): {fu.label_text(o[2].side, o[0])}" for o in options[1:4]],
        )
        raw = label
        filtered_by = None
        filter_reasons: list[str] = []
        board = w.boards.get(side)
        if board is not None:
            result["board"] = _clean(board.as_dict())
            mode = getattr(self.evidence, "mode", "filter")
            new, reasons = ev.apply_to_label(label, board, mode)
            if new != label:
                label, filtered_by = new, "evidence"
                filter_reasons.extend(reasons)
            result["risks"] = [f"evidence: {n}" for n in board.notes]
        reasons = filter_reasons + fu.reasons_for(side, cand, prof) + why
        cap, cap_reasons, cap_risks = self._live_cap(w, side, cand, p)
        label, raw = label.cap(cap), raw.cap(cap)
        reasons = cap_reasons + reasons
        result["risks"] += cap_risks
        if w.listed is False:
            label = raw = SignalLabel.NO_TRADE
            reasons.insert(0, f"no USDT perpetual for {w.asset.symbol} on the reachable exchanges")
        elif w.listed is None:
            result["risks"].append("could not confirm a USDT perpetual for this coin (futures data unreachable)")
        risk = self._risk()
        r_unit = abs(cand.entry - cand.stop)
        valid_until = cand.time + timedelta(seconds=prof.setup.seconds * p.fresh_candles)
        if utcnow() > valid_until + timedelta(seconds=prof.setup.seconds):
            if label.rank > SignalLabel.WATCH.rank:
                reasons.insert(0, "signal is older than its entry window")
            label, raw = label.cap(SignalLabel.WATCH), raw.cap(SignalLabel.WATCH)
        cost = 2.0 * (self.fee_pct + self.slippage_pct)
        lev = fu.leverage_plan(side, cand.entry, cand.stop, cost_pct=cost, funding_rate_pct_8h=w.funding_pct,
                               hold_hours=hold * prof.setup.minutes / 60.0, risk_pct_equity=risk.max_risk_per_signal_pct,
                               max_leverage=self.max_leverage, mmr_pct=self.mmr_pct,
                               equity=self._equity() if self._equity is not None else None)
        zone = ((cand.entry - 0.3 * r_unit, cand.entry + p.max_chase_r * r_unit) if side == "long"
                else (cand.entry - p.max_chase_r * r_unit, cand.entry + 0.3 * r_unit))
        result["plan"] = _clean({
            "side": side, "entry": cand.entry, "entry_low": zone[0], "entry_high": zone[1], "stop": cand.stop,
            "tp1": cand.tp1, "tp2": cand.tp2, "risk_pct": cand.risk_pct, "cost_pct": cost,
            "exit_rule": fu.exit_text(side, key, cand, p), "valid_until": valid_until,
            "time_exit": f"close the rest after {fmt_duration(hold * prof.setup.minutes)} ({hold} x {prof.setup.label} candles)",
        })
        result["leverage"] = _clean(lev.as_dict())
        if w.mark and w.price:
            basis = (w.mark / w.price - 1.0) * 100.0
            result["basis_pct"] = basis
            if abs(basis) >= 0.3:
                result["risks"].append(f"perpetual trades {basis:+.2f}% away from spot: adjust the levels by that much")
        held = raw.rank >= SignalLabel.BUY.rank and label.rank < SignalLabel.BUY.rank
        result["status"] = ("setup now" if label.rank >= SignalLabel.BUY.rank
                            else "held back by a filter" if held and filtered_by else "setup, not taken")
        if held:
            result["filtered_by"], result["would_be"] = filtered_by or "live check", fu.label_text(side, raw)
        result["signal"], result["label"] = label.value, fu.label_text(side, label)
        result["reasons"] = reasons
        result["summary"] = self._summary(result)
        if label.rank >= SignalLabel.BUY.rank or (held and filtered_by):
            await self._persist(result, prof, key, side)
        return result

    def _waiting(self, result: dict[str, Any], w: _FWork, validated: dict[str, list[str]]) -> dict[str, Any]:
        sides = [a for a in (w.long, w.short) if a is not None and a.context_ok]
        a = sides[0] if sides else None
        result["signal"] = result["label"] = "WATCH" if a is not None else "NO TRADE"
        if a is not None:
            result["side"] = a.side
            result["pending"] = _clean(a.pending) if a.pending else None
            result["status"] = f"waiting ({a.side})"
            result["reasons"] = [f"no setup yet: {a.pending['text']}" if a.pending else f"{a.side} trend context is right; waiting for an entry"]
            if validated[a.side]:
                result["reasons"].append(f"validated {a.side} strategies here: {', '.join(validated[a.side])}")
            board = w.boards.get(a.side)
            if board is not None:
                result["board"] = _clean(board.as_dict())
        else:
            result["status"] = "no trend either way"
            result["reasons"] = ["neither the long nor the short trend context is in place (range or mixed timeframes)"]
        result["summary"] = f"{result['label']}: {result['reasons'][0]}"
        return result

    def _live_cap(self, w: _FWork, side: str, cand: sc.Candidate, p: sc.ScalpParams) -> tuple[SignalLabel, list[str], list[str]]:
        cap = SignalLabel.STRONG_BUY
        reasons: list[str] = []
        risks: list[str] = []
        price = w.price
        r_unit = abs(cand.entry - cand.stop)
        sign = 1.0 if side == "long" else -1.0
        risk = self._risk()
        if price is None:
            cap = SignalLabel.NO_TRADE
            reasons.append("live price unavailable")
        elif sign * (price - cand.stop) <= 0:
            cap = SignalLabel.NO_TRADE
            reasons.append(f"invalidated: price {fmt_price(price)} is beyond the stop {fmt_price(cand.stop)}")
        elif sign * (price - cand.tp1) >= 0:
            cap = cap.cap(SignalLabel.WATCH)
            reasons.append(f"missed: price {fmt_price(price)} already reached TP1 {fmt_price(cand.tp1)}")
        elif sign * (price - cand.entry) > p.max_chase_r * r_unit:
            cap = cap.cap(SignalLabel.WATCH)
            reasons.append(f"price ran {abs(price - cand.entry) / r_unit:.2f}R past the signal entry: wait for a retest of "
                           f"{fmt_price(cand.entry)} or skip")
        usd = w.quote_usd_rate or (1.0 if (w.quote_asset or "").upper() in USD_STABLE_QUOTES else 0.0)
        book = w.book
        if book is None or not book.valid:
            cap = SignalLabel.NO_TRADE
            reasons.append("order book unavailable")
        else:
            if book.spread_bps is not None and book.spread_bps > risk.max_spread_bps:
                cap = SignalLabel.NO_TRADE
                reasons.append(f"spread {book.spread_bps:.1f} bps above the {risk.max_spread_bps:g} bps limit")
            depth = min(book.bid_depth_quote or 0.0, book.ask_depth_quote or 0.0) * usd
            if depth < risk.min_depth_usd:
                cap = SignalLabel.NO_TRADE
                reasons.append(f"order book too thin (${depth:,.0f} on the thinner side)")
            imb = book.imbalance
            if imb is not None and sign * imb <= risk.min_book_imbalance_strong:
                cap = cap.cap(SignalLabel.BUY)
                risks.append(f"the order book leans against this {side} (imbalance {imb:+.2f})")
        volume_usd = (w.volume_24h_quote or 0.0) * usd
        if volume_usd < risk.min_volume_24h_usd:
            cap = SignalLabel.NO_TRADE
            reasons.append(f"24h volume ${volume_usd:,.0f} below the ${risk.min_volume_24h_usd:,.0f} minimum")
        return cap, reasons, risks

    @staticmethod
    def _summary(r: dict[str, Any]) -> str:
        plan, lev = r.get("plan"), r.get("leverage")
        if plan and r["signal"] in ("BUY", "STRONG BUY"):
            return (f"{r['label']} {r['symbol']} ({r['strategy']['name']}): entry {fmt_price(plan['entry'])}, stop "
                    f"{fmt_price(plan['stop'])}, TP1 {fmt_price(plan['tp1'])}; {lev['leverage']}x, liquidation "
                    f"{fmt_price(lev['liquidation_price'])}")
        return f"{r['label']}: {r['reasons'][0]}" if r.get("reasons") else r["label"]

    def _no_trade_reason(self, prof: sc.ScalpProfile, results: list[dict[str, Any]]) -> str | None:
        if any(r["signal"] in ("BUY", "STRONG BUY") for r in results):
            return None
        research = self._research.get(prof.key, {})
        valid = [f"{r.name} ({side})" for (side, _), r in research.items() if r.validated]
        fired = [r for r in results if r.get("plan")]
        if not valid:
            promising = [f"{r.name} ({side}, {r.all.expectancy_r:+.2f}R over {r.all.trades})" for (side, _), r in research.items()
                         if (r.train.expectancy_r or 0) > 0 and (r.test.expectancy_r or 0) > 0 and r.reasons[0].startswith("only")]
            if promising:
                text = (f"Promising but not proven yet: {', '.join(promising[:3])}. Select more coins so the research has "
                        "enough trades.")
            else:
                text = ("No long or short strategy made money after futures costs on the selected coins at this horizon, "
                        "on both the older and the newer part of the history, so there is no trade. Try another horizon or "
                        "more coins.")
        else:
            text = f"Validated here: {', '.join(valid)}."
            if not fired:
                text += " None of them fired on the last candles; scan again after the next candle closes."
        if fired:
            details = "; ".join(f"{r['symbol']} {r['side']} ({(r['strategy'] or {}).get('name', r['setup'])}): {r['reasons'][0]}"
                                for r in fired[:3])
            text += f" {len(fired)} setup(s) fired but are not trades: {details}."
        return text

    # ------------------------------------------------------------------ persistence

    async def _persist(self, r: dict[str, Any], prof: sc.ScalpProfile, key: str, side: str) -> None:
        if self._sessions is None or r.get("plan") is None or r.get("setup_time") is None:
            return
        memo = (r["symbol"], prof.key, side)
        if self._stored.get(memo) == r["setup_time"]:
            return
        strategy = strategy_name(prof.key)
        plan, lev = r["plan"], r["leverage"]
        held = bool(r.get("would_be"))
        try:
            async with self._sessions() as session:
                exists = (await session.execute(
                    select(Signal.id).where(Signal.symbol == r["symbol"], Signal.strategy == strategy,
                                            Signal.created_at >= r["setup_time"]).limit(1)
                )).scalar_one_or_none()
                if exists is None:
                    signal = Signal(
                        symbol=r["symbol"], timeframe=prof.setup.value, strategy=strategy,
                        signal=("WATCH" if held else r["label"])[:16],
                        signal_score=int(round((r.get("record") or {}).get("win_rate") or 0)),
                        data_health_score=100, data_state="HEALTHY", entry_low=min(plan["entry_low"], plan["entry_high"]),
                        entry_high=plan["entry"], stop_loss=plan["stop"],
                        risk_reward=round(abs(plan["tp2"] - plan["entry"]) / abs(plan["entry"] - plan["stop"]), 4),
                        trend="UP" if side == "long" else "DOWN", market_regime=None, reasons=list(r["reasons"]),
                        risks=list(r["risks"]), invalidation=f"a {prof.setup.label} close beyond {fmt_price(plan['stop'])}",
                        summary=r.get("summary"), status="FILTERED" if held else "OPEN", engine_version=FUTURES_VERSION,
                        input_features=_jsonable({"setup": key, "setup_time": r["setup_time"], "horizon": prof.key,
                                                  "side": side, "evidence": evidence_record(r.get("board"))}),
                        quant_output=_jsonable({"side": side, "strategy": key, "max_hold": (r["strategy"] or {}).get("hold"),
                                                "exit": (r["strategy"] or {}).get("spec"), "target": (r["strategy"] or {}).get("target"),
                                                "leverage": lev, "filtered_by": r.get("filtered_by"), "would_be": r.get("would_be"),
                                                "record": r.get("record")}),
                    )
                    session.add(signal)
                    await session.flush()
                    session.add_all([
                        SignalTarget(signal_id=signal.id, kind="TP", level_index=1, price=plan["tp1"], allocation_pct=50.0),
                        SignalTarget(signal_id=signal.id, kind="TP", level_index=2, price=plan["tp2"], allocation_pct=50.0),
                        SignalTarget(signal_id=signal.id, kind="SL", level_index=1, price=plan["stop"], allocation_pct=100.0),
                    ])
                    await session.commit()
        except Exception:
            log.exception("futures signal persistence failed", extra={"symbol": r["symbol"]})
            return
        self._stored[memo] = r["setup_time"]
