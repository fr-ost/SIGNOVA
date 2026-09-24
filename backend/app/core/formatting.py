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
