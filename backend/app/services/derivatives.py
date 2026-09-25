"""Futures positioning per coin (Phase 10): funding, open interest, long/short ratios, whales,
taker volume and liquidations, from public exchange APIs with failover.

For each metric the first exchange that lists the coin and answers wins, in this order:
Binance futures, Bybit, OKX, Hyperliquid (funding and current open interest only). An
exchange that refuses the server's region (HTTP 403 / 451) is skipped for an hour instead of
being asked again for every coin. Liquidation orders come from OKX (the only one of these that
publishes them without a streaming connection). Everything is fetched only during a scan or
when a coin is opened, and cached (DERIVATIVES_CACHE_SECONDS). Nothing is ever guessed: a
metric no exchange answered is missing, and the evidence board says so.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from statistics import fmean
from typing import Any

import httpx

from app.config import Settings
from app.core.enums import ProviderStatus
from app.core.timeutil import utcnow
from app.data import derivatives as d
from app.data.health import ProviderHealthRegistry
from app.services.cache import AsyncTTLCache
from app.services.onchain import SourceError, fetch_json

log = logging.getLogger(__name__)

RESTRICTED_SKIP_SECONDS = 3600
HOT_FUNDING_PCT = 0.05
COLD_FUNDING_PCT = -0.03
PROVIDERS = {"binance": "binance_futures", "bybit": "bybit", "okx": "okx", "hyperliquid": "hyperliquid"}


@dataclass
class DerivativesSnapshot:
    symbol: str
    fetched_at: datetime
    listed: list[str] = field(default_factory=list)  # exchanges with a USDT perpetual for this coin
    sources: dict[str, str] = field(default_factory=dict)  # metric -> exchange
    funding_pct: float | None = None  # latest, percent per 8h
    funding_history: list[d.Point] = field(default_factory=list)
    open_interest_usd: float | None = None
    oi_history: list[d.Point] = field(default_factory=list)
    oi_unit: str = "coin"  # coin | usd (OKX publishes history in USD)
    long_share: list[d.Point] = field(default_factory=list)  # all accounts, fraction long (0..1)
    top_long_share: list[d.Point] = field(default_factory=list)  # top trader positions, fraction long
    taker_ratio: list[d.Point] = field(default_factory=list)  # futures taker buy / sell volume
    liquidations: list[d.Liquidation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return bool(self.sources)

    def as_dict(self) -> dict[str, Any]:
        def pts(points: list[d.Point]) -> list[list[Any]]:
            return [[p.time.isoformat(), p.value] for p in points]

        return {
            "symbol": self.symbol, "fetched_at": self.fetched_at, "listed": self.listed, "sources": self.sources,
            "funding_pct": self.funding_pct, "funding_history": pts(self.funding_history),
            "open_interest_usd": self.open_interest_usd, "oi_unit": self.oi_unit, "oi_history": pts(self.oi_history),
            "long_share": pts(self.long_share), "top_long_share": pts(self.top_long_share),
            "taker_ratio": pts(self.taker_ratio),
            "liquidations": [{"time": x.time.isoformat(), "side": x.side, "price": x.price, "usd": x.usd}
                             for x in self.liquidations[-200:]],
            "errors": self.errors,
        }


@dataclass
class MarketDerivatives:
    fetched_at: datetime
    quotes: dict[str, dict[str, d.PerpQuote]]  # venue -> spot base -> quote (mark per spot coin)
    okx_contracts: dict[str, float]  # OKX instId -> contract value in coins
    errors: list[str]

    def best(self, base: str) -> d.PerpQuote | None:
        for venue in ("binance", "bybit", "hyperliquid"):
            q = self.quotes.get(venue, {}).get(base.upper())
            if q is not None and q.funding_pct_8h is not None:
                return q
        return None

    def summary(self, bases: list[str] | None = None) -> dict[str, Any]:
        """Market-wide positioning over the given coins (all listed coins when None)."""
        rates: dict[str, float] = {}
        oi = 0.0
        for base in bases or sorted({b for q in self.quotes.values() for b in q}):
            q = self.best(base)
            if q is None or q.funding_pct_8h is None:
                continue
            rates[base] = q.funding_pct_8h
            oi += q.open_interest_usd or 0.0
        if not rates:
            return {"coins": 0, "errors": self.errors}
        return {
            "coins": len(rates), "avg_funding_pct": fmean(rates.values()),
            "hot": sorted(b for b, r in rates.items() if r >= HOT_FUNDING_PCT),
            "cold": sorted(b for b, r in rates.items() if r <= COLD_FUNDING_PCT),
            "open_interest_usd": oi or None, "errors": self.errors,
            "venues": sorted(v for v, q in self.quotes.items() if q),
        }


class DerivativesService:
    def __init__(self, settings: Settings, http: httpx.AsyncClient, health: ProviderHealthRegistry) -> None:
        self._s = settings
        self._http = http
        self._health = health
        self._cache = AsyncTTLCache(prune_expired=True)
        self._restricted: dict[str, float] = {}
        self._limits = {v: asyncio.Semaphore(max(1, settings.derivatives_concurrency)) for v in PROVIDERS}

    @property
    def enabled(self) -> bool:
        return self._s.derivatives_enabled

    # ------------------------------------------------------------------ requests

    def _skipped(self, venue: str) -> bool:
        until = self._restricted.get(venue)
        return until is not None and time.monotonic() < until

    async def _get(self, venue: str, url: str, params: dict[str, Any] | None = None) -> Any:
        if self._skipped(venue):
            raise SourceError(f"{PROVIDERS[venue]}: skipped (refused this server's region recently)")
        async with self._limits[venue]:
            try:
                return await fetch_json(self._http, self._health, PROVIDERS[venue], "derivatives", url, params, timeout=10.0)
            except SourceError as exc:
                if exc.status_code in (403, 451):
                    self._restricted[venue] = time.monotonic() + RESTRICTED_SKIP_SECONDS
                raise

    async def _post(self, venue: str, url: str, body: dict[str, Any]) -> Any:
        if self._skipped(venue):
            raise SourceError(f"{PROVIDERS[venue]}: skipped (refused this server's region recently)")
        provider = PROVIDERS[venue]
        self._health.register(provider, "derivatives")
        started = time.monotonic()
        async with self._limits[venue]:
            try:
                response = await self._http.post(url, json=body, timeout=10.0)
            except httpx.HTTPError as exc:
                self._health.record_failure(provider, f"transport error: {type(exc).__name__}")
                raise SourceError(f"{provider}: unreachable ({type(exc).__name__})") from None
        if response.status_code >= 400:
            status = ProviderStatus.RESTRICTED if response.status_code in (401, 403, 451) else None
            self._health.record_failure(provider, f"HTTP {response.status_code}", status=status)
            if response.status_code in (403, 451):
                self._restricted[venue] = time.monotonic() + RESTRICTED_SKIP_SECONDS
            raise SourceError(f"{provider}: HTTP {response.status_code}", response.status_code)
        try:
            data = response.json()
        except ValueError:
            self._health.record_failure(provider, "invalid JSON")
            raise SourceError(f"{provider}: invalid JSON") from None
        self._health.record_success(provider, (time.monotonic() - started) * 1000)
        return data

    # ------------------------------------------------------------------ market-wide listings

    async def market(self, *, force: bool = False) -> MarketDerivatives:
        return await self._cache.get_or_load("market", self._load_market, 120, force=force)

    def cached_market(self) -> MarketDerivatives | None:
        entry = self._cache.peek("market")
        return entry[0] if entry else None

    async def _load_market(self) -> MarketDerivatives:
        s = self._s
        jobs = {
            "binance": self._get("binance", f"{s.binance_futures_url.rstrip('/')}/fapi/v1/premiumIndex"),
            "bybit": self._get("bybit", f"{s.bybit_base_url.rstrip('/')}/v5/market/tickers", {"category": "linear"}),
            "hyperliquid": self._post("hyperliquid", s.hyperliquid_info_url, {"type": "metaAndAssetCtxs"}),
            "okx": self._okx_contracts(),
        }
        results = dict(zip(jobs, await asyncio.gather(*jobs.values(), return_exceptions=True), strict=True))
        errors: list[str] = []
        quotes: dict[str, dict[str, d.PerpQuote]] = {}
        parsers = {"binance": (d.binance_premium, d.binance_candidates),
                   "bybit": (d.bybit_tickers, d.bybit_candidates),
                   "hyperliquid": (d.hyperliquid_contexts, d.hyperliquid_candidates)}
        for venue, (parse, candidates) in parsers.items():
            raw = results[venue]
            if isinstance(raw, BaseException):
                errors.append(str(raw) if isinstance(raw, SourceError) else f"{venue}: {type(raw).__name__}")
                continue
            try:
                listing = parse(raw)
            except d.ParseError as exc:
                self._health.record_failure(PROVIDERS[venue], f"unexpected payload: {exc}")
                errors.append(f"{PROVIDERS[venue]}: unexpected payload")
                continue
            quotes[venue] = _by_base(listing, candidates)
        okx = results["okx"]
        contracts: dict[str, float] = {}
        if isinstance(okx, BaseException):
            errors.append(str(okx) if isinstance(okx, SourceError) else f"okx: {type(okx).__name__}")
        else:
            contracts = okx
        return MarketDerivatives(utcnow(), quotes, contracts, errors)

    async def _okx_contracts(self) -> dict[str, float]:
        async def load() -> dict[str, float]:
            raw = await self._get("okx", f"{self._s.okx_base_url.rstrip('/')}/api/v5/public/instruments", {"instType": "SWAP"})
            try:
                return d.okx_instruments(raw)
            except d.ParseError as exc:
                self._health.record_failure("okx", f"unexpected payload: {exc}")
                raise SourceError("okx: unexpected payload") from None

        return await self._cache.get_or_load("okx_contracts", load, 6 * 3600)

    # ------------------------------------------------------------------ per coin

    def cached(self, symbol: str) -> DerivativesSnapshot | None:
        entry = self._cache.peek(("coin", symbol.upper()))
        return entry[0] if entry else None

    async def snapshot(self, symbol: str, *, force: bool = False) -> DerivativesSnapshot:
        base = symbol.upper()
        return await self._cache.get_or_load(("coin", base), lambda: self._load_coin(base),
                                             self._s.derivatives_cache_seconds, force=force)

    async def _load_coin(self, base: str) -> DerivativesSnapshot:
        snap = DerivativesSnapshot(symbol=base, fetched_at=utcnow())
        if not self.enabled:
            snap.errors.append("futures data switched off (DERIVATIVES_ENABLED=false)")
            return snap
        market = await self.market()
        snap.errors.extend(market.errors)
        binance = market.quotes.get("binance", {}).get(base)
        bybit = market.quotes.get("bybit", {}).get(base)
        hyper = market.quotes.get("hyperliquid", {}).get(base)
        okx_ct = market.okx_contracts.get(d.okx_inst(base))
        snap.listed = [v for v, q in (("binance", binance), ("bybit", bybit), ("okx", okx_ct), ("hyperliquid", hyper)) if q]
        if not snap.listed:
            snap.errors.append(f"{base}: no USDT perpetual found on the reachable exchanges")
            return snap
        best = next((q for q in (binance, bybit, hyper) if q is not None and q.funding_pct_8h is not None), None)
        if best is not None:
            snap.funding_pct, snap.sources["funding"] = best.funding_pct_8h, best.venue
        oi_quote = next((q for q in (binance, bybit, hyper) if q is not None and q.open_interest_usd), None)
        if oi_quote is not None:
            snap.open_interest_usd = oi_quote.open_interest_usd
        hours = max(24, min(500, self._s.derivatives_history_hours))
        loaders: dict[str, list[tuple[str, Callable[[], Awaitable[Any]]]]] = {
            "oi_history": [], "long_share": [], "top_long_share": [], "taker_ratio": [], "funding_history": [],
            "liquidations": [],
        }
        if binance is not None:
            for metric, loader in self._binance_loaders(binance, hours).items():
                loaders[metric].append(("binance", loader))
        if bybit is not None:
            for metric, loader in self._bybit_loaders(bybit, hours).items():
                loaders[metric].append(("bybit", loader))
        if okx_ct:
            for metric, loader in self._okx_loaders(base, okx_ct).items():
                loaders[metric].append(("okx", loader))
        await asyncio.gather(*(self._metric(snap, metric, chain) for metric, chain in loaders.items() if chain))
        if "funding" not in snap.sources and okx_ct:
            try:
                rate = d.okx_funding(await self._get(
                    "okx", f"{self._s.okx_base_url.rstrip('/')}/api/v5/public/funding-rate", {"instId": d.okx_inst(base)}))
            except (SourceError, d.ParseError) as exc:
                snap.errors.append(str(exc))
            else:
                if rate is not None:
                    snap.funding_pct, snap.sources["funding"] = rate, "okx"
        if snap.open_interest_usd is None and snap.oi_history:
            last = snap.oi_history[-1].value
            mark = best.mark if best is not None else None
            snap.open_interest_usd = last if snap.oi_unit == "usd" else (last * mark if mark else None)
        return snap

    async def _metric(self, snap: DerivativesSnapshot, metric: str,
                      chain: list[tuple[str, Callable[[], Awaitable[Any]]]]) -> None:
        """Fill one metric from the first exchange in `chain` that answers with data."""
        for venue, loader in chain:
            try:
                points = await loader()
            except SourceError as exc:
                snap.errors.append(str(exc))
                continue
            except d.ParseError as exc:
                self._health.record_failure(PROVIDERS[venue], f"unexpected payload: {exc}")
                snap.errors.append(f"{PROVIDERS[venue]}: unexpected {metric} payload")
                continue
            if points:
                snap.sources[metric] = venue
                setattr(snap, metric, points)
                if metric == "oi_history":
                    snap.oi_unit = "usd" if venue == "okx" else "coin"
                return

    def _binance_loaders(self, q: d.PerpQuote, hours: int) -> dict[str, Callable[[], Awaitable[Any]]]:
        root = self._s.binance_futures_url.rstrip("/")
        sym, mult = q.symbol, q.multiplier

        def series(path: str, key: str, limit: int, scale: float = 1.0) -> Callable[[], Awaitable[list[d.Point]]]:
            async def load() -> list[d.Point]:
                raw = await self._get("binance", f"{root}{path}", {"symbol": sym, "period": "1h", "limit": limit})
                return d.binance_series(raw, key, scale=scale)
            return load

        async def funding() -> list[d.Point]:
            return d.binance_funding(await self._get("binance", f"{root}/fapi/v1/fundingRate", {"symbol": sym, "limit": 30}))

        return {
            "oi_history": series("/futures/data/openInterestHist", "sumOpenInterest", hours, mult),
            "long_share": series("/futures/data/globalLongShortAccountRatio", "longAccount", hours),
            "top_long_share": series("/futures/data/topLongShortPositionRatio", "longAccount", hours),
            "taker_ratio": series("/futures/data/takerlongshortRatio", "buySellRatio", 48),
            "funding_history": funding,
        }

    def _bybit_loaders(self, q: d.PerpQuote, hours: int) -> dict[str, Callable[[], Awaitable[Any]]]:
        root = f"{self._s.bybit_base_url.rstrip('/')}/v5/market"
        sym, mult = q.symbol, q.multiplier

        async def oi() -> list[d.Point]:
            raw = await self._get("bybit", f"{root}/open-interest",
                                  {"category": "linear", "symbol": sym, "intervalTime": "1h", "limit": min(200, hours)})
            return d.bybit_series(raw, "openInterest", scale=mult)

        async def ratio() -> list[d.Point]:
            raw = await self._get("bybit", f"{root}/account-ratio",
                                  {"category": "linear", "symbol": sym, "period": "1h", "limit": min(500, hours)})
            return d.bybit_series(raw, "buyRatio")

        async def funding() -> list[d.Point]:
            raw = await self._get("bybit", f"{root}/funding/history", {"category": "linear", "symbol": sym, "limit": 30})
            return d.bybit_series(raw, "fundingRate", "fundingRateTimestamp", scale=100.0)

        return {"oi_history": oi, "long_share": ratio, "funding_history": funding}

    def _okx_loaders(self, base: str, ct_val: float) -> dict[str, Callable[[], Awaitable[Any]]]:
        root = f"{self._s.okx_base_url.rstrip('/')}/api/v5"

        async def liquidations() -> list[d.Liquidation]:
            raw = await self._get("okx", f"{root}/public/liquidation-orders",
                                  {"instType": "SWAP", "uly": f"{base}-USDT", "state": "filled", "limit": 100})
            return d.okx_liquidations(raw, ct_val)

        async def oi_usd() -> list[d.Point]:
            raw = await self._get("okx", f"{root}/rubik/stat/contracts/open-interest-volume", {"ccy": base, "period": "1H"})
            return d.okx_rows(raw, 1)

        async def ratio() -> list[d.Point]:
            raw = await self._get("okx", f"{root}/rubik/stat/contracts/long-short-account-ratio", {"ccy": base, "period": "1H"})
            return [d.Point(p.time, p.value / (1.0 + p.value)) for p in d.okx_rows(raw, 1) if p.value >= 0]

        async def taker() -> list[d.Point]:
            raw = await self._get("okx", f"{root}/rubik/stat/taker-volume", {"ccy": base, "instType": "CONTRACTS", "period": "1H"})
            return d.okx_taker_ratio(raw)

        return {"liquidations": liquidations, "oi_history": oi_usd, "long_share": ratio, "taker_ratio": taker}


def _by_base(listing: dict[str, d.PerpQuote], candidates: Any) -> dict[str, d.PerpQuote]:
    """Map an exchange listing to spot base assets, preferring the 1:1 contract."""
    bases: dict[str, d.PerpQuote] = {}
    for symbol in listing:
        base = _base_of(symbol)
        if base is None:
            continue
        found = d.resolve(listing, candidates(base))
        if found is None:
            continue
        sym, mult = found
        q = listing[sym]
        bases[base] = replace(q, symbol=sym, multiplier=mult, mark=q.mark / mult if q.mark else q.mark)
    return bases


def _base_of(symbol: str) -> str | None:
    s = symbol.upper()
    if s.endswith("USDT"):
        s = s[:-4]
    for prefix in ("1000000", "10000", "1000", "1M"):
        if s.startswith(prefix) and len(s) > len(prefix):
            return s[len(prefix):]
    if s.endswith("1000") and len(s) > 4:
        return s[:-4]
    if s.startswith("K") and symbol.startswith("k"):
        return s[1:]
    return s or None
