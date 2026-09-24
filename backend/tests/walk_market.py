"""A consistent synthetic market for multi-timeframe tests (scalp engine, track record).

One 5-minute random walk with regime drifts and occasional volume bursts, aggregated into
15m/1H/4H candles, so every timeframe agrees. Test-only: production never generates data.
"""

from __future__ import annotations

import math
import random
from collections import OrderedDict
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.core.enums import Timeframe
from app.core.timeutil import floor_to_timeframe, utcnow
from app.data.normalization.schemas import Candle
from tests.conftest import FakeSpotAdapter

WALK_TIMEFRAMES = (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4)


def walk(n5: int, *, seed: int = 1, end: datetime | None = None, drift: float = 0.00004, vol: float = 0.0025,
         symbol: str = "XUSDT", source: str = "fake") -> list[Candle]:
    """`n5` closed 5m candles ending at `end` (default: the last closed 5m candle before now)."""
    rng = random.Random(seed)
    end = end or floor_to_timeframe(utcnow(), Timeframe.M5)
    start = end - timedelta(minutes=5 * n5)
    price, out, regime = 100.0, [], drift
    for k in range(n5):
        if k % 400 == 0:
            regime = rng.choice([drift * 3, drift * 2, drift, -drift, 0.0])
        steps = [price]
        for _ in range(5):
            steps.append(steps[-1] * math.exp(rng.gauss(regime / 5, vol / math.sqrt(5))))
        o, c = steps[0], steps[-1]
        hi = max(steps) * (1 + abs(rng.gauss(0, vol / 4)))
        lo = min(steps) * (1 - abs(rng.gauss(0, vol / 4)))
        v = 100 * (1 + abs(rng.gauss(0, 0.5))) * (3 if rng.random() < 0.03 else 1) * (1.3 if c > o else 1)
        t = start + timedelta(minutes=5 * k)
        out.append(Candle(source, symbol, Timeframe.M5, t, t + timedelta(minutes=5), o, hi, lo, c, v, None, True))
        price = c
    return out


def aggregate(c5: list[Candle], tf: Timeframe) -> list[Candle]:
    if tf == Timeframe.M5:
        return list(c5)
    groups: OrderedDict[int, list[Candle]] = OrderedDict()
    for c in c5:
        ts = int(c.open_time.timestamp())
        groups.setdefault(ts - ts % tf.seconds, []).append(c)
    need = tf.seconds // 300
    out = []
    for key, g in groups.items():
        if len(g) < need:
            continue
        t = datetime.fromtimestamp(key, tz=UTC)
        out.append(Candle(g[0].source, g[0].symbol, tf, t, t + timedelta(seconds=tf.seconds), g[0].open,
                          max(x.high for x in g), min(x.low for x in g), g[-1].close, sum(x.volume for x in g), None, True))
    return out


class WalkSpotAdapter(FakeSpotAdapter):
    """FakeSpotAdapter whose 5m-4H candles come from one consistent walk per coin, scaled to
    the coin's price. Supports paged `history` like the Binance adapter."""

    def __init__(self, *args, n5: int = 16000, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.n5 = n5
        self._series: dict[str, dict[Timeframe, list[Candle]]] = {}

    def series(self, symbol: str) -> dict[Timeframe, list[Candle]]:
        if symbol not in self._series:
            base = self._base(symbol)
            raw = walk(self.n5, seed=sum(map(ord, base)), symbol=symbol, source=self.name)
            scale = self._price(base) / raw[-1].close
            scaled = [replace(c, open=c.open * scale, high=c.high * scale, low=c.low * scale, close=c.close * scale)
                      for c in raw]
            self._series[symbol] = {tf: aggregate(scaled, tf) for tf in WALK_TIMEFRAMES}
        return self._series[symbol]

    async def candles(self, symbol: str, timeframe: Timeframe, limit: int) -> list[Candle]:
        if timeframe not in WALK_TIMEFRAMES:
            return await super().candles(symbol, timeframe, limit)
        self._count("candles")
        self._check()
        return self.series(symbol)[timeframe][-limit:]

    async def history(self, symbol: str, timeframe: Timeframe, total: int) -> list[Candle]:
        self._count("history")
        return await self.candles(symbol, timeframe, total)
