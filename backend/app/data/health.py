"""Provider health tracking.

Every provider call reports success, failure, rate limiting or geo-restriction here.
The registry is the single source of truth for /api/provider-health and for the
DATA_HEALTH_CHECK stage of the integrity gate. Status transitions are marked dirty so
they can be persisted to the provider_health table without writing on every request.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.enums import ProviderStatus
from app.core.timeutil import utcnow

_DOWN_AFTER_CONSECUTIVE_FAILURES = 3
_SLOW_LATENCY_MS = 4000.0
_EWMA_ALPHA = 0.2


@dataclass
class ProviderHealth:
    provider: str
    role: str = "unknown"
    status: ProviderStatus = ProviderStatus.UNKNOWN
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0
    rate_limit_hits: int = 0
    last_latency_ms: float | None = None
    avg_latency_ms: float | None = None
    circuit_state: str = "closed"
    status_changed_at: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)


class ProviderHealthRegistry:
    def __init__(self, clock: Callable[[], datetime] = utcnow) -> None:
        self._clock = clock
        self._items: dict[str, ProviderHealth] = {}
        self._dirty: set[str] = set()

    def register(self, provider: str, role: str) -> None:
        item = self._items.get(provider)
        if item is None:
            self._items[provider] = ProviderHealth(provider=provider, role=role)
        else:
            item.role = role

    def _item(self, provider: str) -> ProviderHealth:
        if provider not in self._items:
            self._items[provider] = ProviderHealth(provider=provider)
        return self._items[provider]

    def _set_status(self, item: ProviderHealth, status: ProviderStatus) -> None:
        if item.status != status:
            item.status = status
            item.status_changed_at = self._clock()
            self._dirty.add(item.provider)

    def record_success(self, provider: str, latency_ms: float) -> None:
        item = self._item(provider)
        item.total_requests += 1
        item.consecutive_failures = 0
        item.last_success_at = self._clock()
        item.last_latency_ms = round(latency_ms, 1)
        item.avg_latency_ms = (
            round(latency_ms, 1)
            if item.avg_latency_ms is None
            else round(item.avg_latency_ms * (1 - _EWMA_ALPHA) + latency_ms * _EWMA_ALPHA, 1)
        )
        status = ProviderStatus.DEGRADED if item.avg_latency_ms > _SLOW_LATENCY_MS else ProviderStatus.UP
        self._set_status(item, status)

    def record_failure(
        self,
        provider: str,
        error: str,
        *,
        status: ProviderStatus | None = None,
    ) -> None:
        item = self._item(provider)
        item.total_requests += 1
        item.total_failures += 1
        item.consecutive_failures += 1
        item.last_failure_at = self._clock()
        item.last_error = error[:500]
        if status is None:
            status = (
                ProviderStatus.DOWN
                if item.consecutive_failures >= _DOWN_AFTER_CONSECUTIVE_FAILURES
                else ProviderStatus.DEGRADED
            )
        self._set_status(item, status)

    def record_rate_limited(self, provider: str, retry_after: float | None, http_status: int) -> None:
        item = self._item(provider)
        item.total_requests += 1
        item.rate_limit_hits += 1
        item.last_failure_at = self._clock()
        wait = f"{retry_after:.0f}s" if retry_after is not None else "unspecified"
        item.last_error = f"HTTP {http_status} rate limited (retry after {wait})"
        self._set_status(item, ProviderStatus.RATE_LIMITED)

    def record_client_error(self, provider: str, error: str) -> None:
        """A 4xx caused by the request (bad symbol etc.). Not a provider outage."""
        item = self._item(provider)
        item.total_requests += 1
        item.last_error = error[:500]

    def note_plan_limited(self, provider: str, message: str) -> None:
        """An endpoint outside the API plan: counted as a request, never as an error."""
        item = self._item(provider)
        item.total_requests += 1
        notes = item.details.setdefault("plan_limited", [])
        if message not in notes:
            notes.append(message[:200])
            del notes[:-5]

    def set_circuit(self, provider: str, state: str) -> None:
        item = self._item(provider)
        if item.circuit_state != state:
            item.circuit_state = state
            self._dirty.add(provider)

    def set_detail(self, provider: str, key: str, value: Any) -> None:
        self._item(provider).details[key] = value

    def get(self, provider: str) -> ProviderHealth | None:
        item = self._items.get(provider)
        return copy.deepcopy(item) if item else None

    def status_of(self, provider: str) -> ProviderStatus:
        item = self._items.get(provider)
        return item.status if item else ProviderStatus.UNKNOWN

    def snapshot(self) -> list[ProviderHealth]:
        return [copy.deepcopy(item) for item in sorted(self._items.values(), key=lambda i: i.provider)]

    def pop_dirty(self) -> list[ProviderHealth]:
        dirty = [copy.deepcopy(self._items[name]) for name in sorted(self._dirty) if name in self._items]
        self._dirty.clear()
        return dirty
