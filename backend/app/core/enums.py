"""Shared enumerations used across the data, validation and API layers."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class Timeframe(StrEnum):
    """Supported analysis timeframes. Values follow exchange interval notation."""

    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def seconds(self) -> int:
        return _TIMEFRAME_SECONDS[self.value]

    @property
    def ms(self) -> int:
        return self.seconds * 1000

    @property
    def minutes(self) -> int:
        return self.seconds // 60

    @property
    def label(self) -> str:
        """Display label used in the dashboard (5m, 15m, 1H, 4H, 1D)."""
        return _TIMEFRAME_LABELS[self.value]

    @classmethod
    def parse(cls, raw: str) -> Timeframe:
        value = (raw or "").strip().lower()
        try:
            return cls(value)
        except ValueError:
            raise ValueError(
                f"Unsupported timeframe {raw!r}; use one of 5m, 15m, 1H, 4H, 1D"
            ) from None


_TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_TIMEFRAME_LABELS = {"5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}

ALL_TIMEFRAMES: tuple[Timeframe, ...] = (
    Timeframe.M5,
    Timeframe.M15,
    Timeframe.H1,
    Timeframe.H4,
    Timeframe.D1,
)


class DataState(StrEnum):
    """Data-integrity states. Anything other than HEALTHY/DEGRADED blocks actionable signals."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE_DATA = "STALE_DATA"
    DATA_CONFLICT = "DATA_CONFLICT"
    API_FAILURE = "API_FAILURE"
    EXTREME_VOLATILITY = "EXTREME_VOLATILITY"
    SIGNAL_PAUSED = "SIGNAL_PAUSED"


DATA_STATE_SEVERITY: dict[DataState, int] = {
    DataState.HEALTHY: 0,
    DataState.DEGRADED: 1,
    DataState.EXTREME_VOLATILITY: 2,
    DataState.DATA_CONFLICT: 3,
    DataState.STALE_DATA: 4,
    DataState.API_FAILURE: 5,
    DataState.SIGNAL_PAUSED: 6,
}


def worst_state(states: Iterable[DataState]) -> DataState:
    """Return the most severe state; HEALTHY when the iterable is empty."""
    worst = DataState.HEALTHY
    for state in states:
        if DATA_STATE_SEVERITY[state] > DATA_STATE_SEVERITY[worst]:
            worst = state
    return worst


class ProcessingState(StrEnum):
    IDLE = "IDLE"
    ANALYZING = "ANALYZING"
    LIVE_MONITORING = "LIVE_MONITORING"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class ProviderStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    UP = "UP"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    RESTRICTED = "RESTRICTED"
    DOWN = "DOWN"


class CrossCheckStatus(StrEnum):
    CONSISTENT = "CONSISTENT"
    WARNING = "WARNING"
    CONFLICT = "CONFLICT"
    UNVERIFIED = "UNVERIFIED"


class GateStage(StrEnum):
    """Ordered pre-signal integrity pipeline stages."""

    DATA_HEALTH_CHECK = "DATA_HEALTH_CHECK"
    MARKET_DATA_CHECK = "MARKET_DATA_CHECK"
    TIMESTAMP_CHECK = "TIMESTAMP_CHECK"
    CANDLE_COMPLETENESS_CHECK = "CANDLE_COMPLETENESS_CHECK"
    SOURCE_CONSISTENCY_CHECK = "SOURCE_CONSISTENCY_CHECK"
    VOLATILITY_CHECK = "VOLATILITY_CHECK"


class SignalLabel(StrEnum):
    STRONG_BUY = "STRONG BUY"
    BUY = "BUY"
    WATCH = "WATCH"
    NO_TRADE = "NO TRADE"


class Availability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
