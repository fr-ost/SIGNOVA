"""Normalized, provider-independent data structures.

Provider -> Adapter -> these schemas -> Validation -> Database / Analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.enums import Timeframe


@dataclass(frozen=True, slots=True)
class Candle:
    source: str
    symbol: str
    timeframe: Timeframe
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float | None = None
    trades: int | None = None
    taker_buy_base: float | None = None
    is_closed: bool = True


@dataclass(frozen=True, slots=True)
class Ticker:
    source: str
    symbol: str
    base_asset: str
    quote_asset: str
    last_price: float
    bid: float | None
    ask: float | None
    open_24h: float | None
    high_24h: float | None
    low_24h: float | None
    pct_change_24h: float | None
    volume_base_24h: float | None
    volume_quote_24h: float | None
    event_time: datetime | None
    received_at: datetime

    @property
    def observed_at(self) -> datetime:
        """Provider timestamp when available, otherwise the local receipt time."""
        return self.event_time or self.received_at


@dataclass(frozen=True, slots=True)
class OrderBook:
    source: str
    symbol: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    received_at: datetime
    last_update_id: int | None = None


@dataclass(frozen=True, slots=True)
class ListingEntry:
    source: str
    source_id: str
    symbol: str
    name: str
    slug: str | None
    rank: int | None
    price_usd: float
    market_cap_usd: float
    volume_24h_usd: float | None
    pct_change_1h: float | None
    pct_change_24h: float | None
    pct_change_7d: float | None
    tags: tuple[str, ...]
    last_updated: datetime | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class GlobalMetrics:
    source: str
    total_market_cap_usd: float
    total_volume_24h_usd: float | None
    btc_dominance_pct: float | None
    eth_dominance_pct: float | None
    market_cap_change_24h_pct: float | None
    updated_at: datetime | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class FearGreed:
    source: str
    value: int
    classification: str
    updated_at: datetime | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class AltcoinSeason:
    """CoinMarketCap Altcoin Season Index: above 75 suggests altcoin season, below 25 Bitcoin season."""

    source: str
    value: int
    snapshot_time: datetime | None
    yearly_high: int | None
    yearly_low: int | None
    fetched_at: datetime
