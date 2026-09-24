import random

import httpx
import pytest

from app.core.enums import ProviderStatus
from app.data.health import ProviderHealthRegistry
from app.data.http import (
    CircuitBreaker,
    ProviderClientError,
    ProviderError,
    ProviderHttpClient,
    ProviderRateLimited,
    ProviderRestricted,
    ProviderUnavailable,
    RetryPolicy,
    parse_retry_after,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(responses, clock=None, **kwargs):
    clock = clock or FakeClock()
    queue = list(responses)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    health = ProviderHealthRegistry()
    client = ProviderHttpClient(
        "test",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        health,
        rate_per_second=0,
        retry=kwargs.pop("retry", RetryPolicy(max_retries=3, base_delay=0.5, max_delay=4)),
        breaker=kwargs.pop("breaker", CircuitBreaker(3, 60, clock=clock)),
        sleep=clock.sleep,
        rng=random.Random(0),
        clock=clock,
        **kwargs,
    )
    return client, health, calls, clock


async def test_success_records_health():
    client, health, calls, _ = make_client([httpx.Response(200, json={"ok": 1})])
    assert await client.get_json("https://x/y") == {"ok": 1}
    assert health.status_of("test") == ProviderStatus.UP
    assert len(calls) == 1


async def test_retries_5xx_with_exponential_backoff_then_succeeds():
    client, health, calls, clock = make_client(
        [httpx.Response(502), httpx.Response(503), httpx.Response(200, json=[1])]
    )
    assert await client.get_json("https://x/y") == [1]
    assert len(calls) == 3
    assert len(clock.sleeps) == 2
    assert 0.25 <= clock.sleeps[0] <= 0.5 and 0.5 <= clock.sleeps[1] <= 1.0
    assert health.status_of("test") == ProviderStatus.UP


async def test_timeouts_exhaust_retries_and_raise():
    client, health, calls, _ = make_client([httpx.ConnectTimeout("boom")])
    with pytest.raises(ProviderError) as exc:
        await client.get_json("https://x/y")
    assert exc.value.retryable is True
    assert len(calls) == 4  # 1 + 3 retries
    assert health.get("test").consecutive_failures == 1


async def test_429_honours_retry_after_seconds():
    client, health, calls, clock = make_client(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})]
    )
    await client.get_json("https://x/y")
    assert clock.sleeps == [7.0]
    assert health.get("test").rate_limit_hits == 1


async def test_long_retry_after_opens_circuit_instead_of_waiting():
    client, health, calls, clock = make_client([httpx.Response(429, headers={"Retry-After": "120"})])
    with pytest.raises(ProviderRateLimited) as exc:
        await client.get_json("https://x/y")
    assert exc.value.retry_after == 120
    assert clock.sleeps == []
    assert health.status_of("test") == ProviderStatus.RATE_LIMITED
    with pytest.raises(ProviderUnavailable):
        await client.get_json("https://x/y")


async def test_418_ip_ban_opens_circuit():
    client, _, _, clock = make_client([httpx.Response(418, headers={"Retry-After": "30"})])
    with pytest.raises(ProviderRateLimited):
        await client.get_json("https://x/y")
    assert client.breaker.state == CircuitBreaker.OPEN
    clock.now += 31
    assert client.breaker.state == CircuitBreaker.HALF_OPEN


async def test_451_is_reported_as_restricted_and_not_retried():
    client, health, calls, _ = make_client([httpx.Response(451, text="restricted location")])
    with pytest.raises(ProviderRestricted):
        await client.get_json("https://x/y")
    assert len(calls) == 1
    assert health.status_of("test") == ProviderStatus.RESTRICTED


async def test_403_access_denied_is_restricted():
    client, health, calls, _ = make_client([httpx.Response(403, text="Host not in allowlist")])
    with pytest.raises(ProviderRestricted) as exc:
        await client.get_json("https://x/y")
    assert exc.value.status_code == 403 and len(calls) == 1
    assert health.status_of("test") == ProviderStatus.RESTRICTED


async def test_client_error_is_not_counted_as_outage():
    client, health, calls, _ = make_client([httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})])
    with pytest.raises(ProviderClientError):
        await client.get_json("https://x/y")
    assert len(calls) == 1
    assert health.get("test").consecutive_failures == 0
    assert client.breaker.state == CircuitBreaker.CLOSED


async def test_circuit_breaker_opens_after_threshold_and_recovers():
    clock = FakeClock()
    client, health, calls, _ = make_client(
        [httpx.Response(500)], clock=clock, retry=RetryPolicy(max_retries=0)
    )
    for _ in range(3):
        with pytest.raises(ProviderError):
            await client.get_json("https://x/y")
    with pytest.raises(ProviderUnavailable):
        await client.get_json("https://x/y")
    assert len(calls) == 3
    assert health.status_of("test") == ProviderStatus.DOWN
    clock.now += 61
    assert client.breaker.state == CircuitBreaker.HALF_OPEN


async def test_non_json_body_is_bad_response():
    client, _, _, _ = make_client([httpx.Response(200, text="<html>")], retry=RetryPolicy(max_retries=0))
    with pytest.raises(ProviderError) as exc:
        await client.get_json("https://x/y")
    assert "JSON" in exc.value.message


def test_parse_retry_after_variants():
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
