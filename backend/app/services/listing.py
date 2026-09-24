"""Market-cap listing with provider fallback: CoinMarketCap -> CoinGecko -> CoinPaprika."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.core.timeutil import utcnow
from app.data.adapters.base import ListingAdapter
from app.data.http import ProviderError
from app.data.normalization.schemas import ListingEntry
from app.data.validation.listings import validate_listing
from app.services.cache import AsyncTTLCache

log = logging.getLogger(__name__)


class ListingUnavailable(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("no listing provider returned valid data: " + "; ".join(errors))
        self.errors = errors


@dataclass
class ListingResult:
    entries: list[ListingEntry]
    source: str
    fallback_used: bool
    fetched_at: datetime
    newest_update_age_seconds: float | None
    issues: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    access: str | None = None  # "pro" / "keyless" for CoinMarketCap; None for other sources


class ListingService:
    def __init__(
        self,
        adapters: list[ListingAdapter],
        *,
        fetch_limit: int,
        min_entries: int,
        max_age_seconds: float,
        refresh_seconds: float = 60.0,
    ) -> None:
        if not adapters:
            raise ValueError("at least one listing adapter is required")
        self._adapters = adapters
        self._fetch_limit = fetch_limit
        self._min_entries = min_entries
        self._max_age = max_age_seconds
        self._refresh = refresh_seconds
        self._cache = AsyncTTLCache()

    async def latest(self, *, force: bool = False) -> ListingResult:
        """Cached listing shared by the universe builder and reference-price checks.

        A short TTL keeps reference prices fresh enough for cross-validation while
        limiting calls to the keyless listing APIs to about one per minute.
        """
        return await self._cache.get_or_load("listing", self.fetch, self._refresh, force=force)

    async def fetch(self) -> ListingResult:
        errors: list[str] = []
        for index, adapter in enumerate(self._adapters):
            try:
                entries = await adapter.listings(self._fetch_limit)
            except ProviderError as exc:
                errors.append(f"{adapter.name}: {exc.message}")
                continue
            now = utcnow()
            validation = validate_listing(
                entries, min_entries=self._min_entries, now=now, max_age_seconds=self._max_age
            )
            if not validation.ok:
                errors.append(f"{adapter.name}: rejected ({'; '.join(validation.issues)})")
                continue
            if index > 0:
                log.warning("listing fallback used", extra={"source": adapter.name, "errors": errors})
            return ListingResult(
                entries=validation.entries,
                source=adapter.name,
                fallback_used=index > 0,
                fetched_at=now,
                newest_update_age_seconds=validation.newest_update_age_seconds,
                issues=validation.issues,
                errors=errors,
                access=getattr(adapter, "last_access", None),
            )
        raise ListingUnavailable(errors)
