"""Track record: what happened to every buy signal after it was shown.

Each stored BUY / STRONG BUY (swing and scalp) is followed on the candles after it, with the
same pessimistic simulator the backtest uses (app.analysis.trade_sim): entry at the price
when the signal was shown, the plan's stop and targets, the stop moved to break-even after
the first target, and a time limit (14 days for swing signals, the horizon's limit for
scalps). A signal that appears while an earlier signal of the same strategy on the same
coin is still running is marked SKIPPED, so one market move is never counted twice.

Setups a filter held back (the evidence board, the learned model or the statistical filter;
status FILTERED) are followed the same way in their own lane, so the record can show whether
holding them back was right. They never count in the strategy's own record.

It uses candles the analysis already fetched (no extra provider calls) and only needs the
database. Results are measured, forward-looking evidence: unlike a backtest, nothing here
was known when the rules were written.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.engine import STRATEGY as SWING_STRATEGY
from app.analysis.trade_sim import OPEN, SimTarget, simulate_long
from app.core.enums import Timeframe
from app.core.timeutil import utcnow
from app.data.normalization.schemas import Candle
from app.models import Signal, SignalOutcome, SignalTarget

log = logging.getLogger(__name__)

TRACK_TIMEFRAME = {
    SWING_STRATEGY: Timeframe.H1,
    "scalp_15m": Timeframe.M5,
    "scalp_1h": Timeframe.M15,
    "scalp_4h": Timeframe.H1,
    "scalp_1d": Timeframe.H4,
}
SWING_MAX_HOLD_HOURS = 14 * 24
BUY_LABELS = ("BUY", "STRONG BUY")
FILTERED = "FILTERED"  # held back by a filter; followed in its own lane
STRATEGY_LABELS = {
    SWING_STRATEGY: "Swing (4H setup)",
    "scalp_15m": "Scalp 15m",
    "scalp_1h": "Trade 1h",
    "scalp_4h": "Trade 4h",
    "scalp_1d": "Trade 1d",
}


def _max_hold(signal: Signal) -> int:
    if signal.strategy == SWING_STRATEGY:
        return SWING_MAX_HOLD_HOURS
    value = (signal.quant_output or {}).get("max_hold")
    return int(value) if isinstance(value, int | float) and value > 0 else 8


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def default_cost_pct(strategy: str) -> float:
    """Round-trip costs in percent: fees 0.1% per side, slippage 0.05% (swing) or 0.02% (scalp)."""
    return 2.0 * (0.1 + (0.05 if strategy == SWING_STRATEGY else 0.02))


class OutcomeTracker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        cost_pct: Callable[[str], float] = default_cost_pct,
    ) -> None:
        self._sessions = session_factory
        self._cost_pct = cost_pct

    async def update(self, symbol: str, candles: dict[Timeframe, Sequence[Candle]]) -> int:
        """Evaluate this coin's open buy signals whose tracking timeframe is in `candles`.
        Returns the number of signals that got an outcome."""
        if self._sessions is None:
            return 0
        strategies = [s for s, tf in TRACK_TIMEFRAME.items() if tf in candles and candles[tf]]
        if not strategies:
            return 0
        finished = 0
        try:
            async with self._sessions() as session:
                rows = (
                    await session.execute(
                        select(Signal)
                        .where(Signal.symbol == symbol.upper(), Signal.strategy.in_(strategies),
                               or_(and_(Signal.status == "OPEN", Signal.signal.in_(BUY_LABELS)), Signal.status == FILTERED))
                        .order_by(Signal.created_at, Signal.id)
                    )
                ).scalars().all()
                if not rows:
                    return 0
                targets = await self._targets(session, [r.id for r in rows])
                busy_until: dict[tuple[str, bool], datetime] = {}  # (strategy, held back) lanes
                for lane in {(r.strategy, r.status == FILTERED) for r in rows}:
                    last_exit = await self._last_exit(session, symbol.upper(), lane[0], filtered=lane[1])
                    if last_exit is not None:
                        busy_until[lane] = last_exit
                blocked: set[tuple[str, bool]] = set()  # lanes with a still-running earlier trade
                for signal in rows:
                    created = _aware(signal.created_at)
                    held = signal.status == FILTERED
                    lane = (signal.strategy, held)
                    prefix = "FILTERED_" if held else ""
                    if lane in blocked:
                        continue
                    if lane in busy_until and created < busy_until[lane]:
                        signal.status = prefix + ("SKIP" if held else "SKIPPED")
                        continue
                    series = candles[TRACK_TIMEFRAME[signal.strategy]]
                    outcome = self._evaluate(signal, targets.get(signal.id, []), series, self._cost_pct(signal.strategy))
                    if outcome is None:
                        if self._unreachable(signal, series):
                            signal.status = prefix + ("EXP" if held else "EXPIRED")  # its candles are gone
                            continue
                        blocked.add(lane)  # still running: later signals wait for it
                        continue
                    session.add(outcome)
                    signal.status = prefix + ("WIN" if (outcome.r_multiple or 0) > 0 else "LOSS")
                    busy_until[lane] = created + timedelta(seconds=outcome.time_to_outcome_seconds or 0)
                    finished += 1
                await session.commit()
        except Exception:
            log.exception("track record update failed", extra={"symbol": symbol})
            return 0
        return finished

    @staticmethod
    async def _targets(session: AsyncSession, ids: list[int]) -> dict[int, list[SignalTarget]]:
        rows = (
            await session.execute(
                select(SignalTarget).where(SignalTarget.signal_id.in_(ids), SignalTarget.kind == "TP")
                .order_by(SignalTarget.level_index)
            )
        ).scalars().all()
        out: dict[int, list[SignalTarget]] = {}
        for t in rows:
            out.setdefault(t.signal_id, []).append(t)
        return out

    @staticmethod
    async def _last_exit(session: AsyncSession, symbol: str, strategy: str, *, filtered: bool = False) -> datetime | None:
        lane = Signal.status.like("FILTERED%") if filtered else ~Signal.status.like("FILTERED%")
        row = (
            await session.execute(
                select(Signal.created_at, SignalOutcome.time_to_outcome_seconds)
                .join(SignalOutcome, SignalOutcome.signal_id == Signal.id)
                .where(Signal.symbol == symbol, Signal.strategy == strategy, lane)
                .order_by(Signal.created_at.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        return _aware(row[0]) + timedelta(seconds=row[1] or 0)

    @staticmethod
    def _unreachable(signal: Signal, candles: Sequence[Candle]) -> bool:
        """The candles start after the signal and its whole holding period has passed."""
        tf_seconds = candles[0].timeframe.seconds
        created = _aware(signal.created_at)
        window = timedelta(seconds=tf_seconds * _max_hold(signal))
        return candles[0].open_time > created + timedelta(seconds=tf_seconds) and utcnow() > created + window

    @staticmethod
    def _evaluate(
        signal: Signal, targets: list[SignalTarget], candles: Sequence[Candle], cost_pct: float
    ) -> SignalOutcome | None:
        entry, stop = signal.entry_high, signal.stop_loss
        if entry is None or stop is None or not (0 < stop < entry) or not targets:
            return None
        created = _aware(signal.created_at)
        start = next((k for k, c in enumerate(candles) if c.open_time >= created), None)
        if start is None:
            return None  # no candle after the signal yet
        if candles[0].open_time > created + timedelta(seconds=candles[0].timeframe.seconds):
            return None  # these candles start after the signal: cannot follow it from the start
        quant = signal.quant_output if isinstance(signal.quant_output, dict) else {}
        trail = exit_signal = None
        breakeven = True
        if isinstance(quant.get("exit"), dict):  # a library strategy: the same exit rules as its backtest
            from app.analysis.strategies import ExitRules, ExitSpec, sim_targets as spec_targets

            spec = ExitSpec.from_dict(quant["exit"])
            sim_targets = spec_targets(entry, stop, spec, quant.get("target"))
            trail, exit_signal = ExitRules(candles).functions(spec, start - 1)
            breakeven = spec.breakeven
            if not sim_targets and trail is None and exit_signal is None:
                return None
        else:
            total = math.fsum(t.allocation_pct or 0 for t in targets) or 100.0
            sim_targets = [SimTarget(t.price, (t.allocation_pct or 0) / total) for t in targets if t.price > entry]
            if not sim_targets:
                return None
        res = simulate_long(candles, start, entry=entry, stop=stop, targets=sim_targets,
                            cost_pct=cost_pct, max_hold=_max_hold(signal), breakeven_after_first=breakeven,
                            trail=trail, exit_signal=exit_signal)
        if res.outcome == OPEN or res.exit_time is None:
            return None
        return SignalOutcome(
            signal_id=signal.id,
            outcome=res.outcome,
            exit_price=res.exit_price,
            return_pct=round(res.return_pct, 4),
            r_multiple=round(res.r_multiple, 4),
            mfe_pct=round(res.mfe_pct, 4),
            mae_pct=round(res.mae_pct, 4),
            time_to_outcome_seconds=int((res.exit_time - created).total_seconds()),
            hit_targets=res.hit_targets,
            evaluated_at=utcnow(),
        )

    async def performance(self, days: int = 90) -> dict[str, Any]:
        """Win rate, average R and recent outcomes per strategy over the last `days`."""
        empty: dict[str, Any] = {"days": days, "strategies": [], "recent": [], "persistence": "disabled"}
        if self._sessions is None:
            return empty
        since = utcnow() - timedelta(days=days)
        try:
            async with self._sessions() as session:
                closed = (
                    await session.execute(
                        select(Signal, SignalOutcome)
                        .join(SignalOutcome, SignalOutcome.signal_id == Signal.id)
                        .where(Signal.created_at >= since)
                        .order_by(Signal.created_at.desc())
                    )
                ).all()
                pending = (
                    await session.execute(
                        select(Signal.strategy, Signal.status)
                        .where(Signal.created_at >= since, Signal.signal.in_(BUY_LABELS),
                               Signal.status.in_(("OPEN", "SKIPPED")))
                    )
                ).all()
        except Exception:
            log.exception("track record query failed")
            return {**empty, "persistence": "failed"}
        by: dict[str, list[float]] = {}
        by_ai: dict[str, list[float]] = {}
        by_grade: dict[str, list[float]] = {}
        held: dict[str, list[float]] = {}
        shown_closed = []
        for sig, out in closed:
            if sig.status.startswith("FILTERED"):
                who = (sig.quant_output or {}).get("filtered_by") if isinstance(sig.quant_output, dict) else None
                held.setdefault(who or "filter", []).append(out.r_multiple or 0.0)
                continue
            shown_closed.append((sig, out))
            by.setdefault(sig.strategy, []).append(out.r_multiple or 0.0)
            evidence = (sig.input_features or {}).get("evidence") if isinstance(sig.input_features, dict) else None
            if isinstance(evidence, dict) and evidence.get("grade"):
                by_grade.setdefault(str(evidence["grade"]), []).append(out.r_multiple or 0.0)
            verdict = (sig.ai_output or {}).get("verdict") if isinstance(sig.ai_output, dict) else None
            by_ai.setdefault(verdict or "not reviewed", []).append(out.r_multiple or 0.0)
        open_count: dict[str, int] = {}
        skipped: dict[str, int] = {}
        for strategy, status in pending:
            bucket = open_count if status == "OPEN" else skipped
            bucket[strategy] = bucket.get(strategy, 0) + 1
        strategies = []
        for strategy in sorted(set(by) | set(open_count) | set(skipped), key=lambda s: list(STRATEGY_LABELS).index(s)
                               if s in STRATEGY_LABELS else 99):
            rs = by.get(strategy, [])
            gains = math.fsum(r for r in rs if r > 0)
            losses = -math.fsum(r for r in rs if r < 0)
            strategies.append({
                "strategy": strategy,
                "label": STRATEGY_LABELS.get(strategy, strategy),
                "closed": len(rs),
                "wins": sum(1 for r in rs if r > 0),
                "win_rate": 100.0 * sum(1 for r in rs if r > 0) / len(rs) if rs else None,
                "avg_r": statistics.fmean(rs) if rs else None,
                "total_r": math.fsum(rs),
                "profit_factor": (gains / losses) if losses > 0 else None,
                "open": open_count.get(strategy, 0),
                "skipped": skipped.get(strategy, 0),
            })
        recent = [
            {
                "symbol": sig.symbol, "strategy": sig.strategy, "label": STRATEGY_LABELS.get(sig.strategy, sig.strategy),
                "signal": sig.signal, "created_at": sig.created_at, "entry": sig.entry_high, "stop": sig.stop_loss,
                "outcome": out.outcome, "r_multiple": out.r_multiple, "return_pct": out.return_pct,
                "ai_verdict": (sig.ai_output or {}).get("verdict") if isinstance(sig.ai_output, dict) else None,
                "hit_targets": out.hit_targets, "hours": round((out.time_to_outcome_seconds or 0) / 3600, 1),
            }
            for sig, out in shown_closed[:25]
        ]
        ai_rows = [
            {"verdict": verdict, "closed": len(rs), "win_rate": 100.0 * sum(1 for r in rs if r > 0) / len(rs),
             "avg_r": statistics.fmean(rs), "total_r": math.fsum(rs)}
            for verdict, rs in sorted(by_ai.items()) if rs
        ]
        def row(rs: list[float]) -> dict[str, Any]:
            return {"closed": len(rs), "win_rate": 100.0 * sum(1 for r in rs if r > 0) / len(rs),
                    "avg_r": statistics.fmean(rs), "total_r": math.fsum(rs)}

        grade_order = ["strong_for", "for", "neutral", "against", "strong_against", "thin"]
        evidence_rows = [{"grade": g, **row(by_grade[g])} for g in grade_order if by_grade.get(g)]
        held_rows = [{"filtered_by": who, **row(rs)} for who, rs in sorted(held.items()) if rs]
        return {"days": days, "strategies": strategies, "recent": recent, "by_ai": ai_rows, "by_evidence": evidence_rows,
                "held_back": held_rows, "persistence": "ok"}
