"""Shared test fixtures.

The fake adapters and synthetic candles below exist only for tests. Production code
never generates or substitutes data.
"""

from __future__ import annotations

import math
import os
import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from app.config import Settings
from app.core.enums import Timeframe
from app.core.timeutil import floor_to_timeframe, utcnow
from app.data.http import ProviderUnavailable
from app.data.normalization.schemas import Candle, ListingEntry, OrderBook, Ticker

# Tests stay offline: futures data and the pre-scan headline refresh are opt-in per test.
os.environ.setdefault("DERIVATIVES_ENABLED", "false")
os.environ.setdefault("EVIDENCE_REFRESH_NEWS_DEFAULT", "false")


def make_candles(
    timeframe: Timeframe,
    count: int,
    *,
    now: datetime | None = None,
    end_price: float = 100.0,
    vol: float = 0.003,
    seed: int = 7,
    include_forming: bool = True,
    source: str = "fake",
    symbol: str = "BTCUSDT",
) -> list[Candle]:
    """Deterministic random walk whose last closed candle closes at `end_price`."""
    rng = random.Random(seed)
    now = now or utcnow()
    interval = timedelta(seconds=timeframe.seconds)
    current_open = floor_to_timeframe(now, timeframe)
    first_open = current_open - interval * count
    closes = [1.0]
    for _ in range(count):
        closes.append(closes[-1] * math.exp(rng.gauss(0, vol)))
    scale = end_price / closes[count]
    candles: list[Candle] = []
    total = count + (1 if include_forming else 0)
    for i in range(total):
        open_price = closes[i] * scale
        close_price = (closes[i + 1] if i + 1 < len(closes) else closes[-1]) * scale
        high = max(open_price, close_price) * (1 + abs(rng.gauss(0, vol / 3)))
        low = min(open_price, close_price) * (1 - abs(rng.gauss(0, vol / 3)))
        open_time = first_open + interval * i
        candles.append(
            Candle(
                source=source,
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                close_time=open_time + interval,
                open=open_price,
                high=high,
                low=low,
                close=close_price,
                volume=10 + rng.random() * 5,
                quote_volume=None,
                is_closed=open_time + interval <= now,
            )
        )
    return candles


def make_listing(symbol: str, price: float, market_cap: float, *, source: str = "fakelist", tags=(), name=None, rank=None) -> ListingEntry:
    now = utcnow()
    return ListingEntry(
        source=source,
        source_id=symbol.lower(),
        symbol=symbol,
        name=name or symbol.title(),
        slug=symbol.lower(),
        rank=rank,
        price_usd=price,
        market_cap_usd=market_cap,
        volume_24h_usd=market_cap / 50,
        pct_change_1h=0.1,
        pct_change_24h=1.5,
        pct_change_7d=3.0,
        tags=tuple(tags),
        last_updated=now - timedelta(seconds=30),
        fetched_at=now,
    )


# Universe used by service/API tests: 22 coins, one stablecoin, one wrapped token,
# one asset that is not listed on any fake exchange.
UNIVERSE_SPEC = [
    ("BTC", 60000.0, 1.2e12, ()),
    ("ETH", 3000.0, 4.0e11, ()),
    ("USDT", 1.0, 1.4e11, ("stablecoin",)),
    ("XRP", 0.6, 3.5e10, ()),
    ("BNB", 550.0, 8.0e10, ()),
    ("SOL", 150.0, 7.0e10, ()),
    ("USDC", 1.0, 6.0e10, ()),
    ("WBTC", 60000.0, 9.0e9, ()),
    ("DOGE", 0.15, 2.2e10, ()),
    ("ADA", 0.45, 1.6e10, ()),
    ("TRX", 0.12, 1.1e10, ()),
    ("AVAX", 30.0, 1.2e10, ()),
    ("LINK", 14.0, 8.5e9, ()),
    ("DOT", 6.0, 8.0e9, ()),
    ("TON", 5.0, 1.25e10, ()),
    ("SHIB", 0.00002, 1.15e10, ()),
    ("BCH", 400.0, 7.8e9, ()),
    ("LTC", 80.0, 6.0e9, ()),
    ("NEAR", 5.0, 5.5e9, ()),
    ("MATIC", 0.7, 5.0e9, ()),
    ("UNI", 8.0, 4.8e9, ()),
    ("LEO", 6.0, 5.2e9, ()),
    ("ICP", 9.0, 4.0e9, ()),
    ("APT", 8.0, 3.5e9, ()),
]
NOT_ON_EXCHANGE = {"LEO"}


class FakeListingAdapter:
    def __init__(self, name: str = "fakelist", *, fail: bool = False, entries: list[ListingEntry] | None = None) -> None:
        self.name = name
        self.fail = fail
        self.calls = 0
        self._entries = entries

    async def listings(self, limit: int) -> list[ListingEntry]:
        self.calls += 1
        if self.fail:
            raise ProviderUnavailable(self.name, "simulated outage")
        if self._entries is not None:
            return self._entries
        return [make_listing(s, p, m, source=self.name, tags=t) for s, p, m, t in UNIVERSE_SPEC][:limit]


class FakeSpotAdapter:
    def __init__(
        self,
        name: str = "fakeex",
        quote_asset: str = "USDT",
        *,
        prices: dict[str, float] | None = None,
        fail: bool = False,
        price_multiplier: dict[str, float] | None = None,
        stale_ticker_seconds: float = 0.0,
        exclude: set[str] | None = None,
    ) -> None:
        self.name = name
        self.quote_asset = quote_asset
        self.fail = fail
        self.prices = prices or {s: p for s, p, _, _ in UNIVERSE_SPEC}
        self.multiplier = price_multiplier or {}
        self.stale_ticker_seconds = stale_ticker_seconds
        self.exclude = (exclude if exclude is not None else set(NOT_ON_EXCHANGE)) | {"USDT", "USDC"}
        self.calls: dict[str, int] = {}

    def _count(self, op: str) -> None:
        self.calls[op] = self.calls.get(op, 0) + 1

    def _check(self) -> None:
        if self.fail:
            raise ProviderUnavailable(self.name, "simulated outage")

    def _base(self, symbol: str) -> str:
        return symbol[: -len(self.quote_asset)]

    def _price(self, base: str) -> float:
        return self.prices[base] * self.multiplier.get(base, 1.0)

    async def tradable_pairs(self) -> dict[str, str]:
        self._count("pairs")
        self._check()
        return {b: f"{b}{self.quote_asset}" for b in self.prices if b not in self.exclude}

    async def tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        self._count("tickers")
        self._check()
        now = utcnow()
        observed = now - timedelta(seconds=self.stale_ticker_seconds)
        out = {}
        for symbol in symbols:
            price = self._price(self._base(symbol))
            out[symbol] = Ticker(
                source=self.name,
                symbol=symbol,
                base_asset=self._base(symbol),
                quote_asset=self.quote_asset,
                last_price=price,
                bid=price * 0.9999,
                ask=price * 1.0001,
                open_24h=price / 1.01,
                high_24h=price * 1.02,
                low_24h=price * 0.98,
                pct_change_24h=1.0,
                volume_base_24h=1000.0,
                volume_quote_24h=1000.0 * price,
                event_time=observed,
                received_at=now,
            )
        return out

    async def candles(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        self._count("candles")
        self._check()
        candles = make_candles(timeframe, limit, end_price=self._price(self._base(symbol)), source=self.name, symbol=symbol)
        return [replace(c, source=self.name) for c in candles]

    async def order_book(self, symbol: str, depth: int) -> OrderBook:
        self._count("order_book")
        self._check()
        price = self._price(self._base(symbol))
        bids = tuple((price * (1 - 0.0005 * (i + 1)), 2.0) for i in range(depth))
        asks = tuple((price * (1 + 0.0005 * (i + 1)), 1.5) for i in range(depth))
        return OrderBook(source=self.name, symbol=symbol, bids=bids, asks=asks, received_at=utcnow())


class FakeGlobalAdapter:
    name = "fakeglobal"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def global_metrics(self):
        from app.data.normalization.schemas import GlobalMetrics

        if self.fail:
            raise ProviderUnavailable(self.name, "down")
        now = utcnow()
        return GlobalMetrics(self.name, 2.4e12, 9e10, 55.0, 15.0, 1.2, now, now)


class FakeFearGreedAdapter:
    name = "fakefng"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def fear_greed(self):
        from app.data.normalization.schemas import FearGreed

        if self.fail:
            raise ProviderUnavailable(self.name, "down")
        now = utcnow()
        return FearGreed(self.name, 62, "Greed", now, now)


@pytest.fixture
def test_settings(tmp_path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        json_logs=False,
        log_level="WARNING",
        universe_size=20,
        candle_fetch_limit=300,
        candle_fetch_limit_long=300,  # FakeReferenceAdapter mirrors 300-candle exchange history
        candle_min_history=210,
    )


class FakeAltcoinSeasonAdapter:
    name = "fakealt"

    def __init__(self, fail: bool = False, value: int = 38) -> None:
        self.fail = fail
        self.value = value

    async def altcoin_season(self):
        from app.data.normalization.schemas import AltcoinSeason

        if self.fail:
            raise ProviderUnavailable(self.name, "down")
        now = utcnow()
        return AltcoinSeason(self.name, self.value, now, 80, 12, now)


class FakeReferenceAdapter:
    """Aggregated reference candles that mirror the fake exchange (optionally distorted)."""

    supported_timeframes = (Timeframe.H1, Timeframe.D1)

    def __init__(self, name: str = "fakelist", *, scale: float = 1.0, error: Exception | None = None,
                 exchange_limit: int = 300) -> None:
        self.name = name
        self.scale = scale
        self.error = error
        self.exchange_limit = exchange_limit
        self.calls = 0

    async def reference_candles(self, reference_id: str, timeframe: Timeframe, count: int):
        self.calls += 1
        if self.error is not None:
            raise self.error
        base = reference_id.upper()
        price = {s: p for s, p, _, _ in UNIVERSE_SPEC}[base]
        candles = make_candles(timeframe, self.exchange_limit, end_price=price, source=self.name)
        closed = [c for c in candles if c.is_closed][-count:]
        return [replace(c, close=c.close * self.scale, high=c.high * self.scale, low=c.low * self.scale,
                        open=c.open * self.scale) for c in closed]


# --------------------------------------------------------------------------- Phase 2 helpers


def candles_from_closes(
    closes: list[float],
    timeframe: Timeframe = Timeframe.H4,
    *,
    now: datetime | None = None,
    wick: float = 0.004,
    volumes: list[float] | None = None,
    symbol: str = "TESTUSDT",
    source: str = "fake",
) -> list[Candle]:
    """Closed candles ending at the last fully closed interval; each opens at the previous close."""
    now = now or utcnow()
    interval = timedelta(seconds=timeframe.seconds)
    last_open = floor_to_timeframe(now, timeframe) - interval
    first_open = last_open - interval * (len(closes) - 1)
    out: list[Candle] = []
    for i, close in enumerate(closes):
        open_price = closes[i - 1] if i else close
        open_time = first_open + interval * i
        out.append(
            Candle(
                source=source,
                symbol=symbol,
                timeframe=timeframe,
                open_time=open_time,
                close_time=open_time + interval,
                open=open_price,
                high=max(open_price, close) * (1 + wick),
                low=min(open_price, close) * (1 - wick),
                close=close,
                volume=volumes[i] if volumes else 100.0,
                is_closed=True,
            )
        )
    return out


def zigzag(start: float, legs: list[tuple[int, float]]) -> list[float]:
    """Closes moving `pct` percent in total over `steps` candles per leg."""
    closes = [start]
    for steps, pct in legs:
        step = (1 + pct / 100.0) ** (1 / steps)
        for _ in range(steps):
            closes.append(closes[-1] * step)
    return closes


def trend_series(n: int, start: float = 100.0, drift_pct: float = 0.3, wave_pct: float = 2.0, period: int = 24) -> list[float]:
    """Exponential drift with a sine wave on top: swings for structure, a clear trend for EMAs."""
    return [start * (1 + drift_pct / 100.0) ** i * (1 + wave_pct / 100.0 * math.sin(2 * math.pi * i / period)) for i in range(n)]


ANALYSIS_COUNTS = {Timeframe.D1: 300, Timeframe.H4: 400, Timeframe.H1: 400, Timeframe.M15: 300, Timeframe.M5: 300}


def market_candles(
    price_at,
    *,
    now: datetime | None = None,
    counts: dict[Timeframe, int] | None = None,
    wick: float = 0.002,
    symbol: str = "TESTUSDT",
) -> dict[Timeframe, list[Candle]]:
    """Consistent candles on every timeframe, sampled from `price_at(hours_before_now)`.

    Up candles carry more volume than down candles, like a market with buyers in control.
    """
    now = now or utcnow()
    out: dict[Timeframe, list[Candle]] = {}
    for tf, count in (counts or ANALYSIS_COUNTS).items():
        interval = timedelta(seconds=tf.seconds)
        last_open = floor_to_timeframe(now, tf) - interval
        candles = []
        for k in range(count):
            open_time = last_open - interval * (count - 1 - k)
            close_time = open_time + interval
            samples = [
                price_at((now - (open_time + interval * j / 8)).total_seconds() / 3600) for j in range(9)
            ]
            open_price, close_price = samples[0], samples[-1]
            candles.append(
                Candle(
                    source="fake", symbol=symbol, timeframe=tf, open_time=open_time, close_time=close_time,
                    open=open_price, high=max(samples) * (1 + wick), low=min(samples) * (1 - wick),
                    close=close_price, volume=150.0 if close_price >= open_price else 100.0, is_closed=True,
                )
            )
        out[tf] = candles
    return out
