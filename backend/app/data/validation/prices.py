"""Cross-source price validation.

The exchange price (Binance or Kraken) is compared with the independent listing
aggregator price (CoinMarketCap / CoinGecko / CoinPaprika). A stale or missing
reference yields UNVERIFIED, never CONSISTENT: cached data is never treated as current.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from app.core.enums import CrossCheckStatus

USD_QUOTES = frozenset({"USD"})
USD_STABLE_QUOTES = frozenset({"USDT", "USDC", "FDUSD"})


@dataclass(frozen=True, slots=True)
class CrossCheck:
    status: CrossCheckStatus
    primary_price_usd: float | None
    reference_price_usd: float | None
    deviation_pct: float | None
    primary_source: str | None
    reference_source: str | None
    reference_age_seconds: float | None
    reason: str


def quote_to_usd_rate(quote_asset: str, listing_prices_usd: dict[str, float]) -> tuple[float | None, str]:
    """USD value of one unit of the quote asset, and how it was obtained."""
    quote = quote_asset.upper()
    if quote in USD_QUOTES:
        return 1.0, "native USD quote"
    if quote in listing_prices_usd and listing_prices_usd[quote] > 0:
        return listing_prices_usd[quote], f"{quote}/USD from listing source"
    if quote in USD_STABLE_QUOTES:
        return None, f"{quote}/USD rate unavailable"
    return None, f"no USD conversion for quote {quote}"


def cross_validate_price(
    *,
    primary_price_usd: float | None,
    primary_source: str | None,
    reference_price_usd: float | None,
    reference_source: str | None,
    reference_updated_at: datetime | None,
    now: datetime,
    warn_pct: float,
    max_pct: float,
    max_reference_age_seconds: float,
) -> CrossCheck:
    def result(status: CrossCheckStatus, deviation: float | None, age: float | None, reason: str) -> CrossCheck:
        return CrossCheck(
            status=status,
            primary_price_usd=primary_price_usd,
            reference_price_usd=reference_price_usd,
            deviation_pct=None if deviation is None else round(deviation, 4),
            primary_source=primary_source,
            reference_source=reference_source,
            reference_age_seconds=None if age is None else round(age, 1),
            reason=reason,
        )

    if primary_price_usd is None or not math.isfinite(primary_price_usd) or primary_price_usd <= 0:
        return result(CrossCheckStatus.UNVERIFIED, None, None, "no valid exchange price")
    if reference_price_usd is None or not math.isfinite(reference_price_usd) or reference_price_usd <= 0:
        return result(CrossCheckStatus.UNVERIFIED, None, None, "no independent reference price")
    age = (now - reference_updated_at).total_seconds() if reference_updated_at else None
    if age is None:
        return result(CrossCheckStatus.UNVERIFIED, None, None, "reference price has no timestamp")
    if age > max_reference_age_seconds:
        return result(
            CrossCheckStatus.UNVERIFIED, None, age, f"reference price is {age:.0f}s old (too old to compare)"
        )
    deviation = abs(primary_price_usd - reference_price_usd) / reference_price_usd * 100.0
    if deviation > max_pct:
        return result(
            CrossCheckStatus.CONFLICT,
            deviation,
            age,
            f"exchange and reference prices differ by {deviation:.2f}% (limit {max_pct}%)",
        )
    if deviation > warn_pct:
        return result(CrossCheckStatus.WARNING, deviation, age, f"prices differ by {deviation:.2f}%")
    return result(CrossCheckStatus.CONSISTENT, deviation, age, f"prices agree within {deviation:.2f}%")
