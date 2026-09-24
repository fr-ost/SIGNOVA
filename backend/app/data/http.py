"""Resilient HTTP access for public data providers.

Features:
* per-provider rate limiting (minimum spacing plus explicit pauses)
* exponential backoff with jitter for transient failures (timeouts, 5xx)
* Retry-After handling for HTTP 429 / 418 (seconds or HTTP-date)
* circuit breaker so a failing provider is skipped instead of hammered
* explicit HTTP 451 / 403 (restricted or denied) detection
* optional provider-specific error classification (e.g. CoinMarketCap error codes)
* health reporting into ProviderHealthRegistry
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.core.enums import ProviderStatus
from app.data.health import ProviderHealthRegistry

log = logging.getLogger(__name__)


class ProviderError(Exception):
    def __init__(
        self,
        provider: str,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        body: Any = None,
    ) -> None:
        super().__init__(f"[{provider}] {message}")
        self.provider = provider
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.body = body


class ProviderUnavailable(ProviderError):
    """Circuit open, or every endpoint of the provider failed."""


class ProviderRateLimited(ProviderError):
    def __init__(
        self,
        provider: str,
        message: str,
        *,
        retry_after: float | None,
        status_code: int = 429,
        body: Any = None,
    ) -> None:
        super().__init__(provider, message, status_code=status_code, retryable=False, body=body)
        self.retry_after = retry_after


class ProviderRestricted(ProviderError):
    """HTTP 451/403/401: the provider refuses service to this server."""


class ProviderBadResponse(ProviderError):
    """Payload could not be parsed or failed shape validation."""


class ProviderClientError(ProviderError):
    """4xx caused by the request itself (unknown symbol, bad parameter)."""


class ProviderPlanLimited(ProviderClientError):
    """The endpoint is not included in the configured paid plan (not an outage)."""


class ProviderAuthError(ProviderRestricted):
    """Credentials rejected, missing, disabled or unpaid."""


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header given as delta-seconds or an HTTP-date."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    return max(0.0, (when - now).total_seconds())


@dataclass
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    max_inline_retry_after: float = 30.0
    default_ban_seconds: float = 60.0

    def backoff(self, attempt: int, rng: random.Random) -> float:
        """Exponential backoff with 'equal jitter': half fixed, half random."""
        cap = min(self.max_delay, self.base_delay * (2**attempt))
        return cap / 2 + rng.random() * cap / 2


class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._recovery = recovery_seconds
        self._clock = clock
        self._failures = 0
        self._opened_until = 0.0

    @property
    def state(self) -> str:
        if self._opened_until:
            return self.OPEN if self._clock() < self._opened_until else self.HALF_OPEN
        return self.CLOSED

    def allow(self) -> bool:
        return self.state != self.OPEN

    def remaining_open_seconds(self) -> float:
        return max(0.0, self._opened_until - self._clock()) if self._opened_until else 0.0

    def record_success(self) -> None:
        self._failures = 0
        self._opened_until = 0.0

    def record_failure(self) -> None:
        self._failures += 1
        if self.state == self.HALF_OPEN or self._failures >= self._threshold:
            self._opened_until = self._clock() + self._recovery

    def open_for(self, seconds: float) -> None:
        self._opened_until = max(self._opened_until, self._clock() + seconds)


class RateLimiter:
    """Minimum spacing between requests plus explicit pauses (e.g. near a weight limit)."""

    def __init__(
        self,
        rate_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next_allowed = 0.0
        self._paused_until = 0.0
        self._lock = asyncio.Lock()

    def set_rate(self, rate_per_second: float) -> None:
        self._interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0

    def pause_until(self, monotonic_ts: float) -> None:
        self._paused_until = max(self._paused_until, monotonic_ts)

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = max(self._next_allowed, self._paused_until) - now
            if wait > 0:
                await self._sleep(wait)
                now = self._clock()
            self._next_allowed = max(now, self._next_allowed) + self._interval


ResponseHook = Callable[[httpx.Response, "ProviderHttpClient"], None]
ErrorClassifier = Callable[[httpx.Response], "ProviderError | None"]


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _short_body(response: httpx.Response, limit: int = 200) -> str:
    try:
        return response.text[:limit].replace("\n", " ")
    except Exception:  # pragma: no cover - defensive
        return "<unreadable body>"


class _Retry(Exception):
    """Internal signal: wait `delay` seconds and try the request again."""

    def __init__(self, delay: float) -> None:
        self.delay = delay


class ProviderHttpClient:
    """JSON GET client bound to one provider (and one base URL).

    An optional `error_classifier` maps provider-specific error bodies (for example
    CoinMarketCap's error_code values) to precise exception types before the generic
    HTTP-status handling runs.
    """

    def __init__(
        self,
        provider: str,
        client: httpx.AsyncClient,
        health: ProviderHealthRegistry,
        *,
        rate_per_second: float = 5.0,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        response_hook: ResponseHook | None = None,
        error_classifier: ErrorClassifier | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider = provider
        self._client = client
        self._health = health
        self._retry = retry or RetryPolicy()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.breaker = breaker or CircuitBreaker(clock=clock)
        self.limiter = RateLimiter(rate_per_second, clock=clock, sleep=sleep)
        self._response_hook = response_hook
        self._error_classifier = error_classifier

    # -- error handling ------------------------------------------------------------------

    def _rate_limited(self, error: ProviderRateLimited, attempt: int, http_status: int) -> None:
        """Retry short waits inline (raises _Retry); otherwise open the circuit and raise."""
        wait = error.retry_after
        self._health.record_rate_limited(self.provider, wait, http_status)
        can_wait = wait is None or wait <= self._retry.max_inline_retry_after
        if http_status != 418 and can_wait and attempt < self._retry.max_retries:
            raise _Retry(wait if wait is not None else self._retry.backoff(attempt, self._rng))
        ban = wait if wait is not None else self._retry.default_ban_seconds
        self.breaker.open_for(ban)
        self.limiter.pause_until(self._clock() + ban)
        self._health.set_circuit(self.provider, self.breaker.state)
        log.warning(
            "provider rate limited",
            extra={"provider": self.provider, "http_status": http_status, "ban_seconds": round(ban, 1)},
        )
        error.retry_after = ban
        raise error

    def _handle_error_response(self, response: httpx.Response, attempt: int) -> ProviderError:
        """Raise for final errors, raise _Retry for waits, or return a retryable error."""
        status = response.status_code
        classified = self._error_classifier(response) if self._error_classifier else None

        if isinstance(classified, ProviderRateLimited):
            self._rate_limited(classified, attempt, status)
        if isinstance(classified, ProviderRestricted):
            self._health.record_failure(self.provider, classified.message, status=ProviderStatus.RESTRICTED)
            raise classified
        if isinstance(classified, ProviderClientError):
            self._health.record_client_error(self.provider, classified.message)
            raise classified
        if classified is not None:
            return classified

        if status in (429, 418):
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            self._rate_limited(
                ProviderRateLimited(
                    self.provider,
                    f"HTTP {status}: rate limited",
                    retry_after=retry_after,
                    status_code=status,
                    body=_json_or_none(response),
                ),
                attempt,
                status,
            )
        if status in (401, 403, 451):
            # 451: geo-restricted location. 401/403: access denied (WAF, CDN geo block,
            # blocked egress, rejected credentials). The provider is unusable from here.
            reason = (
                "HTTP 451: service unavailable from this server's location"
                if status == 451
                else f"HTTP {status}: access denied ({_short_body(response, 120)})"
            )
            self._health.record_failure(self.provider, reason, status=ProviderStatus.RESTRICTED)
            raise ProviderRestricted(self.provider, reason, status_code=status, body=_json_or_none(response))
        if status >= 500 or status == 408:
            return ProviderError(self.provider, f"HTTP {status}", status_code=status, retryable=True)
        detail = _short_body(response)
        self._health.record_client_error(self.provider, f"HTTP {status}: {detail}")
        raise ProviderClientError(
            self.provider, f"HTTP {status}: {detail}", status_code=status, body=_json_or_none(response)
        )

    # -- request -------------------------------------------------------------------------

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if not self.breaker.allow():
            wait = self.breaker.remaining_open_seconds()
            raise ProviderUnavailable(self.provider, f"circuit open; retry in {wait:.0f}s")

        attempt = 0
        while True:
            await self.limiter.acquire()
            started = self._clock()
            error: ProviderError
            try:
                response = await self._client.get(url, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                error = ProviderError(self.provider, f"timeout ({type(exc).__name__})", retryable=True)
            except httpx.TransportError as exc:
                error = ProviderError(self.provider, f"transport error: {exc}"[:300], retryable=True)
            else:
                latency_ms = (self._clock() - started) * 1000
                if self._response_hook is not None:
                    self._response_hook(response, self)
                if 200 <= response.status_code < 300:
                    try:
                        data = response.json()
                    except ValueError:
                        error = ProviderBadResponse(
                            self.provider,
                            "response body is not valid JSON",
                            status_code=response.status_code,
                            retryable=True,
                        )
                    else:
                        self.breaker.record_success()
                        self._health.record_success(self.provider, latency_ms)
                        self._health.set_circuit(self.provider, self.breaker.state)
                        return data
                else:
                    try:
                        error = self._handle_error_response(response, attempt)
                    except _Retry as retry:
                        attempt += 1
                        await self._sleep(retry.delay)
                        continue

            if error.retryable and attempt < self._retry.max_retries:
                delay = self._retry.backoff(attempt, self._rng)
                attempt += 1
                await self._sleep(delay)
                continue

            self.breaker.record_failure()
            self._health.record_failure(self.provider, error.message)
            self._health.set_circuit(self.provider, self.breaker.state)
            log.warning(
                "provider request failed",
                extra={"provider": self.provider, "error": error.message, "attempts": attempt + 1},
            )
            raise error
