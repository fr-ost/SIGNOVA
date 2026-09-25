"""Learning service (Phase 10): measures the evidence board on closed setups and, once validated,
lets its learned model hold back buys it expects to lose.

Refreshed after scans (at most every 30 minutes) and on request. It reads only the database.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis import evidence_learn as el
from app.analysis import ml
from app.analysis.evidence import Evidence
from app.core.enums import SignalLabel
from app.core.timeutil import utcnow
from app.models import Signal, SignalOutcome
from app.services.settings_store import SettingsStore

log = logging.getLogger(__name__)

REFRESH_SECONDS = 1800
HORIZON_OF = {"mtf_trend_pullback": "swing", "scalp_15m": "15m", "scalp_1h": "1h", "scalp_4h": "4h", "scalp_1d": "1d"}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class LearningService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None, store: SettingsStore) -> None:
        self._sessions = session_factory
        self._store = store
        self.model: ml.Model | None = None
        self.enabled = True
        self.report: dict[str, Any] = {"samples": 0}
        self._refreshed = 0.0

    async def load(self) -> None:
        data = await self._store.get("evidence_model", None)
        if isinstance(data, dict):
            try:
                self.model = ml.Model.from_dict(data)
            except (KeyError, TypeError, ValueError):
                self.model = None
        self.enabled = bool(await self._store.get("evidence_model_enabled", True))
        report = await self._store.get("evidence_report", None)
        if isinstance(report, dict):
            self.report = report

    async def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        await self._store.set("evidence_model_enabled", enabled)

    def gate(self, label: SignalLabel, evidence: Evidence, horizon: str) -> tuple[SignalLabel, list[str]]:
        model = self.model
        if model is None or not model.validated or not self.enabled or label not in (SignalLabel.BUY, SignalLabel.STRONG_BUY):
            return label, []
        h = "swing" if horizon == "swing" else horizon
        p = model.probability(el.model_features(evidence.features(), h, evidence.score))
        if p < model.threshold:
            return SignalLabel.WATCH, [
                f"learned evidence model: win probability {p * 100:.0f}% is below its {model.threshold * 100:.0f}% threshold "
                "(validated on newer outcomes)"]
        return label, []

    def probability(self, evidence: Evidence, horizon: str) -> float | None:
        if self.model is None:
            return None
        return self.model.probability(el.model_features(evidence.features(), horizon, evidence.score))

    def status(self) -> dict[str, Any]:
        model = self.model
        return {
            **self.report,
            "needed": ml.MIN_TRAIN + ml.MIN_TEST,
            "enabled": self.enabled,
            "model": None if model is None else {
                "validated": model.validated, "threshold": model.threshold, "trained_at": model.trained_at,
                "metrics": {k: v for k, v in model.metrics.items() if k != "top_features"},
                "top_features": model.metrics.get("top_features"), "reasons": model.reasons,
            },
        }

    async def refresh(self, *, force: bool = False) -> dict[str, Any]:
        if self._sessions is None:
            return self.status()
        if not force and time.monotonic() - self._refreshed < REFRESH_SECONDS:
            return self.status()
        self._refreshed = time.monotonic()
        try:
            async with self._sessions() as session:
                rows = (await session.execute(
                    select(Signal, SignalOutcome).join(SignalOutcome, SignalOutcome.signal_id == Signal.id)
                    .where(Signal.created_at >= utcnow() - timedelta(days=365))
                )).all()
        except Exception:
            log.exception("learning query failed")
            return self.status()
        samples: list[el.Sample] = []
        for sig, out in rows:
            evidence = (sig.input_features or {}).get("evidence") if isinstance(sig.input_features, dict) else None
            if not isinstance(evidence, dict) or not isinstance(evidence.get("features"), dict):
                continue
            samples.append(el.Sample(
                time=_aware(sig.created_at), horizon=HORIZON_OF.get(sig.strategy, "swing"),
                features={k: float(v) for k, v in evidence["features"].items() if isinstance(v, int | float)},
                grade=str(evidence.get("grade") or "thin"), score=evidence.get("score"),
                r_multiple=float(out.r_multiple or 0.0), shown=not sig.status.startswith("FILTERED"),
                filtered_by=(sig.quant_output or {}).get("filtered_by") if isinstance(sig.quant_output, dict) else None,
            ))
        samples.sort(key=lambda s: s.time)
        now = utcnow()
        model = el.train(samples, now)
        if model is not None:
            self.model = model
            await self._store.set("evidence_model", model.as_dict())
        shown = [s for s in samples if s.shown]
        held = [s for s in samples if not s.shown]
        self.report = {
            "refreshed_at": now.isoformat(), "samples": len(samples), "shown": len(shown), "held_back": len(held),
            "factors": el.factor_table(samples), "grades": el.grade_table(samples),
            "shown_stats": el.stats_of([s.r_multiple for s in shown]),
            "held_stats": el.stats_of([s.r_multiple for s in held]),
        }
        await self._store.set("evidence_report", self.report)
        return self.status()
