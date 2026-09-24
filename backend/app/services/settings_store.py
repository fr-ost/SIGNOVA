"""Small JSON preferences in the `app_settings` table (lab results, applied variants, models).

Values are cached in memory; without a database they live for the process lifetime only.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timeutil import utcnow
from app.models import AppSetting

log = logging.getLogger(__name__)


class SettingsStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None) -> None:
        self._sessions = session_factory
        self._cache: dict[str, Any] = {}

    async def get(self, key: str, default: Any = None) -> Any:
        if key in self._cache:
            return self._cache[key]
        if self._sessions is None:
            return default
        try:
            async with self._sessions() as session:
                row = await session.get(AppSetting, key)
        except Exception:
            log.exception("setting could not be read", extra={"key": key})
            return default
        value = row.value.get("v", default) if row is not None and isinstance(row.value, dict) else default
        self._cache[key] = value
        return value

    async def set(self, key: str, value: Any) -> None:
        self._cache[key] = value
        if self._sessions is None:
            return
        try:
            async with self._sessions() as session:
                row = await session.get(AppSetting, key)
                if row is None:
                    session.add(AppSetting(key=key, value={"v": value}, updated_at=utcnow()))
                else:
                    row.value, row.updated_at = {"v": value}, utcnow()
                await session.commit()
        except Exception:
            log.exception("setting could not be saved (kept in memory)", extra={"key": key})
