"""Dynamic Top-N universe: CoinMarketCap Top 20 by market cap, stablecoins excluded.

Assets are ranked by market cap from the listing chain, stablecoins (and by default
wrapped/staked/pegged tokens) are removed, and each remaining asset is mapped to a
spot market (Binance first, Kraken fallback). An asset with no supported spot market
stays in the universe and is shown as unsupported instead of being silently dropped.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.core.timeutil import utcnow
from app.data.normalization.schemas import ListingEntry
from app.data.normalization.stablecoins import classify_listing
from app.data.validation.prices import USD_STABLE_QUOTES
from app.services.cache import AsyncTTLCache
from app.services.listing import ListingService, ListingUnavailable
from app.services.spot_router import MarketRef, SpotMarketRouter

log = logging.getLogger(__name__)


@dataclass
class UniverseAsset:
    universe_rank: int
    symbol: str
    name: str
    slug: str | None
    listing: ListingEntry
    markets: list[MarketRef]
    unsupported_reason: str | None = None
    watchlist: bool = False  # added manually by the user, not by market-cap rank

    @property
    def supported(self) -> bool:
        return bool(self.markets)

    @property
    def primary_market(self) -> MarketRef | None:
        return self.markets[0] if self.markets else None


@dataclass
class ExcludedAsset:
    symbol: str
    name: str
    market_cap_usd: float
    reason: str


@dataclass
class Universe:
    assets: list[UniverseAsset]
    excluded: list[ExcludedAsset]
    listing_source: str
    fallback_used: bool
    built_at: datetime
    listing_access: str | None
    listing_fetched_at: datetime
    listing_newest_update_age_seconds: float | None
    quote_usd_prices: dict[str, float]
    errors: list[str] = field(default_factory=list)
    stale: bool = False

    def find(self, symbol: str) -> UniverseAsset | None:
        wanted = symbol.upper()
        return next((a for a in self.assets if a.symbol == wanted), None)


class UniverseService:
    def __init__(
        self,
        listing: ListingService,
        router: SpotMarketRouter,
        *,
        size: int,
        refresh_seconds: float,
        exclude_wrapped: bool,
        symbol_overrides: dict[str, str] | None = None,
        extra_symbols: Callable[[], list[str]] | None = None,
        quotes: Callable[[list[str]], Awaitable[list[ListingEntry]]] | None = None,
    ) -> None:
        """`extra_symbols` returns the user's watchlist; `quotes` prices watchlist coins that
        are not in the fetched listing (CoinMarketCap quotes)."""
        self._extra = extra_symbols
        self._quotes = quotes
        self._listing = listing
        self._router = router
        self._size = size
        self._refresh = refresh_seconds
        self._exclude_wrapped = exclude_wrapped
        self._overrides = {k.upper(): v.upper() for k, v in (symbol_overrides or {}).items()}
        self._cache = AsyncTTLCache()
        self._last_good: Universe | None = None

    def invalidate(self) -> None:
        self._cache.invalidate()

    @property
    def last(self) -> Universe | None:
        """The latest universe built, without fetching anything."""
        return self._last_good

    async def _watch_entries(self, listing_entries: list[ListingEntry], taken: set[str]) -> tuple[list[ListingEntry], list[str]]:
        wanted = [s for s in (self._extra() if self._extra else []) if s not in taken]
        if not wanted:
            return [], []
        by_symbol: dict[str, ListingEntry] = {}
        for entry in listing_entries:
            if entry.symbol not in by_symbol:  # listing is rank-ordered: first is the largest coin
                by_symbol[entry.symbol] = entry
        errors: list[str] = []
        missing = [s for s in wanted if s not in by_symbol]
        if missing and self._quotes is not None:
            try:
                for entry in await self._quotes(missing):
                    by_symbol.setdefault(entry.symbol, entry)
            except Exception as exc:  # quotes are best effort; the coin is still shown
                errors.append(f"watchlist quotes: {exc}")
        found = [by_symbol[s] for s in wanted if s in by_symbol]
        errors.extend(f"watchlist: no price reference found for {s}" for s in wanted if s not in by_symbol)
        return found, errors

    async def get(self, *, force: bool = False) -> Universe:
        try:
            universe = await self._cache.get_or_load("universe", self._build, self._refresh, force=force)
        except ListingUnavailable as exc:
            if self._last_good is None:
                raise
            log.error("universe refresh failed; serving last known universe", extra={"errors": exc.errors})
            return dataclasses.replace(self._last_good, stale=True, errors=list(exc.errors))
        self._last_good = universe
        return universe

    async def _build(self) -> Universe:
        listing = await self._listing.latest()
        selected: list[ListingEntry] = []
        excluded: list[ExcludedAsset] = []
        for entry in listing.entries:
            if len(selected) >= self._size:
                break
            cls = classify_listing(entry)
            if cls.is_stablecoin:
                excluded.append(ExcludedAsset(entry.symbol, entry.name, entry.market_cap_usd, f"stablecoin: {cls.reason}"))
            elif cls.is_wrapped_or_derivative and self._exclude_wrapped:
                excluded.append(ExcludedAsset(entry.symbol, entry.name, entry.market_cap_usd, f"derivative: {cls.reason}"))
            else:
                selected.append(entry)

        watch_entries, watch_errors = await self._watch_entries(listing.entries, {e.symbol for e in selected})
        watch_symbols = {e.symbol for e in watch_entries}
        selected = selected + watch_entries
        candidates, resolve_errors = await self._router.resolve([e.symbol for e in selected], self._overrides)
        assets: list[UniverseAsset] = []
        for rank, entry in enumerate(selected, start=1):
            markets = candidates.get(entry.symbol, [])
            reason = None
            if not markets:
                reason = (
                    "spot pair lists unavailable" if resolve_errors and len(resolve_errors) == len(self._router.adapter_names)
                    else "no USDT/USD spot pair on supported exchanges"
                )
            assets.append(
                UniverseAsset(
                    universe_rank=rank,
                    symbol=entry.symbol,
                    name=entry.name,
                    slug=entry.slug,
                    listing=entry,
                    markets=markets,
                    unsupported_reason=reason,
                    watchlist=entry.symbol in watch_symbols,
                )
            )
        quote_prices = {e.symbol: e.price_usd for e in listing.entries if e.symbol in USD_STABLE_QUOTES}
        log.info(
            "universe built",
            extra={
                "source": listing.source,
                "assets": len(assets),
                "unsupported": sum(1 for a in assets if not a.supported),
                "excluded": len(excluded),
            },
        )
        return Universe(
            assets=assets,
            excluded=excluded,
            listing_source=listing.source,
            fallback_used=listing.fallback_used,
            built_at=utcnow(),
            listing_access=listing.access,
            listing_fetched_at=listing.fetched_at,
            listing_newest_update_age_seconds=listing.newest_update_age_seconds,
            quote_usd_prices=quote_prices,
            errors=listing.errors + resolve_errors + watch_errors,
        )
