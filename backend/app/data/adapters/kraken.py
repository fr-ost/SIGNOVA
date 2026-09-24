"""Kraken adapter: raw Kraken payloads -> normalized schemas (fallback spot source)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from app.core.enums import Timeframe
from app.core.timeutil import from_seconds, to_ms, utcnow
from app.data.adapters._parse import opt_float, opt_int, to_float
from app.data.http import ProviderBadResponse
from app.data.normalization.schemas import Candle, OrderBook, Ticker
from app.data.providers.kraken import PROVIDER, KrakenRestClient

_BASE_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}


def _result_rows(result: dict[str, Any], what: str) -> Any:
    keys = [key for key in result if key != "last"]
    if len(keys) != 1:
        raise ProviderBadResponse(PROVIDER, f"{what}: expected one pair in result, got {len(keys)}")
    return result[keys[0]]


def parse_asset_pairs(result: dict[str, Any], quote_asset: str) -> tuple[dict[str, str], dict[str, str]]:
    """Return (base -> altname, altname -> result key)."""
    pairs: dict[str, str] = {}
    keys_by_alt: dict[str, str] = {}
    for key, info in result.items():
        if not isinstance(info, dict):
            continue
        wsname, altname = info.get("wsname"), info.get("altname")
        if not isinstance(wsname, str) or "/" not in wsname or not isinstance(altname, str):
            continue
        if info.get("status", "online") != "online":
            continue
        base, quote = wsname.split("/", 1)
        if quote != quote_asset:
            continue
        base = _BASE_ALIASES.get(base, base).upper()
        pairs.setdefault(base, altname)
        keys_by_alt[altname] = key
    return pairs, keys_by_alt


def parse_ohlc(result: dict[str, Any], symbol: str, timeframe: Timeframe, now: datetime) -> list[Candle]:
    rows = _result_rows(result, "OHLC")
    if not isinstance(rows, list):
        raise ProviderBadResponse(PROVIDER, "OHLC rows are not a list")
    now_ms = to_ms(now)
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 8:
            raise ProviderBadResponse(PROVIDER, "malformed OHLC row")
        open_time = from_seconds(row[0])
        close_ms = to_ms(open_time) + timeframe.ms
        vwap = opt_float(row[5])
        volume = to_float(row[6], PROVIDER, "volume")
        candles.append(
            Candle(
                source=PROVIDER,
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                close_time=from_seconds(close_ms / 1000),
                open=to_float(row[1], PROVIDER, "open"),
                high=to_float(row[2], PROVIDER, "high"),
                low=to_float(row[3], PROVIDER, "low"),
                close=to_float(row[4], PROVIDER, "close"),
                volume=volume,
                quote_volume=volume * vwap if vwap else None,
                trades=opt_int(row[7]),
                is_closed=close_ms <= now_ms,
            )
        )
    # Kraken documents the last row as the current, not-yet-committed frame.
    if candles and candles[-1].is_closed:
        candles[-1] = replace(candles[-1], is_closed=False)
    return candles


def parse_ticker(info: Any, symbol: str, base: str, quote: str, received_at: datetime) -> Ticker:
    if not isinstance(info, dict) or "c" not in info:
        raise ProviderBadResponse(PROVIDER, "malformed ticker")

    def pick(field: str, index: int) -> float | None:
        values = info.get(field)
        return opt_float(values[index]) if isinstance(values, list) and len(values) > index else None

    volume_24h = pick("v", 1)
    vwap_24h = pick("p", 1)
    return Ticker(
        source=PROVIDER,
        symbol=symbol,
        base_asset=base,
        quote_asset=quote,
        last_price=to_float(info["c"][0], PROVIDER, "c"),
        bid=pick("b", 0),
        ask=pick("a", 0),
        open_24h=None,  # Kraken's 'o' is today's open, not a rolling 24h open.
        high_24h=pick("h", 1),
        low_24h=pick("l", 1),
        pct_change_24h=None,
        volume_base_24h=volume_24h,
        volume_quote_24h=volume_24h * vwap_24h if volume_24h is not None and vwap_24h else None,
        event_time=None,
        received_at=received_at,
    )


def parse_depth(result: dict[str, Any], symbol: str, received_at: datetime) -> OrderBook:
    book = _result_rows(result, "Depth")
    if not isinstance(book, dict):
        raise ProviderBadResponse(PROVIDER, "depth book is not an object")

    def levels(rows: Any, side: str) -> tuple[tuple[float, float], ...]:
        if not isinstance(rows, list):
            raise ProviderBadResponse(PROVIDER, f"depth {side} is not a list")
        return tuple(
            (to_float(r[0], PROVIDER, f"{side}.price"), to_float(r[1], PROVIDER, f"{side}.qty")) for r in rows
        )

    return OrderBook(
        source=PROVIDER,
        symbol=symbol,
        bids=levels(book.get("bids"), "bids"),
        asks=levels(book.get("asks"), "asks"),
        received_at=received_at,
    )


class KrakenSpotAdapter:
    name = PROVIDER

    def __init__(
        self,
        client: KrakenRestClient,
        *,
        quote_asset: str = "USD",
        pairs_ttl_seconds: float = 21600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self.quote_asset = quote_asset
        self._pairs_ttl = pairs_ttl_seconds
        self._clock = clock
        self._pairs: dict[str, str] | None = None
        self._keys_by_alt: dict[str, str] = {}
        self._base_by_alt: dict[str, str] = {}
        self._loaded_at = 0.0

    async def tradable_pairs(self) -> dict[str, str]:
        if self._pairs is None or self._clock() - self._loaded_at > self._pairs_ttl:
            pairs, keys = parse_asset_pairs(await self._client.asset_pairs(), self.quote_asset)
            self._pairs, self._keys_by_alt = pairs, keys
            self._base_by_alt = {alt: base for base, alt in pairs.items()}
            self._loaded_at = self._clock()
        return dict(self._pairs)

    async def tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        if not symbols:
            return {}
        await self.tradable_pairs()
        result = await self._client.ticker(symbols)
        received = utcnow()
        out: dict[str, Ticker] = {}
        for alt in symbols:
            key = self._keys_by_alt.get(alt, alt)
            info = result.get(key) or result.get(alt)
            if info is None:
                continue
            out[alt] = parse_ticker(info, alt, self._base_by_alt.get(alt, alt), self.quote_asset, received)
        return out

    async def candles(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        result = await self._client.ohlc(symbol, timeframe.minutes)
        candles = parse_ohlc(result, symbol, timeframe, utcnow())
        return candles[-limit:] if limit > 0 else candles

    async def order_book(self, symbol: str, depth: int) -> OrderBook:
        return parse_depth(await self._client.depth(symbol, min(depth, 500)), symbol, utcnow())
