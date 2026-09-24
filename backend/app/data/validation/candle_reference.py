"""Cross-check exchange candle history against an independent aggregated source.

Spot-price cross-validation only proves the *current* price is sane. This check proves the
*history* the indicators are computed from is sane too: each closed exchange candle is
matched to CoinMarketCap's aggregated candle for the same UTC period and the closes are
compared (after converting the exchange quote, e.g. USDT, to USD).

Only closes are compared. Highs and lows legitimately differ between one exchange and a
cross-exchange aggregate because wicks are venue-specific.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.enums import CrossCheckStatus, Timeframe
from app.core.timeutil import to_ms
from app.data.normalization.schemas import Candle


@dataclass(frozen=True, slots=True)
class CandleCrossCheck:
    timeframe: Timeframe
    status: CrossCheckStatus
    reference_source: str | None
    compared: int
    median_deviation_pct: float | None
    max_deviation_pct: float | None
    outliers: int
    reason: str


def unverified(timeframe: Timeframe, reason: str, source: str | None = None) -> CandleCrossCheck:
    return CandleCrossCheck(timeframe, CrossCheckStatus.UNVERIFIED, source, 0, None, None, 0, reason)


def cross_validate_candles(
    exchange_closed: Sequence[Candle],
    reference: Sequence[Candle],
    timeframe: Timeframe,
    *,
    usd_rate: float | None,
    reference_source: str,
    warn_pct: float = 1.0,
    max_pct: float = 2.5,
    min_overlap: int = 10,
    max_outlier_share: float = 0.1,
) -> CandleCrossCheck:
    if usd_rate is None or usd_rate <= 0:
        return unverified(timeframe, "exchange quote could not be converted to USD", reference_source)
    by_time = {to_ms(c.open_time): c for c in reference if c.close > 0}
    deviations: list[float] = []
    for candle in exchange_closed:
        ref = by_time.get(to_ms(candle.open_time))
        if ref is None:
            continue
        deviations.append(abs(candle.close * usd_rate - ref.close) / ref.close * 100.0)
    compared = len(deviations)
    if compared < min_overlap:
        return unverified(
            timeframe,
            f"only {compared} overlapping {timeframe.label} candles (need {min_overlap})",
            reference_source,
        )
    median = statistics.median(deviations)
    worst = max(deviations)
    outliers = sum(1 for d in deviations if d > max_pct)
    allowed_outliers = max(1, int(compared * max_outlier_share))
    if median > max_pct or outliers > allowed_outliers:
        status = CrossCheckStatus.CONFLICT
        reason = (
            f"{timeframe.label} history disagrees with {reference_source}: median close gap "
            f"{median:.2f}%, {outliers} of {compared} candles above {max_pct}%"
        )
    elif median > warn_pct or outliers:
        status = CrossCheckStatus.WARNING
        reason = f"{timeframe.label} history mostly agrees (median gap {median:.2f}%, worst {worst:.2f}%)"
    else:
        status = CrossCheckStatus.CONSISTENT
        reason = f"{timeframe.label} history matches {reference_source} (median gap {median:.2f}% over {compared})"
    return CandleCrossCheck(
        timeframe=timeframe,
        status=status,
        reference_source=reference_source,
        compared=compared,
        median_deviation_pct=round(median, 4),
        max_deviation_pct=round(worst, 4),
        outliers=outliers,
        reason=reason,
    )
