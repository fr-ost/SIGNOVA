"""Process-wide system state (single-worker deployment).

Phase 1 tracks the aggregate data state. The processing lifecycle (START ANALYSIS,
LIVE MONITOR, EMERGENCY STOP) builds on this store in the on-demand phase.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import DataState, ProcessingState
from app.core.timeutil import utcnow


@dataclass
class SystemStateSnapshot:
    processing_state: ProcessingState
    data_state: DataState | None
    data_state_reasons: list[str]
    signals_paused: bool
    signal_paused_reason: str | None
    updated_at: datetime


@dataclass
class SystemStateStore:
    processing_state: ProcessingState = ProcessingState.IDLE
    data_state: DataState | None = None
    data_state_reasons: list[str] = field(default_factory=list)
    signal_paused_reason: str | None = None
    updated_at: datetime = field(default_factory=utcnow)

    def update_data_state(self, state: DataState, reasons: list[str]) -> bool:
        """Record the aggregate data state. Returns True when the state changed."""
        changed = state != self.data_state
        self.data_state = state
        self.data_state_reasons = list(reasons)
        self.updated_at = utcnow()
        return changed

    def snapshot(self) -> SystemStateSnapshot:
        return SystemStateSnapshot(
            processing_state=self.processing_state,
            data_state=self.data_state,
            data_state_reasons=list(self.data_state_reasons),
            signals_paused=self.signal_paused_reason is not None,
            signal_paused_reason=self.signal_paused_reason,
            updated_at=self.updated_at,
        )
