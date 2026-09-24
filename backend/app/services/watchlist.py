"""User watchlist: coins added manually and analysed alongside the Top 20.

Kept in memory for the universe builder and persisted in the `watchlist` table so it
survives restarts. Without a database it lives for the process lifetime only.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import WatchlistItem

log = logging.getLogger(__name__)

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{1,15}$")
MAX_ITEMS = 20


class WatchlistError(ValueError):
    pass


class WatchlistService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._sessions = session_factory
        self._on_change = on_change
        self._items: dict[str, str | None] = {}
        self._load_lock = asyncio.Lock()
        self._loaded = False

    def symbols(self) -> list[str]:
        return list(self._items)

    def items(self) -> list[tuple[str, str | None]]:
        return list(self._items.items())

    async def load(self, *, force: bool = False) -> None:
        async with self._load_lock:
            if self._loaded and not force:
                return
            await self._load()
            self._loaded = True
        if self._items:
            self._changed()  # rebuild a universe that may have been built before the load

    async def _load(self) -> None:
        if self._sessions is None:
            return
        try:
            async with self._sessions() as session:
                rows = (await session.execute(select(WatchlistItem).order_by(WatchlistItem.id))).scalars().all()
        except Exception:
            log.exception("watchlist load failed")
            return
        self._items = {row.symbol: row.note for row in rows}

    @staticmethod
    def normalize(symbol: str) -> str:
        value = (symbol or "").strip().upper()
        if not SYMBOL_PATTERN.match(value):
            raise WatchlistError("symbol must be 1-15 letters or digits, e.g. PEPE")
        return value

    async def add(self, symbol: str, note: str | None = None) -> str:
        value = self.normalize(symbol)
        await self.load()
        if value in self._items:
            return value
        if len(self._items) >= MAX_ITEMS:
            raise WatchlistError(f"the watchlist holds at most {MAX_ITEMS} coins")
        clean_note = (note or "").strip()[:255] or None
        if self._sessions is not None:
            async with self._sessions() as session:
                session.add(WatchlistItem(symbol=value, note=clean_note))
                await session.commit()
        self._items[value] = clean_note
        self._changed()
        return value

    async def remove(self, symbol: str) -> bool:
        value = self.normalize(symbol)
        await self.load()
        if value not in self._items:
            return False
        if self._sessions is not None:
            async with self._sessions() as session:
                await session.execute(delete(WatchlistItem).where(WatchlistItem.symbol == value))
                await session.commit()
        del self._items[value]
        self._changed()
        return True

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()
