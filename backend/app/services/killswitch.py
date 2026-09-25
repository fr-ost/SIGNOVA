"""Emergency stop: one switch that halts everything that touches the outside world.

Engaging it stops the running scans (swing and scalp), the auto-analyze schedule, the strategy
lab and live prices, pauses signals, and makes the API refuse every request that would call a
provider, a news source or OpenAI, until it is released. It is stored in the database, so a
restart keeps it engaged (the auto schedule does not come back on its own). Reading what is
already stored (last scan, track record, watchlist) keeps working.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import ProcessingState
from app.core.timeutil import parse_iso, utcnow
from app.models import AppSetting
from app.services.system_state import SystemStateStore

log = logging.getLogger(__name__)

KEY = "emergency_stop"
PAUSE_REASON = "emergency stop engaged"

# Allowed while the switch is engaged (everything else answers 503).
ALLOW_ANY_PREFIX = (
    "/static/", "/api/control/kill", "/api/control/resume", "/api/control/stop", "/api/control/selection",
    "/api/watchlist",
)
ALLOW_GET = {
    "/", "/health", "/api", "/api/docs", "/api/openapi.json", "/api/control/status", "/api/system/state",
    "/api/performance", "/api/signals", "/api/signals/history", "/api/scalp", "/api/lab", "/api/ml",
    "/api/evidence/settings", "/api/evidence/learning",
}


def allowed_during_stop(method: str, path: str) -> bool:
    if path.startswith(ALLOW_ANY_PREFIX):
        return True
    return method in ("GET", "HEAD") and path.rstrip("/") in ALLOW_GET | {""}


class KillSwitch:
    def __init__(self, state: SystemStateStore, session_factory: async_sessionmaker[AsyncSession] | None) -> None:
        self._state = state
        self._sessions = session_factory
        self.active = False
        self.since: datetime | None = None
        self.reason: str | None = None

    def status(self) -> dict[str, Any]:
        return {"active": self.active, "since": self.since, "reason": self.reason}

    def _apply(self) -> None:
        self._state.emergency_stop = self.active
        if self.active:
            self._state.processing_state = ProcessingState.EMERGENCY_STOP
            self._state.signal_paused_reason = PAUSE_REASON
        else:
            if self._state.processing_state == ProcessingState.EMERGENCY_STOP:
                self._state.processing_state = ProcessingState.IDLE
            if self._state.signal_paused_reason == PAUSE_REASON:
                self._state.signal_paused_reason = None

    async def load(self) -> None:
        if self._sessions is None:
            return
        try:
            async with self._sessions() as session:
                row = await session.get(AppSetting, KEY)
        except Exception:
            log.exception("emergency stop state could not be read")
            return
        if row is not None and isinstance(row.value, dict) and row.value.get("active"):
            self.active = True
            self.since = parse_iso(row.value.get("since")) or utcnow()
            self.reason = row.value.get("reason")
            self._apply()
            log.warning("emergency stop is engaged (restored after restart)")

    async def _save(self) -> None:
        if self._sessions is None:
            return
        value = {"active": self.active, "since": self.since.isoformat() if self.since else None, "reason": self.reason}
        try:
            async with self._sessions() as session:
                row = await session.get(AppSetting, KEY)
                if row is None:
                    session.add(AppSetting(key=KEY, value=value, updated_at=utcnow()))
                else:
                    row.value, row.updated_at = value, utcnow()
                await session.commit()
        except Exception:
            log.exception("emergency stop state could not be saved (kept in memory)")

    async def engage(self, reason: str | None = None) -> None:
        self.active = True
        self.since = utcnow()
        self.reason = (reason or "manual emergency stop")[:200]
        self._apply()
        await self._save()
        log.warning("EMERGENCY STOP engaged", extra={"reason": self.reason})

    async def release(self) -> None:
        self.active = False
        self.since = None
        self.reason = None
        self._apply()
        await self._save()
        log.warning("emergency stop released")
