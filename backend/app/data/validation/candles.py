"""Candle integrity validation.

Produces a cleaned list of *closed* candles plus a report. Forming (unclosed) candles are
never returned for analysis. Critical issues make the timeframe unusable for signals.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import Timeframe
from app.core.timeutil import from_ms, to_ms
from app.data.normalization.schemas import Candle


@dataclass(frozen=True, slots=True)
class CandleAnomaly:
    open_time: datetime
    kind: str
    detail: str


@dataclass
class CandleValidationReport:
    timeframe: Timeframe
    source: str | None
    received: int = 0
    closed: int = 0
    forming_present: bool = False
    duplicates: int = 0
    conflicting_duplicates: int = 0
    invalid_ohlc: int = 0
    misaligned: int = 0
    out_of_order: int = 0
    missing_candles: int = 0
    missing_in_recent_window: int = 0
    gap_ranges: list[tuple[datetime, datetime]] = field(default_factory=list)
    completeness_pct: float = 0.0
    last_closed_open_time: datetime | None = None
    last_closed_close_time: datetime | None = None
    last_closed_age_seconds: float | None = None
    is_stale: bool = False
    insufficient_history: bool = False
    min_required: int = 0
    anomalies: list[CandleAnomaly] = field(default_factory=list)
    recent_anomaly: bool = False
    issues: list[str] = field(default_factory=list)
    critical_issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.critical_issues


def _valid_ohlc(c: Candle) -> bool:
    values = (c.open, c.high, c.low, c.close)
    if not all(math.isfinite(v) and v > 0 for v in values):
        return False
    if not (math.isfinite(c.volume) and c.volume >= 0):
        return False
    if c.high < max(c.open, c.close, c.low) or c.low > min(c.open, c.close, c.high):
        return False
    return c.close_time > c.open_time


def _same_values(a: Candle, b: Candle) -> bool:
    return (a.open, a.high, a.low, a.close, a.volume) == (b.open, b.high, b.low, b.close, b.volume)


def _robust_z(values: Sequence[float]) -> list[float]:
    median = statistics.median(values)
    mad = statistics.median(abs(v - median) for v in values)
    if mad == 0:
        mean_abs = statistics.fmean(abs(v - median) for v in values)
        if mean_abs == 0:
            return [0.0] * len(values)
        return [(v - median) / (1.2533 * mean_abs) for v in values]
    return [0.6745 * (v - median) / mad for v in values]


def validate_candles(
    candles: Sequence[Candle],
    timeframe: Timeframe,
    now: datetime,
    *,
    min_required: int = 50,
    stale_tolerance_intervals: float = 1.0,
    stale_grace_seconds: float = 90.0,
    recent_window: int = 50,
    max_recent_missing_ratio: float = 0.02,
    outlier_z: float = 12.0,
    range_outlier_ratio: float = 15.0,
    anomaly_recent: int = 3,
) -> tuple[list[Candle], CandleValidationReport]:
    report = CandleValidationReport(
        timeframe=timeframe,
        source=candles[0].source if candles else None,
        received=len(candles),
        min_required=min_required,
    )
    interval_ms = timeframe.ms
    now_ms = to_ms(now)

    # 1. ordering and duplicates
    by_time: dict[int, Candle] = {}
    previous_ms: int | None = None
    for candle in candles:
        key = to_ms(candle.open_time)
        if previous_ms is not None and key < previous_ms:
            report.out_of_order += 1
        previous_ms = key
        existing = by_time.get(key)
        if existing is not None:
            report.duplicates += 1
            if not _same_values(existing, candle) and existing.is_closed and candle.is_closed:
                report.conflicting_duplicates += 1
        by_time[key] = candle
    ordered = [by_time[k] for k in sorted(by_time)]

    # 2. structural validity and closed/forming separation
    recent_cutoff_ms = now_ms - interval_ms * (recent_window + 1)
    invalid_recent = 0
    closed: list[Candle] = []
    for candle in ordered:
        open_ms = to_ms(candle.open_time)
        if open_ms % interval_ms != 0:
            report.misaligned += 1
            invalid_recent += open_ms >= recent_cutoff_ms
            continue
        if not _valid_ohlc(candle):
            report.invalid_ohlc += 1
            invalid_recent += open_ms >= recent_cutoff_ms
            continue
        if candle.is_closed and open_ms + interval_ms <= now_ms:
            closed.append(candle)
        else:
            report.forming_present = True
    report.closed = len(closed)

    # 3. gaps (missing candles)
    recent_start_ms = to_ms(closed[-recent_window].open_time) if len(closed) >= recent_window else None
    for prev, cur in zip(closed, closed[1:], strict=False):
        delta = to_ms(cur.open_time) - to_ms(prev.open_time)
        if delta > interval_ms:
            missing = delta // interval_ms - 1
            report.missing_candles += missing
            if recent_start_ms is None or to_ms(cur.open_time) > recent_start_ms:
                report.missing_in_recent_window += missing
            if len(report.gap_ranges) < 10:
                report.gap_ranges.append(
                    (from_ms(to_ms(prev.open_time) + interval_ms), from_ms(to_ms(cur.open_time) - interval_ms))
                )
    total_expected = report.closed + report.missing_candles
    report.completeness_pct = round(100.0 * report.closed / total_expected, 2) if total_expected else 0.0

    # 4. freshness
    if closed:
        last = closed[-1]
        report.last_closed_open_time = last.open_time
        report.last_closed_close_time = last.close_time
        report.last_closed_age_seconds = round((now - last.close_time).total_seconds(), 1)
        allowed = timeframe.seconds * stale_tolerance_intervals + stale_grace_seconds
        report.is_stale = report.last_closed_age_seconds > allowed

    report.insufficient_history = report.closed < min_required

    # 5. anomaly detection (flags only; the integrity gate decides what they mean)
    if len(closed) >= 31:
        returns = [math.log(b.close / a.close) for a, b in zip(closed, closed[1:], strict=False)]
        for idx, z in enumerate(_robust_z(returns), start=1):
            if abs(z) > outlier_z:
                report.anomalies.append(
                    CandleAnomaly(closed[idx].open_time, "return_outlier", f"robust z-score {z:.1f}")
                )
        ranges = [(c.high - c.low) / c.close for c in closed]
        median_range = statistics.median(ranges)
        if median_range > 0:
            for candle, value in zip(closed, ranges, strict=False):
                ratio = value / median_range
                if ratio > range_outlier_ratio:
                    report.anomalies.append(
                        CandleAnomaly(candle.open_time, "range_outlier", f"range {ratio:.1f}x median")
                    )
        recent_times = {c.open_time for c in closed[-anomaly_recent:]}
        report.recent_anomaly = any(a.open_time in recent_times for a in report.anomalies)

    # 6. issue classification
    if not closed:
        report.critical_issues.append("no closed candles received")
    if report.is_stale:
        report.critical_issues.append(
            f"latest closed {timeframe.label} candle is {report.last_closed_age_seconds:.0f}s old (stale)"
        )
    if report.insufficient_history:
        report.critical_issues.append(
            f"only {report.closed} closed {timeframe.label} candles; {min_required} required"
        )
    if report.conflicting_duplicates:
        report.critical_issues.append(f"{report.conflicting_duplicates} conflicting duplicate candles")
    if invalid_recent:
        report.critical_issues.append(f"{invalid_recent} invalid or misaligned candles in the recent window")
    window = min(recent_window, max(report.closed, 1))
    if report.missing_in_recent_window / window > max_recent_missing_ratio:
        report.critical_issues.append(
            f"{report.missing_in_recent_window} missing {timeframe.label} candles in the last {window}"
        )

    if report.duplicates and not report.conflicting_duplicates:
        report.issues.append(f"{report.duplicates} identical duplicate candles removed")
    if report.out_of_order:
        report.issues.append(f"{report.out_of_order} out-of-order candles re-sorted")
    if report.missing_candles > report.missing_in_recent_window:
        report.issues.append(
            f"{report.missing_candles - report.missing_in_recent_window} older missing candles"
        )
    if report.invalid_ohlc + report.misaligned > invalid_recent:
        report.issues.append("older invalid candles removed")
    if report.anomalies:
        report.issues.append(f"{len(report.anomalies)} abnormal candles flagged")

    return closed, report
