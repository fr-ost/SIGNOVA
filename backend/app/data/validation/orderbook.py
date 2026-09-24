"""Order-book sanity checks and liquidity summary (spread, depth, imbalance).

Imbalance is a pressure *proxy* only; it never implies knowledge of anyone's intent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.data.normalization.schemas import OrderBook


@dataclass
class OrderBookSummary:
    source: str
    symbol: str
    valid: bool
    best_bid: float | None = None
    best_ask: float | None = None
    mid: float | None = None
    spread_bps: float | None = None
    band_pct: float = 1.0
    bid_depth_quote: float | None = None
    ask_depth_quote: float | None = None
    imbalance: float | None = None
    bid_levels: int = 0
    ask_levels: int = 0
    issues: list[str] = field(default_factory=list)


def summarize_order_book(book: OrderBook, band_pct: float = 1.0) -> OrderBookSummary:
    summary = OrderBookSummary(
        source=book.source,
        symbol=book.symbol,
        valid=False,
        band_pct=band_pct,
        bid_levels=len(book.bids),
        ask_levels=len(book.asks),
    )
    if not book.bids or not book.asks:
        summary.issues.append("order book side is empty")
        return summary
    levels = book.bids + book.asks
    if any(not (math.isfinite(p) and p > 0 and math.isfinite(q) and q >= 0) for p, q in levels):
        summary.issues.append("order book contains invalid levels")
        return summary
    bids = sorted(book.bids, key=lambda lvl: lvl[0], reverse=True)
    asks = sorted(book.asks, key=lambda lvl: lvl[0])
    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid >= best_ask:
        summary.issues.append("crossed book (best bid >= best ask)")
        return summary
    mid = (best_bid + best_ask) / 2
    low, high = mid * (1 - band_pct / 100), mid * (1 + band_pct / 100)
    bid_depth = sum(p * q for p, q in bids if p >= low)
    ask_depth = sum(p * q for p, q in asks if p <= high)
    total = bid_depth + ask_depth
    summary.valid = True
    summary.best_bid, summary.best_ask, summary.mid = best_bid, best_ask, mid
    summary.spread_bps = round((best_ask - best_bid) / mid * 10_000, 3)
    summary.bid_depth_quote = round(bid_depth, 2)
    summary.ask_depth_quote = round(ask_depth, 2)
    summary.imbalance = round((bid_depth - ask_depth) / total, 4) if total > 0 else None
    if bids[-1][0] > low or asks[-1][0] < high:
        summary.issues.append(f"fetched depth does not fully cover the +/-{band_pct}% band")
    return summary
