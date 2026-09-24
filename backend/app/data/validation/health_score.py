"""Data health score (0-100).

Weights: freshness 30%, completeness 30%, cross-source consistency 25%, provider 15%.
The score is informational; the integrity gate's pass/fail decision is separate and
stricter (a single critical failure blocks signals regardless of the score).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.enums import CrossCheckStatus, ProviderStatus
from app.data.validation.candles import CandleValidationReport

WEIGHTS = {"freshness": 0.30, "completeness": 0.30, "consistency": 0.25, "provider": 0.15}

_PROVIDER_SCORES = {
    ProviderStatus.UP: 1.0,
    ProviderStatus.DEGRADED: 0.6,
    ProviderStatus.UNKNOWN: 0.5,
    ProviderStatus.RATE_LIMITED: 0.3,
    ProviderStatus.RESTRICTED: 0.0,
    ProviderStatus.DOWN: 0.0,
}

_CONSISTENCY_SCORES = {
    CrossCheckStatus.CONSISTENT: 1.0,
    CrossCheckStatus.WARNING: 0.7,
    CrossCheckStatus.UNVERIFIED: 0.4,
    CrossCheckStatus.CONFLICT: 0.0,
}


def freshness_component(age_seconds: float | None, max_age_seconds: float) -> float:
    if age_seconds is None or max_age_seconds <= 0:
        return 0.0
    if age_seconds <= max_age_seconds * 0.5:
        return 1.0
    if age_seconds >= max_age_seconds:
        return 0.0
    return round(1.0 - (age_seconds - max_age_seconds * 0.5) / (max_age_seconds * 0.5), 4)


def completeness_component(reports: Iterable[CandleValidationReport]) -> float:
    values: list[float] = []
    for report in reports:
        value = report.completeness_pct / 100.0
        if report.is_stale or report.conflicting_duplicates or report.closed == 0:
            value = 0.0
        elif report.insufficient_history:
            value *= 0.5
        elif report.critical_issues:
            value *= 0.5
        values.append(value)
    return round(sum(values) / len(values), 4) if values else 0.0


def consistency_component(status: CrossCheckStatus | None) -> float:
    return _CONSISTENCY_SCORES.get(status, 0.4) if status is not None else 0.4


def provider_component(status: ProviderStatus) -> float:
    return _PROVIDER_SCORES.get(status, 0.5)


def health_score(components: dict[str, float]) -> int:
    """Weighted score over the components provided (weights renormalized).

    The full integrity gate always supplies all four components; the lighter market
    overview supplies only freshness, consistency and provider (no candles fetched).
    """
    present = [name for name in WEIGHTS if name in components]
    weight_sum = sum(WEIGHTS[name] for name in present)
    if not weight_sum:
        return 0
    total = sum(WEIGHTS[name] * max(0.0, min(1.0, components[name])) for name in present)
    return int(round(total / weight_sum * 100))
