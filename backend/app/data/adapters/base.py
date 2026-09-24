"""Adapter interfaces. Every provider is consumed through one of these contracts."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.enums import Timeframe
from app.data.normalization.schemas import (
    AltcoinSeason,
    Candle,
    FearGreed,
    GlobalMetrics,
    ListingEntry,
    OrderBook,
    Ticker,
)


@runtime_checkable
class SpotMarketAdapter(Protocol):
    name: str
    quote_asset: str

    async def tradable_pairs(self) -> dict[str, str]:
        """Map of base asset (e.g. BTC) -> exchange symbol (e.g. BTCUSDT)."""

    async def tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        """Tickers keyed by exchange symbol. Missing symbols are simply absent."""

    async def candles(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        """Most recent candles, oldest first. The newest may be an unclosed candle."""

    async def order_book(self, symbol: str, depth: int) -> OrderBook: ...


@runtime_checkable
class ListingAdapter(Protocol):
    name: str

    async def listings(self, limit: int) -> list[ListingEntry]: ...


@runtime_checkable
class GlobalMetricsAdapter(Protocol):
    name: str

    async def global_metrics(self) -> GlobalMetrics: ...


@runtime_checkable
class FearGreedAdapter(Protocol):
    name: str

    async def fear_greed(self) -> FearGreed: ...


@runtime_checkable
class AltcoinSeasonAdapter(Protocol):
    name: str

    async def altcoin_season(self) -> AltcoinSeason: ...


@runtime_checkable
class ReferenceCandleAdapter(Protocol):
    """Independent aggregated candles used to cross-check exchange candle history."""

    name: str
    supported_timeframes: tuple[Timeframe, ...]

    async def reference_candles(self, reference_id: str, timeframe: Timeframe, count: int) -> list[Candle]:
        """Closed candles in USD, oldest first."""
