"""Human-readable numbers for reasons and summaries (prices span 0.00001 to 100,000)."""

from __future__ import annotations

import math


def fmt_price(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    magnitude = abs(value)
    if magnitude >= 1000:
        return f"{value:,.2f}"
    if magnitude >= 1:
        return f"{value:,.4f}"
    if magnitude == 0:
        return "0"
    decimals = 3 - math.floor(math.log10(magnitude))  # four significant digits
    return f"{value:.{decimals}f}"


def fmt_pct(value: float | None, digits: int = 2, signed: bool = True) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:+.{digits}f}%" if signed else f"{value:.{digits}f}%"


def fmt_duration(minutes: float) -> str:
    """A holding time in the unit a trader would say: 30 -> '30 minutes', 1440 -> '24 hours', 4320 -> '3 days'."""
    m = round(minutes)
    if m < 120:
        return f"{m} minute{'' if m == 1 else 's'}"
    hours = m / 60
    if hours <= 48:
        return f"{hours:g} hours"
    return f"{hours / 24:g} days"
