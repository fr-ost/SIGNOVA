"""Which coins a scan analyses (the user's manual selection).

Mode "all" analyses the whole universe (Top 20 plus the watchlist). Mode "selected" analyses
only the chosen coins, e.g. just five, which also saves provider calls. The choice is kept in
the `app_settings` table so it survives restarts. Market data for the table is unaffected.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from typing import Any, Literal, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timeutil import utcnow
from app.models import AppSetting
from app.services.watchlist import WatchlistService

log = logging.getLogger(__name__)

KEY = "scan_selection"
MAX_SELECTED = 60
Mode = Literal["all", "selected"]
T = TypeVar("T")


class SelectionError(ValueError):
    pass


class ScanSelectionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None) -> None:
        self._sessions = session_factory
        self.mode: Mode = "all"
        self._symbols: list[str] = []
        self._lock = asyncio.Lock()
        self._loaded = False

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    def includes(self, symbol: str) -> bool:
        return self.mode == "all" or symbol.upper() in self._symbols

    def filter(self, items: Sequence[T], symbol_of: Any = lambda a: a.symbol) -> list[T]:
        return list(items) if self.mode == "all" else [i for i in items if symbol_of(i) in self._symbols]

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "symbols": self.symbols}

    async def load(self) -> None:
        async with self._lock:
            if self._loaded or self._sessions is None:
                self._loaded = True
                return
            try:
                async with self._sessions() as session:
                    row = await session.get(AppSetting, KEY)
            except Exception:
                log.exception("scan selection load failed; analysing every coin")
                return
            if row is not None and isinstance(row.value, dict):
                mode = row.value.get("mode")
                self.mode = "selected" if mode == "selected" else "all"
                self._symbols = [s for s in row.value.get("symbols", []) if isinstance(s, str)][:MAX_SELECTED]
            self._loaded = True

    async def set(self, mode: str, symbols: Iterable[str] = ()) -> dict[str, Any]:
        if mode not in ("all", "selected"):
            raise SelectionError("mode must be 'all' or 'selected'")
        cleaned = list(dict.fromkeys(WatchlistService.normalize(s) for s in symbols))
        if len(cleaned) > MAX_SELECTED:
            raise SelectionError(f"at most {MAX_SELECTED} coins can be selected")
        if mode == "selected" and not cleaned:
            raise SelectionError("select at least one coin, or choose 'all'")
        await self.load()
        self.mode = "selected" if mode == "selected" else "all"
        self._symbols = cleaned
        await self._save()
        return self.status()

    async def include(self, symbol: str) -> None:
        """A coin the user just added to the watchlist joins an active selection."""
        value = symbol.upper()
        if self.mode == "selected" and value not in self._symbols and len(self._symbols) < MAX_SELECTED:
            self._symbols.append(value)
            await self._save()

    async def _save(self) -> None:
        if self._sessions is None:
            return
        try:
            async with self._sessions() as session:
                row = await session.get(AppSetting, KEY)
                value = {"mode": self.mode, "symbols": self._symbols}
                if row is None:
                    session.add(AppSetting(key=KEY, value=value, updated_at=utcnow()))
                else:
                    row.value = value
                    row.updated_at = utcnow()
                await session.commit()
        except Exception:
            log.exception("scan selection save failed (kept in memory)")
