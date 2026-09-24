"""Token unlocks and airdrops (optional keys).

* Token unlocks: Mobula metadata (`MOBULA_API_KEY`, free plan) publishes each token's
  release schedule: unlock date, tokens released and who receives them (team, investors,
  ecosystem...). Large unlocks add supply and are a known selling-pressure risk, so an
  unlock of at least UNLOCK_NOTE_MIN_PCT of circulating supply within UNLOCK_NOTE_DAYS
  becomes a risk note on that coin's signal (never a label change).
* Airdrops: AlphaDrops Developer API (`ALPHADROPS_API_KEY`, a paid subscription) lists
  active, claimable and upcoming airdrops.

Both are fetched only on request (or, for unlocks, at most once per EVENTS_CACHE_SECONDS
before a scan) and cached. Without a key the endpoint says so instead of guessing.
Tokenomist has no free API and is not integrated.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings
from app.core.timeutil import parse_iso, utcnow
from app.data.health import ProviderHealthRegistry
from app.services.cache import AsyncTTLCache
from app.services.onchain import SourceError, fetch_json

log = logging.getLogger(__name__)

MAX_UNLOCK_COINS = 30
AIRDROP_STATUSES = ("active", "claimable", "upcoming")


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _when(value: Any) -> datetime | None:
    """Unix seconds, unix milliseconds or ISO 8601."""
    if isinstance(value, int | float) and value > 0:
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=UTC)
    if isinstance(value, str):
        if value.isdigit():
            return _when(int(value))
        return parse_iso(value)
    return None


@dataclass
class UnlockEvent:
    date: datetime
    days_until: float
    tokens: float
    value_usd: float | None
    pct_circulating: float | None
    allocations: dict[str, float] = field(default_factory=dict)


@dataclass
class CoinUnlocks:
    symbol: str
    name: str
    circulating_supply: float | None
    upcoming: list[UnlockEvent]
    window_tokens: float
    window_value_usd: float | None
    window_pct_circulating: float | None
    next_unlock: UnlockEvent | None


@dataclass
class UnlockDigest:
    fetched_at: datetime
    configured: bool
    window_days: int
    coins: list[CoinUnlocks]
    checked: list[str]
    errors: list[str]
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Airdrop:
    name: str
    status: str | None
    chains: list[str]
    url: str | None
    ends_at: datetime | None
    reward: str | None
    cost: str | None
    token: str | None


@dataclass
class AirdropDigest:
    fetched_at: datetime
    configured: bool
    airdrops: list[Airdrop]
    errors: list[str]
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_release_schedule(
    payload: Any, symbol: str, name: str, price: float | None, now: datetime, window_days: int
) -> CoinUnlocks:
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    data = data if isinstance(data, dict) else {}
    circulating = _float(data.get("circulating_supply"))
    price = price if price is not None else _float(data.get("price"))
    schedule = data.get("release_schedule") or data.get("releaseSchedule") or []
    events: list[UnlockEvent] = []
    for item in schedule if isinstance(schedule, list) else []:
        if not isinstance(item, dict):
            continue
        when = _when(item.get("unlock_date") or item.get("date"))
        tokens = _float(item.get("tokens_to_unlock") or item.get("amount"))
        if when is None or not tokens or when < now:
            continue
        details = item.get("allocation_details") if isinstance(item.get("allocation_details"), dict) else {}
        allocations = {str(k)[:48]: v for k, v in ((k, _float(v)) for k, v in details.items()) if v}
        top = dict(sorted(allocations.items(), key=lambda kv: kv[1], reverse=True)[:4])
        events.append(
            UnlockEvent(
                date=when,
                days_until=round((when - now).total_seconds() / 86400, 1),
                tokens=tokens,
                value_usd=tokens * price if price else None,
                pct_circulating=round(tokens / circulating * 100, 3) if circulating else None,
                allocations=top,
            )
        )
    events.sort(key=lambda e: e.date)
    window = [e for e in events if e.days_until <= window_days]
    tokens = sum(e.tokens for e in window)
    return CoinUnlocks(
        symbol=symbol,
        name=name,
        circulating_supply=circulating,
        upcoming=events[:12],
        window_tokens=tokens,
        window_value_usd=tokens * price if price and window else (0.0 if not window else None),
        window_pct_circulating=round(tokens / circulating * 100, 3) if circulating and window else (0.0 if not window else None),
        next_unlock=events[0] if events else None,
    )


def _list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "airdrops", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                inner = _list(value)
                if inner:
                    return inner
    return []


def _text(value: Any, limit: int = 80) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("value") or value.get("amount")
    if value is None or value == "":
        return None
    return str(value)[:limit]


def parse_airdrops(payload: Any, site: str = "https://alphadrops.net") -> list[Airdrop]:
    out = []
    for item in _list(payload):
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name") or item.get("title") or item.get("project"), 120)
        if not name:
            continue
        chains_raw = item.get("chains") or item.get("ecosystems") or item.get("ecosystem") or item.get("blockchain") or []
        chains_raw = chains_raw if isinstance(chains_raw, list) else [chains_raw]
        chains = [c for c in (_text(c, 32) for c in chains_raw) if c][:5]
        url = item.get("url") or item.get("link") or item.get("website")
        if not (isinstance(url, str) and url.startswith("https://")):
            slug = item.get("slug")
            url = f"{site}/airdrops/{slug}" if isinstance(slug, str) and slug.replace("-", "").isalnum() else None
        out.append(
            Airdrop(
                name=name,
                status=_text(item.get("status"), 24),
                chains=chains,
                url=url,
                ends_at=_when(item.get("end_date") or item.get("ends_at") or item.get("deadline") or item.get("claim_end")),
                reward=_text(item.get("estimated_value") or item.get("reward") or item.get("value")),
                cost=_text(item.get("cost") or item.get("funding")),
                token=_text(item.get("token") or item.get("token_symbol") or item.get("symbol"), 16),
            )
        )
    return out


class EventsService:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        health: ProviderHealthRegistry,
        coins: Callable[[], list[tuple[str, str, float | None]]],
    ) -> None:
        """`coins` returns (symbol, name, USD price) for the coins to check (the analysed ones)."""
        self._s = settings
        self._http = http
        self._health = health
        self._coins = coins
        self._cache = AsyncTTLCache()

    def cached_unlocks(self) -> UnlockDigest | None:
        entry = self._cache.peek("unlocks")
        return entry[0] if entry else None

    def notes_for(self, symbol: str) -> list[str]:
        """Risk notes for large unlocks soon (from the cache only; never fetches)."""
        digest = self.cached_unlocks()
        coin = next((c for c in digest.coins if c.symbol == symbol.upper()), None) if digest else None
        if coin is None:
            return []
        soon = [e for e in coin.upcoming if e.days_until <= self._s.unlock_note_days]
        pct = sum(e.pct_circulating or 0 for e in soon)
        if not soon or pct < self._s.unlock_note_min_pct:
            return []
        value = sum(e.value_usd or 0 for e in soon)
        first = soon[0]
        worth = f" (${value / 1e6:,.1f}M)" if value else ""
        return [f"token unlock in {first.days_until:.0f} days: {pct:.2f}% of circulating supply{worth} over the next "
                f"{self._s.unlock_note_days} days"]

    async def _guarded(self, key: str, loader: Callable[[], Any], ttl: float, force: bool) -> Any:
        entry = self._cache.peek(key)
        if force and entry is not None and entry[1] < self._s.min_refresh_seconds:
            force = False
        return await self._cache.get_or_load(key, loader, ttl, force=force)

    async def unlocks(self, *, force: bool = False) -> UnlockDigest:
        if not self._s.mobula_key:
            return UnlockDigest(utcnow(), False, self._s.unlock_window_days, [], [], [],
                                "Set MOBULA_API_KEY (free at mobula.io) to track token unlocks.")
        return await self._guarded("unlocks", self._load_unlocks, self._s.events_cache_seconds, force)

    async def airdrops(self, *, force: bool = False) -> AirdropDigest:
        if not self._s.alphadrops_key:
            return AirdropDigest(utcnow(), False, [], [],
                                 "Set ALPHADROPS_API_KEY (AlphaDrops Developer API) to list airdrops.")
        return await self._guarded("airdrops", self._load_airdrops, self._s.events_cache_seconds, force)

    async def _mobula(self, symbol: str, name: str) -> Any:
        url = f"{self._s.mobula_base_url.rstrip('/')}/metadata"
        headers = {"Authorization": self._s.mobula_key or ""}
        try:
            return await fetch_json(self._http, self._health, "mobula", "unlocks", url, {"asset": name}, headers=headers)
        except SourceError as exc:
            if exc.status_code in (400, 404):  # name not recognised: try the symbol
                return await fetch_json(self._http, self._health, "mobula", "unlocks", url, {"symbol": symbol}, headers=headers)
            raise

    async def _load_unlocks(self) -> UnlockDigest:
        now = utcnow()
        coins = self._coins()[:MAX_UNLOCK_COINS]
        semaphore = asyncio.Semaphore(3)

        async def one(symbol: str, name: str) -> Any:
            async with semaphore:
                return await self._mobula(symbol, name)

        results = await asyncio.gather(*(one(s, n) for s, n, _ in coins), return_exceptions=True)
        out: list[CoinUnlocks] = []
        errors: list[str] = []
        for (symbol, name, price), result in zip(coins, results, strict=True):
            if isinstance(result, BaseException):
                errors.append(f"{symbol}: {result}")
                continue
            unlocks = parse_release_schedule(result, symbol, name, price, now, self._s.unlock_window_days)
            if unlocks.upcoming:
                out.append(unlocks)
        out.sort(key=lambda c: (-(c.window_pct_circulating or 0), c.next_unlock.date if c.next_unlock else now))
        return UnlockDigest(now, True, self._s.unlock_window_days, out, [s for s, _, _ in coins], errors[:20])

    async def _load_airdrops(self) -> AirdropDigest:
        url = f"{self._s.alphadrops_base_url.rstrip('/')}/airdrops"
        headers = {"Authorization": f"Bearer {self._s.alphadrops_key}"}
        results = await asyncio.gather(
            *(fetch_json(self._http, self._health, "alphadrops", "airdrops", url, {"status": status, "limit": 25}, headers=headers)
              for status in AIRDROP_STATUSES),
            return_exceptions=True,
        )
        drops: dict[str, Airdrop] = {}
        errors: list[str] = []
        for status, result in zip(AIRDROP_STATUSES, results, strict=True):
            if isinstance(result, BaseException):
                errors.append(f"{status}: {result}")
                continue
            for drop in parse_airdrops(result):
                drop.status = drop.status or status
                drops.setdefault(drop.name.lower(), drop)
        order = {s: i for i, s in enumerate(AIRDROP_STATUSES)}
        items = sorted(drops.values(), key=lambda d: (order.get((d.status or "").lower(), 9), d.ends_at or datetime.max.replace(tzinfo=UTC)))
        return AirdropDigest(utcnow(), True, items[:60], sorted(set(errors)))
