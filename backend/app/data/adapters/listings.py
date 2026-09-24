"""Market-cap listing and market-wide context adapters.

Listing chain (Top-20 ranking): CoinMarketCap (Pro with key, else keyless) -> CoinGecko
-> CoinPaprika. Global metrics: CoinMarketCap -> CoinGecko. Fear & Greed: CoinMarketCap ->
alternative.me. Altcoin Season Index and OHLCV reference candles: CoinMarketCap only.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from app.core.enums import Timeframe
from app.core.timeutil import from_seconds, parse_iso, to_ms, utcnow
from app.data.adapters._parse import opt_float, opt_int, to_float
from app.data.http import ProviderBadResponse, ProviderError
from app.data.normalization.schemas import AltcoinSeason, Candle, FearGreed, GlobalMetrics, ListingEntry
from app.data.providers.alternative_me import PROVIDER as ALT_PROVIDER
from app.data.providers.alternative_me import AlternativeMeClient
from app.data.providers.coingecko import PROVIDER as CG_PROVIDER
from app.data.providers.coingecko import CoinGeckoClient
from app.data.providers.coinmarketcap import PROVIDER as CMC_PROVIDER
from app.data.providers.coinmarketcap import CoinMarketCapClient
from app.data.providers.coinpaprika import PROVIDER as CP_PROVIDER
from app.data.providers.coinpaprika import CoinPaprikaClient

# --------------------------------------------------------------------------- parsers

_CMC_USD_ID = 2781


def _cmc_usd_quote(quote: Any) -> dict[str, Any] | None:
    """USD quote from either the v1 shape ({"USD": {...}}) or the v3 shape ([{"symbol": "USD", ...}])."""
    if isinstance(quote, dict):
        usd = quote.get("USD")
        return usd if isinstance(usd, dict) else None
    if isinstance(quote, list):
        for q in quote:
            if isinstance(q, dict) and (
                q.get("symbol") == "USD" or q.get("name") == "USD" or q.get("id") == _CMC_USD_ID
            ):
                return q
    return None


def parse_cmc_listings(data: Any) -> list[ListingEntry]:
    if not isinstance(data, list):
        raise ProviderBadResponse(CMC_PROVIDER, "listings data is not a list")
    fetched = utcnow()
    entries: list[ListingEntry] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        usd = _cmc_usd_quote(item.get("quote") or item.get("quotes"))
        if not isinstance(usd, dict):
            continue
        price = opt_float(usd.get("price"))
        market_cap = opt_float(usd.get("market_cap", usd.get("marketCap")))
        if price is None or market_cap is None:
            continue
        tags_raw = item.get("tags") or []
        tags = tuple(
            (t.get("slug") if isinstance(t, dict) else str(t)).lower()
            for t in tags_raw
            if isinstance(t, dict | str)
        )
        entries.append(
            ListingEntry(
                source=CMC_PROVIDER,
                source_id=str(item.get("id")),
                symbol=str(item.get("symbol", "")).upper(),
                name=str(item.get("name", "")),
                slug=item.get("slug"),
                rank=opt_int(item.get("cmc_rank", item.get("cmcRank"))),
                price_usd=price,
                market_cap_usd=market_cap,
                volume_24h_usd=opt_float(usd.get("volume_24h", usd.get("volume24h"))),
                pct_change_1h=opt_float(usd.get("percent_change_1h", usd.get("percentChange1h"))),
                pct_change_24h=opt_float(usd.get("percent_change_24h", usd.get("percentChange24h"))),
                pct_change_7d=opt_float(usd.get("percent_change_7d", usd.get("percentChange7d"))),
                tags=tags,
                last_updated=parse_iso(usd.get("last_updated") or item.get("last_updated")),
                fetched_at=fetched,
            )
        )
    return entries


def parse_coingecko_markets(data: Any, stablecoin_ids: set[str] | None = None) -> list[ListingEntry]:
    if not isinstance(data, list):
        raise ProviderBadResponse(CG_PROVIDER, "coins/markets payload is not a list")
    fetched = utcnow()
    stable = stablecoin_ids or set()
    entries: list[ListingEntry] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        price = opt_float(item.get("current_price"))
        market_cap = opt_float(item.get("market_cap"))
        if price is None or not market_cap:
            continue
        coin_id = str(item.get("id"))
        entries.append(
            ListingEntry(
                source=CG_PROVIDER,
                source_id=coin_id,
                symbol=str(item.get("symbol", "")).upper(),
                name=str(item.get("name", "")),
                slug=coin_id,
                rank=opt_int(item.get("market_cap_rank")),
                price_usd=price,
                market_cap_usd=market_cap,
                volume_24h_usd=opt_float(item.get("total_volume")),
                pct_change_1h=opt_float(item.get("price_change_percentage_1h_in_currency")),
                pct_change_24h=opt_float(
                    item.get("price_change_percentage_24h_in_currency", item.get("price_change_percentage_24h"))
                ),
                pct_change_7d=opt_float(item.get("price_change_percentage_7d_in_currency")),
                tags=("stablecoin",) if coin_id in stable else (),
                last_updated=parse_iso(item.get("last_updated")),
                fetched_at=fetched,
            )
        )
    return entries


def parse_coinpaprika_tickers(data: Any, limit: int) -> list[ListingEntry]:
    if not isinstance(data, list):
        raise ProviderBadResponse(CP_PROVIDER, "tickers payload is not a list")
    fetched = utcnow()
    ranked = [item for item in data if isinstance(item, dict) and (opt_int(item.get("rank")) or 0) > 0]
    ranked.sort(key=lambda item: int(item["rank"]))
    entries: list[ListingEntry] = []
    for item in ranked[:limit]:
        usd = (item.get("quotes") or {}).get("USD") or {}
        price = opt_float(usd.get("price"))
        market_cap = opt_float(usd.get("market_cap"))
        if price is None or not market_cap:
            continue
        entries.append(
            ListingEntry(
                source=CP_PROVIDER,
                source_id=str(item.get("id")),
                symbol=str(item.get("symbol", "")).upper(),
                name=str(item.get("name", "")),
                slug=item.get("id"),
                rank=opt_int(item.get("rank")),
                price_usd=price,
                market_cap_usd=market_cap,
                volume_24h_usd=opt_float(usd.get("volume_24h")),
                pct_change_1h=opt_float(usd.get("percent_change_1h")),
                pct_change_24h=opt_float(usd.get("percent_change_24h")),
                pct_change_7d=opt_float(usd.get("percent_change_7d")),
                tags=(),
                last_updated=parse_iso(item.get("last_updated")),
                fetched_at=fetched,
            )
        )
    return entries


def parse_cmc_global(data: Any) -> GlobalMetrics:
    if not isinstance(data, dict):
        raise ProviderBadResponse(CMC_PROVIDER, "global metrics payload is not an object")
    usd = ((data.get("quote") or {}).get("USD")) or {}
    return GlobalMetrics(
        source=CMC_PROVIDER,
        total_market_cap_usd=to_float(usd.get("total_market_cap"), CMC_PROVIDER, "total_market_cap"),
        total_volume_24h_usd=opt_float(usd.get("total_volume_24h")),
        btc_dominance_pct=opt_float(data.get("btc_dominance")),
        eth_dominance_pct=opt_float(data.get("eth_dominance")),
        market_cap_change_24h_pct=opt_float(usd.get("total_market_cap_yesterday_percentage_change")),
        updated_at=parse_iso(usd.get("last_updated") or data.get("last_updated")),
        fetched_at=utcnow(),
    )


def parse_coingecko_global(payload: Any) -> GlobalMetrics:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ProviderBadResponse(CG_PROVIDER, "global payload missing 'data'")
    caps = data.get("total_market_cap") or {}
    volumes = data.get("total_volume") or {}
    dominance = data.get("market_cap_percentage") or {}
    updated = data.get("updated_at")
    return GlobalMetrics(
        source=CG_PROVIDER,
        total_market_cap_usd=to_float(caps.get("usd"), CG_PROVIDER, "total_market_cap.usd"),
        total_volume_24h_usd=opt_float(volumes.get("usd")),
        btc_dominance_pct=opt_float(dominance.get("btc")),
        eth_dominance_pct=opt_float(dominance.get("eth")),
        market_cap_change_24h_pct=opt_float(data.get("market_cap_change_percentage_24h_usd")),
        updated_at=from_seconds(updated) if updated else None,
        fetched_at=utcnow(),
    )


def parse_cmc_fear_greed(data: Any) -> FearGreed:
    if not isinstance(data, dict):
        raise ProviderBadResponse(CMC_PROVIDER, "fear & greed payload is not an object")
    value = opt_int(data.get("value"))
    if value is None or not 0 <= value <= 100:
        raise ProviderBadResponse(CMC_PROVIDER, f"fear & greed value out of range: {data.get('value')!r}")
    return FearGreed(
        source=CMC_PROVIDER,
        value=value,
        classification=str(data.get("value_classification") or ""),
        updated_at=parse_iso(data.get("update_time")),
        fetched_at=utcnow(),
    )


def parse_alternative_me(payload: Any) -> FearGreed:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ProviderBadResponse(ALT_PROVIDER, "fear & greed payload missing data")
    row = rows[0]
    value = opt_int(row.get("value"))
    if value is None or not 0 <= value <= 100:
        raise ProviderBadResponse(ALT_PROVIDER, f"fear & greed value out of range: {row.get('value')!r}")
    ts = row.get("timestamp")
    return FearGreed(
        source=ALT_PROVIDER,
        value=value,
        classification=str(row.get("value_classification") or ""),
        updated_at=from_seconds(ts) if ts else None,
        fetched_at=utcnow(),
    )


def parse_cmc_altcoin_season(data: Any) -> AltcoinSeason:
    if not isinstance(data, dict):
        raise ProviderBadResponse(CMC_PROVIDER, "altcoin season payload is not an object")
    value = opt_int(data.get("altcoin_index"))
    if value is None or not 0 <= value <= 100:
        raise ProviderBadResponse(CMC_PROVIDER, f"altcoin season index out of range: {data.get('altcoin_index')!r}")
    return AltcoinSeason(
        source=CMC_PROVIDER,
        value=value,
        snapshot_time=parse_iso(data.get("snapshot_time")),
        yearly_high=opt_int(data.get("yearly_high")),
        yearly_low=opt_int(data.get("yearly_low")),
        fetched_at=utcnow(),
    )


def parse_cmc_ohlcv(data: Any, cmc_id: str, timeframe: Timeframe, now: datetime) -> list[Candle]:
    """CoinMarketCap aggregated OHLCV (USD) -> closed candles aligned to UTC boundaries."""
    if isinstance(data, dict) and "quotes" not in data and data:
        # multi-id responses are keyed by id; each value may be an object or a list
        data = data.get(str(cmc_id), next(iter(data.values())))
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or not isinstance(data.get("quotes"), list):
        return []
    symbol = f"CMC:{data.get('symbol') or cmc_id}"
    step = timedelta(seconds=timeframe.seconds)
    candles: list[Candle] = []
    for row in data["quotes"]:
        if not isinstance(row, dict):
            continue
        open_time = parse_iso(row.get("time_open"))
        usd = _cmc_usd_quote(row.get("quote"))
        if open_time is None or usd is None:
            continue
        if to_ms(open_time) % timeframe.ms:
            continue  # not aligned to the requested period; never guess
        close_time = open_time + step
        if close_time > now:
            continue  # the newest period is still forming
        candles.append(
            Candle(
                source=CMC_PROVIDER,
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                close_time=close_time,
                open=to_float(usd.get("open"), CMC_PROVIDER, "open"),
                high=to_float(usd.get("high"), CMC_PROVIDER, "high"),
                low=to_float(usd.get("low"), CMC_PROVIDER, "low"),
                close=to_float(usd.get("close"), CMC_PROVIDER, "close"),
                volume=opt_float(usd.get("volume")) or 0.0,
                quote_volume=opt_float(usd.get("volume")),
                is_closed=True,
            )
        )
    candles.sort(key=lambda c: c.open_time)
    return candles


# --------------------------------------------------------------------------- adapters


class CoinMarketCapAdapter:
    name = CMC_PROVIDER
    supported_timeframes = (Timeframe.H1, Timeframe.D1)

    def __init__(self, client: CoinMarketCapClient) -> None:
        self._client = client

    @property
    def last_access(self) -> str | None:
        """'pro' or 'keyless': which CoinMarketCap API served the latest call."""
        return self._client.last_access

    async def listings(self, limit: int) -> list[ListingEntry]:
        return parse_cmc_listings(await self._client.listings_latest(limit))

    async def global_metrics(self) -> GlobalMetrics:
        return parse_cmc_global(await self._client.global_metrics())

    async def fear_greed(self) -> FearGreed:
        return parse_cmc_fear_greed(await self._client.fear_and_greed_latest())

    async def altcoin_season(self) -> AltcoinSeason:
        return parse_cmc_altcoin_season(await self._client.altcoin_season_latest())

    async def reference_candles(self, reference_id: str, timeframe: Timeframe, count: int) -> list[Candle]:
        if timeframe not in self.supported_timeframes:
            raise ValueError(f"CoinMarketCap OHLCV reference supports only {self.supported_timeframes}")
        period = "hourly" if timeframe == Timeframe.H1 else "daily"
        data = await self._client.ohlcv_historical(reference_id, period, count)
        return parse_cmc_ohlcv(data, reference_id, timeframe, utcnow())


class CoinGeckoAdapter:
    name = CG_PROVIDER

    def __init__(
        self,
        client: CoinGeckoClient,
        *,
        stablecoin_ttl_seconds: float = 86400.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._stable_ttl = stablecoin_ttl_seconds
        self._clock = clock
        self._stable_ids: set[str] | None = None
        self._stable_loaded_at = 0.0

    async def _stablecoin_ids(self) -> set[str]:
        if self._stable_ids is None or self._clock() - self._stable_loaded_at > self._stable_ttl:
            try:
                rows = await self._client.coins_markets(per_page=250, category="stablecoins")
            except ProviderError:
                # Known-symbol and peg heuristics in the classifier still apply.
                return self._stable_ids or set()
            self._stable_ids = {str(r.get("id")) for r in rows if isinstance(r, dict)}
            self._stable_loaded_at = self._clock()
        return self._stable_ids

    async def listings(self, limit: int) -> list[ListingEntry]:
        rows = await self._client.coins_markets(per_page=min(limit, 250))
        return parse_coingecko_markets(rows, await self._stablecoin_ids())

    async def global_metrics(self) -> GlobalMetrics:
        return parse_coingecko_global(await self._client.global_data())


class CoinPaprikaAdapter:
    name = CP_PROVIDER

    def __init__(self, client: CoinPaprikaClient) -> None:
        self._client = client

    async def listings(self, limit: int) -> list[ListingEntry]:
        return parse_coinpaprika_tickers(await self._client.tickers(), limit)


class AlternativeMeAdapter:
    name = ALT_PROVIDER

    def __init__(self, client: AlternativeMeClient) -> None:
        self._client = client

    async def fear_greed(self) -> FearGreed:
        return parse_alternative_me(await self._client.fear_and_greed())
