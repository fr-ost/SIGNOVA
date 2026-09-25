"""Perpetual-futures data from public exchange APIs: parsers and symbol mapping (no I/O).

Spot is what this dashboard trades, but the futures market shows how traders are positioned:
funding (who pays to hold a position), open interest (how much leverage is in the market),
long/short ratios (all accounts, and the largest "top trader" accounts), taker volume (who is
hitting the market) and forced liquidations. Sources, all public and keyless:

* Binance USD-M futures: funding, open-interest history, global and top-trader long/short
  ratios, taker buy/sell volume, funding history.
* Bybit v5 (linear): funding, open interest, open-interest history, account ratio, funding history.
* OKX v5: instruments, open-interest history, long/short ratio, taker volume, liquidation orders.
* Hyperliquid: funding and open interest for every perpetual in one request.

Exchanges quote some small coins per 1000 units (1000PEPEUSDT, kPEPE): the mapping keeps the
multiplier so open interest is always counted in coins of the spot asset. Funding is expressed
in percent per 8 hours (Hyperliquid funds hourly and is scaled; exchanges that fund every 4
hours are shown per funding period).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Point:
    time: datetime
    value: float


@dataclass(frozen=True)
class Liquidation:
    time: datetime
    side: str  # "long": a long position was closed by force (a market sell); "short": a forced buy
    price: float
    usd: float


@dataclass(frozen=True)
class PerpQuote:
    """One perpetual in an exchange's all-market listing."""

    venue: str
    symbol: str  # the exchange's perpetual symbol
    multiplier: float  # spot coins per contract unit (1000 for 1000PEPEUSDT)
    funding_pct_8h: float | None
    open_interest_usd: float | None
    mark: float | None  # price per spot coin


class ParseError(ValueError):
    """The payload does not have the documented shape (or the exchange returned an error code)."""


def num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def ms_time(value: Any) -> datetime | None:
    v = num(value)
    if v is None or v <= 0:
        return None
    if v > 1e14:  # microseconds
        v /= 1000.0
    try:
        return datetime.fromtimestamp(v / 1000.0, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _sorted(points: Iterable[Point]) -> list[Point]:
    unique = {p.time: p for p in points}
    return [unique[t] for t in sorted(unique)]


# ----------------------------------------------------------------------------- symbol mapping


def binance_candidates(base: str) -> list[tuple[str, float]]:
    b = base.upper()
    return [(f"{b}USDT", 1.0), (f"1000{b}USDT", 1000.0), (f"1000000{b}USDT", 1e6), (f"1M{b}USDT", 1e6)]


def bybit_candidates(base: str) -> list[tuple[str, float]]:
    b = base.upper()
    return [(f"{b}USDT", 1.0), (f"1000{b}USDT", 1000.0), (f"{b}1000USDT", 1000.0), (f"10000{b}USDT", 1e4),
            (f"1000000{b}USDT", 1e6)]


def hyperliquid_candidates(base: str) -> list[tuple[str, float]]:
    b = base.upper()
    return [(b, 1.0), (f"k{b}", 1000.0)]


def okx_inst(base: str) -> str:
    return f"{base.upper()}-USDT-SWAP"


def resolve(available: dict[str, Any] | set[str], candidates: list[tuple[str, float]]) -> tuple[str, float] | None:
    for symbol, mult in candidates:
        if symbol in available:
            return symbol, mult
    return None


# ----------------------------------------------------------------------------- Binance USD-M futures


def binance_premium(payload: Any) -> dict[str, PerpQuote]:
    """GET /fapi/v1/premiumIndex (all symbols): mark price and last funding rate."""
    if not isinstance(payload, list):
        raise ParseError("premiumIndex payload is not a list")
    out: dict[str, PerpQuote] = {}
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
            continue
        rate = num(item.get("lastFundingRate"))
        out[item["symbol"]] = PerpQuote("binance", item["symbol"], 1.0, rate * 100 if rate is not None else None,
                                        None, num(item.get("markPrice")))
    return out


def binance_series(payload: Any, key: str, *, scale: float = 1.0) -> list[Point]:
    """/futures/data/* history (openInterestHist, *LongShort*Ratio, takerlongshortRatio)."""
    if not isinstance(payload, list):
        raise ParseError("futures data payload is not a list")
    points = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        t, v = ms_time(item.get("timestamp")), num(item.get(key))
        if t is not None and v is not None:
            points.append(Point(t, v * scale))
    return _sorted(points)


def binance_funding(payload: Any) -> list[Point]:
    """GET /fapi/v1/fundingRate: settled funding rates (percent per period)."""
    if not isinstance(payload, list):
        raise ParseError("fundingRate payload is not a list")
    points = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        t, v = ms_time(item.get("fundingTime")), num(item.get("fundingRate"))
        if t is not None and v is not None:
            points.append(Point(t, v * 100))
    return _sorted(points)


# ----------------------------------------------------------------------------- Bybit v5


def bybit_list(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ParseError("bybit payload is not an object")
    if payload.get("retCode") not in (0, "0"):
        raise ParseError(f"bybit error {payload.get('retCode')}: {str(payload.get('retMsg'))[:80]}")
    result = payload.get("result")
    rows = result.get("list") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise ParseError("bybit payload has no result.list")
    return [r for r in rows if isinstance(r, dict)]


def bybit_tickers(payload: Any) -> dict[str, PerpQuote]:
    """GET /v5/market/tickers?category=linear."""
    out: dict[str, PerpQuote] = {}
    for item in bybit_list(payload):
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol.endswith("USDT"):
            continue
        rate = num(item.get("fundingRate"))
        out[symbol] = PerpQuote("bybit", symbol, 1.0, rate * 100 if rate is not None else None,
                                num(item.get("openInterestValue")), num(item.get("markPrice")))
    return out


def bybit_series(payload: Any, key: str, time_key: str = "timestamp", *, scale: float = 1.0) -> list[Point]:
    points = []
    for item in bybit_list(payload):
        t, v = ms_time(item.get(time_key)), num(item.get(key))
        if t is not None and v is not None:
            points.append(Point(t, v * scale))
    return _sorted(points)


# ----------------------------------------------------------------------------- OKX v5


def okx_data(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        raise ParseError("okx payload is not an object")
    if str(payload.get("code")) != "0":
        raise ParseError(f"okx error {payload.get('code')}: {str(payload.get('msg'))[:80]}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ParseError("okx payload has no data list")
    return data


def okx_instruments(payload: Any) -> dict[str, float]:
    """GET /api/v5/public/instruments?instType=SWAP -> instId: contract value in coins."""
    out: dict[str, float] = {}
    for item in okx_data(payload):
        if not isinstance(item, dict):
            continue
        inst, ct = item.get("instId"), num(item.get("ctVal"))
        if isinstance(inst, str) and inst.endswith("-USDT-SWAP") and ct and item.get("state", "live") == "live":
            out[inst] = ct
    return out


def okx_rows(payload: Any, column: int) -> list[Point]:
    """Rubik statistics: rows of [ts, value, ...] (newest first)."""
    points = []
    for row in okx_data(payload):
        if not isinstance(row, list | tuple) or len(row) <= column:
            continue
        t, v = ms_time(row[0]), num(row[column])
        if t is not None and v is not None:
            points.append(Point(t, v))
    return _sorted(points)


def okx_taker_ratio(payload: Any) -> list[Point]:
    """GET /api/v5/rubik/stat/taker-volume: rows [ts, sellVol, buyVol] -> buy/sell ratio."""
    points = []
    for row in okx_data(payload):
        if not isinstance(row, list | tuple) or len(row) < 3:
            continue
        t, sell, buy = ms_time(row[0]), num(row[1]), num(row[2])
        if t is not None and sell and buy is not None:
            points.append(Point(t, buy / sell))
    return _sorted(points)


def okx_liquidations(payload: Any, ct_val: float, multiplier: float = 1.0) -> list[Liquidation]:
    """GET /api/v5/public/liquidation-orders?instType=SWAP&state=filled: forced orders."""
    out: list[Liquidation] = []
    for item in okx_data(payload):
        details = item.get("details") if isinstance(item, dict) else None
        for d in details if isinstance(details, list) else []:
            if not isinstance(d, dict):
                continue
            t, size, price = ms_time(d.get("ts")), num(d.get("sz")), num(d.get("bkPx"))
            side = str(d.get("posSide") or "").lower()
            if side not in ("long", "short"):  # net mode: a forced sell closes a long
                side = "long" if str(d.get("side", "")).lower() == "sell" else "short"
            if t is None or not size or not price or price <= 0:
                continue
            coins = size * ct_val
            out.append(Liquidation(t, side, price / multiplier, coins * price))
    return sorted(out, key=lambda x: x.time)


def okx_funding(payload: Any) -> float | None:
    """GET /api/v5/public/funding-rate?instId=...: current rate (percent per 8h)."""
    for item in okx_data(payload):
        if isinstance(item, dict) and (rate := num(item.get("fundingRate"))) is not None:
            return rate * 100
    return None


# ----------------------------------------------------------------------------- Hyperliquid


def hyperliquid_contexts(payload: Any) -> dict[str, PerpQuote]:
    """POST /info {"type": "metaAndAssetCtxs"}: [meta with universe, [asset contexts]]."""
    if not isinstance(payload, list) or len(payload) < 2 or not isinstance(payload[0], dict):
        raise ParseError("metaAndAssetCtxs payload has an unexpected shape")
    universe, contexts = payload[0].get("universe"), payload[1]
    if not isinstance(universe, list) or not isinstance(contexts, list):
        raise ParseError("metaAndAssetCtxs payload has no universe")
    out: dict[str, PerpQuote] = {}
    for meta, ctx in zip(universe, contexts, strict=False):
        if not isinstance(meta, dict) or not isinstance(ctx, dict) or not isinstance(meta.get("name"), str):
            continue
        if meta.get("isDelisted"):
            continue
        funding, oi, mark = num(ctx.get("funding")), num(ctx.get("openInterest")), num(ctx.get("markPx"))
        out[meta["name"]] = PerpQuote(
            "hyperliquid", meta["name"], 1.0, funding * 8 * 100 if funding is not None else None,
            oi * mark if oi is not None and mark is not None else None, mark,
        )
    return out
