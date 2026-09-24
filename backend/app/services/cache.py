"""Single-flight async TTL cache.

Concurrent requests for the same key share one in-flight load, so a dashboard with
many open tabs cannot multiply provider calls. Loader failures are not cached.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, TypeVar

T = TypeVar("T")


class AsyncTTLCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._values: dict[Hashable, tuple[float, float, Any]] = {}
        self._locks: dict[Hashable, asyncio.Lock] = {}

    def peek(self, key: Hashable) -> tuple[Any, float] | None:
        """Return (value, age_seconds) even if expired, or None."""
        entry = self._values.get(key)
        if entry is None:
            return None
        stored_at, _, value = entry
        return value, self._clock() - stored_at

    def invalidate(self, key: Hashable | None = None) -> None:
        if key is None:
            self._values.clear()
        else:
            self._values.pop(key, None)

    async def get_or_load(
        self,
        key: Hashable,
        loader: Callable[[], Awaitable[T]],
        ttl_seconds: float,
        *,
        force: bool = False,
    ) -> T:
        entry = self._values.get(key)
        if not force and entry is not None and self._clock() < entry[1]:
            return entry[2]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._values.get(key)
            if not force and entry is not None and self._clock() < entry[1]:
                return entry[2]
            value = await loader()
            now = self._clock()
            self._values[key] = (now, now + ttl_seconds, value)
            return value
