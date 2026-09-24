"""Binance adapter: raw Binance payloads -> normalized schemas."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.core.enums import Timeframe
from app.core.timeutil import from_ms, to_ms, utcnow
from app.data.adapters._parse import opt_float, opt_int, to_float
from app.data.http import ProviderBadResponse
from app.data.normalization.schemas import Candle, OrderBook, Ticker
from app.data.providers.binance import PROVIDER, BinanceRestClient


def parse_exchange_info(raw: Any, quote_asset: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), list):
        raise ProviderBadResponse(PROVIDER, "exchangeInfo missing 'symbols'")
    pairs: dict[str, str] = {}
    for item in raw["symbols"]:
        if not isinstance(item, dict):
            continue
        if item.get("status") != "TRADING" or item.get("quoteAsset") != quote_asset:
            continue
        if item.get("isSpotTradingAllowed") is False:
            continue
        base, symbol = item.get("baseAsset"), item.get("symbol")
        if isinstance(base, str) and isinstance(symbol, str):
            pairs[base.upper()] = symbol
    return pairs


def parse_klines(raw: Any, symbol: str, timeframe: Timeframe, now: datetime) -> list[Candle]:
    if not isinstance(raw, list):
        raise ProviderBadResponse(PROVIDER, "klines payload is not a list")
    now_ms = to_ms(now)
    candles: list[Candle] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < 11:
            raise ProviderBadResponse(PROVIDER, "malformed kline row")
        close_time_ms = int(row[6])
        candles.append(
            Candle(
                source=PROVIDER,
                symbol=symbol,
                timeframe=timeframe,
                open_time=from_ms(row[0]),
                close_time=from_ms(close_time_ms + 1),
                open=to_float(row[1], PROVIDER, "open"),
                high=to_float(row[2], PROVIDER, "high"),
                low=to_float(row[3], PROVIDER, "low"),
                close=to_float(row[4], PROVIDER, "close"),
                volume=to_float(row[5], PROVIDER, "volume"),
                quote_volume=opt_float(row[7]),
                trades=opt_int(row[8]),
                taker_buy_base=opt_float(row[9]),
                is_closed=close_time_ms < now_ms,
            )
        )
    return candles


def parse_ticker(raw: Any, base_asset: str, quote_asset: str, received_at: datetime) -> Ticker:
    if not isinstance(raw, dict) or "symbol" not in raw:
        raise ProviderBadResponse(PROVIDER, "malformed 24hr ticker")
    close_time = raw.get("closeTime")
    return Ticker(
        source=PROVIDER,
        symbol=raw["symbol"],
        base_asset=base_asset,
        quote_asset=quote_asset,
        last_price=to_float(raw.get("lastPrice"), PROVIDER, "lastPrice"),
        bid=opt_float(raw.get("bidPrice")),
        ask=opt_float(raw.get("askPrice")),
        open_24h=opt_float(raw.get("openPrice")),
        high_24h=opt_float(raw.get("highPrice")),
        low_24h=opt_float(raw.get("lowPrice")),
        pct_change_24h=opt_float(raw.get("priceChangePercent")),
        volume_base_24h=opt_float(raw.get("volume")),
        volume_quote_24h=opt_float(raw.get("quoteVolume")),
        event_time=from_ms(close_time) if close_time is not None else None,
        received_at=received_at,
    )


def parse_depth(raw: Any, symbol: str, received_at: datetime) -> OrderBook:
    if not isinstance(raw, dict) or not isinstance(raw.get("bids"), list) or not isinstance(raw.get("asks"), list):
        raise ProviderBadResponse(PROVIDER, "malformed depth payload")

    def levels(rows: list[Any], side: str) -> tuple[tuple[float, float], ...]:
        return tuple(
            (to_float(r[0], PROVIDER, f"{side}.price"), to_float(r[1], PROVIDER, f"{side}.qty")) for r in rows
        )

    return OrderBook(
        source=PROVIDER,
        symbol=symbol,
        bids=levels(raw["bids"], "bids"),
        asks=levels(raw["asks"], "asks"),
        received_at=received_at,
        last_update_id=opt_int(raw.get("lastUpdateId")),
    )


class BinanceSpotAdapter:
    name = PROVIDER

    def __init__(
        self,
        client: BinanceRestClient,
        *,
        quote_asset: str = "USDT",
        pairs_ttl_seconds: float = 21600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self.quote_asset = quote_asset
        self._pairs_ttl = pairs_ttl_seconds
        self._clock = clock
        self._pairs: dict[str, str] | None = None
        self._pairs_loaded_at = 0.0
        self._base_by_symbol: dict[str, str] = {}

    async def tradable_pairs(self) -> dict[str, str]:
        if self._pairs is None or self._clock() - self._pairs_loaded_at > self._pairs_ttl:
            pairs = parse_exchange_info(await self._client.exchange_info(), self.quote_asset)
            self._pairs = pairs
            self._base_by_symbol = {symbol: base for base, symbol in pairs.items()}
            self._pairs_loaded_at = self._clock()
        return dict(self._pairs)

    def _base_of(self, symbol: str) -> str:
        if symbol in self._base_by_symbol:
            return self._base_by_symbol[symbol]
        if symbol.endswith(self.quote_asset):
            return symbol[: -len(self.quote_asset)]
        return symbol

    async def tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        if not symbols:
            return {}
        raw = await self._client.tickers_24h(symbols)
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            raise ProviderBadResponse(PROVIDER, "24hr ticker payload is not a list")
        received = utcnow()
        result: dict[str, Ticker] = {}
        for item in raw:
            ticker = parse_ticker(item, self._base_of(item.get("symbol", "")), self.quote_asset, received)
            result[ticker.symbol] = ticker
        return result

    async def candles(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        raw = await self._client.klines(symbol, timeframe.value, limit)
        return parse_klines(raw, symbol, timeframe, utcnow())

    async def order_book(self, symbol: str, depth: int) -> OrderBook:
        raw = await self._client.depth(symbol, depth)
        return parse_depth(raw, symbol, utcnow())
