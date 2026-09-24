"""Spot market routing with provider failover (Binance -> Kraken).

Each asset gets an ordered list of candidate markets. Every call tries the first
candidate and fails over to the next on provider errors, recording which source
actually served the data. Nothing is ever silently substituted: the source is
always returned alongside the data.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass

from app.core.enums import Timeframe
from app.data.adapters.base import SpotMarketAdapter
from app.data.health import ProviderHealthRegistry
from app.data.http import ProviderError
from app.data.normalization.schemas import Candle, OrderBook, Ticker

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MarketRef:
    adapter: str
    symbol: str
    base_asset: str
    quote_asset: str


class NoMarketData(Exception):
    def __init__(self, base_asset: str, errors: list[str]) -> None:
        super().__init__(f"{base_asset}: no market data ({'; '.join(errors) or 'no candidate markets'})")
        self.base_asset = base_asset
        self.errors = errors


class SpotMarketRouter:
    def __init__(self, adapters: list[SpotMarketAdapter], health: ProviderHealthRegistry) -> None:
        if not adapters:
            raise ValueError("at least one spot market adapter is required")
        self._adapters = {adapter.name: adapter for adapter in adapters}
        self._order = [adapter.name for adapter in adapters]
        self._health = health

    @property
    def adapter_names(self) -> list[str]:
        return list(self._order)

    async def resolve(
        self, base_assets: list[str], overrides: dict[str, str] | None = None
    ) -> tuple[dict[str, list[MarketRef]], list[str]]:
        """Candidate markets per base asset, in adapter priority order, plus errors."""
        overrides = overrides or {}
        errors: list[str] = []
        pairs_by_adapter: dict[str, dict[str, str]] = {}

        async def load(name: str) -> None:
            try:
                pairs_by_adapter[name] = await self._adapters[name].tradable_pairs()
            except ProviderError as exc:
                errors.append(f"{name}: pair list unavailable ({exc.message})")

        await asyncio.gather(*(load(name) for name in self._order))
        result: dict[str, list[MarketRef]] = {}
        for base in base_assets:
            exchange_base = overrides.get(base, base)
            refs = []
            for name in self._order:
                symbol = pairs_by_adapter.get(name, {}).get(exchange_base)
                if symbol:
                    refs.append(MarketRef(name, symbol, base, self._adapters[name].quote_asset))
            result[base] = refs
        return result, errors

    async def tickers(
        self, candidates: dict[str, list[MarketRef]]
    ) -> tuple[dict[str, tuple[Ticker, MarketRef]], dict[str, list[str]]]:
        """Fetch tickers in batches per adapter, failing over per asset."""
        position = {base: 0 for base, refs in candidates.items() if refs}
        found: dict[str, tuple[Ticker, MarketRef]] = {}
        errors: dict[str, list[str]] = defaultdict(list)
        for base, refs in candidates.items():
            if not refs:
                errors[base].append("not listed on any supported spot exchange")

        while position:
            groups: dict[str, list[MarketRef]] = defaultdict(list)
            for base, index in position.items():
                ref = candidates[base][index]
                groups[ref.adapter].append(ref)
            advance: list[str] = []
            for adapter_name, refs in groups.items():
                try:
                    tickers = await self._adapters[adapter_name].tickers([r.symbol for r in refs])
                except ProviderError as exc:
                    for ref in refs:
                        errors[ref.base_asset].append(f"{adapter_name}: {exc.message}")
                        advance.append(ref.base_asset)
                    continue
                for ref in refs:
                    ticker = tickers.get(ref.symbol)
                    if ticker is None:
                        errors[ref.base_asset].append(f"{adapter_name}: ticker missing for {ref.symbol}")
                        advance.append(ref.base_asset)
                    else:
                        found[ref.base_asset] = (ticker, ref)
                        position.pop(ref.base_asset, None)
            for base in advance:
                position[base] += 1
                if position[base] >= len(candidates[base]):
                    position.pop(base)
        return found, dict(errors)

    async def candles(
        self, candidates: list[MarketRef], timeframe: Timeframe, limit: int
    ) -> tuple[list[Candle], MarketRef]:
        errors: list[str] = []
        for ref in candidates:
            try:
                return await self._adapters[ref.adapter].candles(ref.symbol, timeframe, limit), ref
            except ProviderError as exc:
                errors.append(f"{ref.adapter}: {exc.message}")
                log.warning("candle source failed", extra={"adapter": ref.adapter, "symbol": ref.symbol})
        base = candidates[0].base_asset if candidates else "?"
        raise NoMarketData(base, errors)

    async def history(
        self, candidates: list[MarketRef], timeframe: Timeframe, total: int
    ) -> tuple[list[Candle], MarketRef]:
        """Longer history for backtests: paged where the exchange supports it (Binance),
        otherwise the most recent candles one request allows."""
        errors: list[str] = []
        for ref in candidates:
            adapter = self._adapters[ref.adapter]
            try:
                if hasattr(adapter, "history"):
                    return await adapter.history(ref.symbol, timeframe, total), ref
                return await adapter.candles(ref.symbol, timeframe, min(total, 1000)), ref
            except ProviderError as exc:
                errors.append(f"{ref.adapter}: {exc.message}")
        base = candidates[0].base_asset if candidates else "?"
        raise NoMarketData(base, errors)

    async def order_book(self, candidates: list[MarketRef], depth: int) -> tuple[OrderBook, MarketRef]:
        errors: list[str] = []
        for ref in candidates:
            try:
                return await self._adapters[ref.adapter].order_book(ref.symbol, depth), ref
            except ProviderError as exc:
                errors.append(f"{ref.adapter}: {exc.message}")
        base = candidates[0].base_asset if candidates else "?"
        raise NoMarketData(base, errors)
