"""Pre-signal integrity gate (fail closed).

DATA_HEALTH_CHECK -> MARKET_DATA_CHECK -> TIMESTAMP_CHECK -> CANDLE_COMPLETENESS_CHECK
-> SOURCE_CONSISTENCY_CHECK -> VOLATILITY_CHECK

SOURCE_CONSISTENCY_CHECK covers the live price against an independent reference and,
when a reference candle source is available (CoinMarketCap Startup plan or higher), the
1H and 1D candle history against CoinMarketCap's aggregated candles.

Every stage is evaluated so the dashboard can show *all* reasons, but a single failing
stage makes the decision NO TRADE. Later phases append ANALYSIS_CHECK and RISK_CHECK
stages; nothing downstream (including the LLM) can override a failed gate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import CrossCheckStatus, DataState, GateStage, ProviderStatus, Timeframe, worst_state
from app.data.normalization.schemas import Ticker
from app.data.validation.candle_reference import CandleCrossCheck
from app.data.validation.candles import CandleValidationReport
from app.data.validation.health_score import (
    completeness_component,
    consistency_component,
    freshness_component,
    health_score,
    provider_component,
)
from app.data.validation.prices import CrossCheck
from app.data.validation.volatility import VolatilityCheck

_FUTURE_SKEW_SECONDS = 30.0
_FAILED_PROVIDER_STATES = {ProviderStatus.DOWN, ProviderStatus.RESTRICTED}


@dataclass
class IntegrityInputs:
    symbol: str
    now: datetime
    market_source: str | None
    provider_status: ProviderStatus
    ticker: Ticker | None
    ticker_max_age_seconds: float
    candle_reports: dict[Timeframe, CandleValidationReport]
    required_timeframes: tuple[Timeframe, ...]
    cross_check: CrossCheck | None
    volatility: VolatilityCheck | None
    require_cross_validation: bool = True
    signal_paused_reason: str | None = None
    candle_cross_checks: dict[Timeframe, CandleCrossCheck] = field(default_factory=dict)


@dataclass
class StageResult:
    stage: GateStage
    passed: bool
    state: DataState
    reasons: list[str] = field(default_factory=list)


@dataclass
class IntegrityResult:
    symbol: str
    checked_at: datetime
    passed: bool
    state: DataState
    data_health_score: int
    components: dict[str, float]
    stages: list[StageResult]
    reasons: list[str]

    @property
    def decision(self) -> str:
        return "PASS" if self.passed else "NO TRADE"


def _stage(stage: GateStage, failures: list[tuple[DataState, str]], notes: list[str] | None = None) -> StageResult:
    notes = notes or []
    if failures:
        return StageResult(stage, False, worst_state(s for s, _ in failures), [r for _, r in failures] + notes)
    return StageResult(stage, True, DataState.DEGRADED if notes else DataState.HEALTHY, notes)


class IntegrityGate:
    def evaluate(self, data: IntegrityInputs) -> IntegrityResult:
        stages = [
            self._data_health(data),
            self._market_data(data),
            self._timestamps(data),
            self._candles(data),
            self._consistency(data),
            self._volatility(data),
        ]
        passed = all(stage.passed for stage in stages)
        state = worst_state(stage.state for stage in stages)
        reasons = [reason for stage in stages if not stage.passed for reason in stage.reasons]
        ticker_age = (
            (data.now - data.ticker.observed_at).total_seconds() if data.ticker is not None else None
        )
        components = {
            "freshness": freshness_component(ticker_age, data.ticker_max_age_seconds),
            "completeness": completeness_component(
                data.candle_reports[tf] for tf in data.required_timeframes if tf in data.candle_reports
            )
            if any(tf in data.candle_reports for tf in data.required_timeframes)
            else 0.0,
            "consistency": consistency_component(data.cross_check.status if data.cross_check else None),
            "provider": provider_component(data.provider_status),
        }
        return IntegrityResult(
            symbol=data.symbol,
            checked_at=data.now,
            passed=passed,
            state=state,
            data_health_score=health_score(components),
            components=components,
            stages=stages,
            reasons=reasons,
        )

    # -- stages -----------------------------------------------------------------------

    def _data_health(self, d: IntegrityInputs) -> StageResult:
        failures: list[tuple[DataState, str]] = []
        notes: list[str] = []
        if d.signal_paused_reason:
            failures.append((DataState.SIGNAL_PAUSED, f"signals paused: {d.signal_paused_reason}"))
        if d.market_source is None:
            failures.append((DataState.API_FAILURE, "no spot market source available for this asset"))
        elif d.provider_status in _FAILED_PROVIDER_STATES:
            failures.append((DataState.API_FAILURE, f"market provider {d.market_source} is {d.provider_status}"))
        elif d.provider_status in (ProviderStatus.DEGRADED, ProviderStatus.RATE_LIMITED):
            notes.append(f"market provider {d.market_source} is {d.provider_status}")
        return _stage(GateStage.DATA_HEALTH_CHECK, failures, notes)

    def _market_data(self, d: IntegrityInputs) -> StageResult:
        failures: list[tuple[DataState, str]] = []
        t = d.ticker
        if t is None:
            failures.append((DataState.API_FAILURE, "live ticker unavailable"))
        else:
            if not (math.isfinite(t.last_price) and t.last_price > 0):
                failures.append((DataState.DATA_CONFLICT, f"invalid last price {t.last_price!r}"))
            if t.bid is not None and t.ask is not None and t.bid > 0 and t.ask > 0 and t.bid > t.ask:
                failures.append((DataState.DATA_CONFLICT, "ticker bid is above ask (crossed quote)"))
            if t.high_24h is not None and t.low_24h is not None and t.high_24h < t.low_24h:
                failures.append((DataState.DATA_CONFLICT, "24h high is below 24h low"))
        return _stage(GateStage.MARKET_DATA_CHECK, failures)

    def _timestamps(self, d: IntegrityInputs) -> StageResult:
        failures: list[tuple[DataState, str]] = []
        notes: list[str] = []
        if d.ticker is not None:
            age = (d.now - d.ticker.observed_at).total_seconds()
            if age < -_FUTURE_SKEW_SECONDS:
                failures.append((DataState.DATA_CONFLICT, f"ticker timestamp is {-age:.0f}s in the future"))
            elif age > d.ticker_max_age_seconds:
                failures.append((DataState.STALE_DATA, f"ticker is {age:.0f}s old (limit {d.ticker_max_age_seconds:.0f}s)"))
            if d.ticker.event_time is None:
                notes.append("provider supplies no ticker timestamp; receipt time used")
        return _stage(GateStage.TIMESTAMP_CHECK, failures, notes)

    def _candles(self, d: IntegrityInputs) -> StageResult:
        failures: list[tuple[DataState, str]] = []
        notes: list[str] = []
        for tf in d.required_timeframes:
            report = d.candle_reports.get(tf)
            if report is None:
                failures.append((DataState.API_FAILURE, f"{tf.label} candles unavailable"))
                continue
            for issue in report.critical_issues:
                if report.is_stale and "stale" in issue:
                    state = DataState.STALE_DATA
                elif report.conflicting_duplicates and "conflicting" in issue:
                    state = DataState.DATA_CONFLICT
                else:
                    state = DataState.DEGRADED
                failures.append((state, f"{tf.label}: {issue}"))
            notes.extend(f"{tf.label}: {issue}" for issue in report.issues)
        return _stage(GateStage.CANDLE_COMPLETENESS_CHECK, failures, notes)

    def _consistency(self, d: IntegrityInputs) -> StageResult:
        failures: list[tuple[DataState, str]] = []
        notes: list[str] = []
        cc = d.cross_check
        if cc is None or cc.status == CrossCheckStatus.UNVERIFIED:
            reason = f"price not cross-verified: {cc.reason if cc else 'no check performed'}"
            if d.require_cross_validation:
                failures.append((DataState.DEGRADED, reason))
            else:
                notes.append(reason)
        elif cc.status == CrossCheckStatus.CONFLICT:
            failures.append((DataState.DATA_CONFLICT, cc.reason))
        elif cc.status == CrossCheckStatus.WARNING:
            notes.append(cc.reason)

        # Candle history vs an independent aggregate (enrichment: a conflict blocks, an
        # unavailable reference is only noted because lower API plans cannot provide it).
        for check in d.candle_cross_checks.values():
            if check.status == CrossCheckStatus.CONFLICT:
                failures.append((DataState.DATA_CONFLICT, check.reason))
            elif check.status == CrossCheckStatus.WARNING:
                notes.append(check.reason)
            elif check.status == CrossCheckStatus.UNVERIFIED:
                notes.append(f"{check.timeframe.label} candle history not cross-verified: {check.reason}")

        corroborated = cc is not None and cc.status in (CrossCheckStatus.CONSISTENT, CrossCheckStatus.WARNING)
        for tf in d.required_timeframes:
            report = d.candle_reports.get(tf)
            if report is not None and report.recent_anomaly and not corroborated:
                failures.append(
                    (DataState.DATA_CONFLICT, f"{tf.label}: abnormal recent candle not corroborated by a second source")
                )
        return _stage(GateStage.SOURCE_CONSISTENCY_CHECK, failures, notes)

    def _volatility(self, d: IntegrityInputs) -> StageResult:
        failures: list[tuple[DataState, str]] = []
        v = d.volatility
        if v is None or not v.available:
            reason = v.reason if v else "no 5m data"
            failures.append((DataState.DEGRADED, f"volatility could not be assessed ({reason})"))
        elif v.extreme:
            failures.append((DataState.EXTREME_VOLATILITY, f"extreme volatility: {v.reason}"))
        return _stage(GateStage.VOLATILITY_CHECK, failures)
