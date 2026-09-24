"""CoinMarketCap Pro integration: error codes, plan detection, credit pacing, keyless fallback."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.enums import CrossCheckStatus, DataState, ProviderStatus, Timeframe
from app.core.timeutil import floor_to_timeframe, utcnow
from app.data.adapters.listings import (
    CoinMarketCapAdapter,
    parse_cmc_altcoin_season,
    parse_cmc_listings,
    parse_cmc_ohlcv,
)
from app.data.health import ProviderHealthRegistry
from app.data.http import (
    ProviderAuthError,
    ProviderBadResponse,
    ProviderPlanLimited,
    ProviderRateLimited,
    ProviderUnavailable,
    RetryPolicy,
)
from app.data.providers.cmc_budget import CRITICAL, ENRICHMENT, CmcCreditBudget, CmcPlanInfo, parse_key_info
from app.data.providers.coinmarketcap import KEYLESS_PROVIDER, PROVIDER, CoinMarketCapClient, classify_code
from app.data.validation.candle_reference import cross_validate_candles
from tests.conftest import make_candles

KEY = "cmc-test-key-0123456789abcdef"
PRO = "https://pro.test"
PUBLIC = "https://pro.test/public-api"


async def _no_sleep(_: float) -> None:
    return None


def envelope(data, credits=1, code=0, message=""):
    return {"status": {"error_code": code, "error_message": message, "credit_count": credits}, "data": data}


def key_info(month_left=140_000, day_left=5_000, limit=150_000, rate=300):
    reset = (utcnow() + timedelta(days=10)).isoformat().replace("+00:00", "Z")
    return {
        "plan": {"credit_limit_monthly": limit, "credit_limit_monthly_reset_timestamp": reset, "rate_limit_minute": rate},
        "usage": {
            "current_minute": {"requests_made": 1, "requests_left": rate - 1},
            "current_day": {"credits_used": 10, "credits_left": day_left},
            "current_month": {"credits_used": limit - month_left, "credits_left": month_left},
        },
    }


def v3_listing():
    now = utcnow().isoformat().replace("+00:00", "Z")
    return [
        {"id": 1, "name": "Bitcoin", "symbol": "BTC", "slug": "bitcoin", "cmc_rank": 1, "tags": ["mineable"],
         "quote": [{"id": 2781, "symbol": "USD", "price": 63120.9, "market_cap": 1.26e12, "volume_24h": 2.9e10,
                    "percent_change_24h": -1.2, "last_updated": now}]},
        {"id": 825, "name": "Tether USDt", "symbol": "USDT", "slug": "tether", "cmc_rank": 3, "tags": ["stablecoin"],
         "quote": [{"id": 2781, "symbol": "USD", "price": 0.9990, "market_cap": 1.86e11, "last_updated": now}]},
    ]


class Recorder:
    """MockTransport handler that records requests and serves scripted responses per path."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for prefix, responder in self.routes.items():
            if path.endswith(prefix):
                return responder(request) if callable(responder) else responder
        return httpx.Response(404, json={"status": {"error_code": 404, "error_message": "no route"}})

    def paths(self):
        return [r.url.path for r in self.requests]


def make_client(routes, *, api_key=KEY, budget=None, keyless_fallback=True):
    rec = Recorder(routes)
    health = ProviderHealthRegistry()
    client = CoinMarketCapClient(
        PUBLIC,
        PRO,
        httpx.AsyncClient(transport=httpx.MockTransport(rec)),
        health,
        api_key=api_key,
        budget=budget,
        keyless_fallback=keyless_fallback,
        retry=RetryPolicy(max_retries=0),
        keyless_rate_per_second=0,
        sleep=_no_sleep,
    )
    client._pro and client._pro.limiter.set_rate(0)
    return client, rec, health


# ------------------------------------------------------------------ error codes


@pytest.mark.parametrize(
    ("code", "exc_type"),
    [(1001, ProviderAuthError), (1002, ProviderAuthError), (1003, ProviderAuthError), (1004, ProviderAuthError),
     (1005, ProviderAuthError), (1007, ProviderAuthError), (1006, ProviderPlanLimited),
     (1008, ProviderRateLimited), (1009, ProviderRateLimited), (1010, ProviderRateLimited), (1011, ProviderRateLimited)],
)
def test_classify_every_documented_error_code(code, exc_type):
    assert isinstance(classify_code(code, "msg", PROVIDER, 400), exc_type)


def test_rate_limit_waits_match_the_limit_type():
    now = datetime(2026, 9, 24, 10, 15, 30, tzinfo=UTC)
    assert 30 <= classify_code(1008, "", PROVIDER, 429, now).retry_after <= 31  # next minute
    daily = classify_code(1009, "", PROVIDER, 429, now).retry_after
    assert timedelta(seconds=daily) == datetime(2026, 9, 25, 0, 0, 5, tzinfo=UTC) - now  # UTC midnight
    assert classify_code(1010, "", PROVIDER, 429, now).retry_after == 86400
    assert classify_code(0, "", PROVIDER, 200) is None and classify_code(None, "", PROVIDER, 500) is None


# ------------------------------------------------------------------ budget


def plan(month_left=100_000, limit=150_000, day_left=None, reset_days=10.0):
    now = utcnow()
    return CmcPlanInfo(limit, now + timedelta(days=reset_days), 300, 0, day_left, limit - month_left, month_left, now)


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_budget_unknown_plan_allows_and_key_info_parses():
    b = CmcCreditBudget()
    assert b.allow(1)[0] is True
    info = parse_key_info(key_info())
    assert info.credit_limit_monthly == 150_000 and info.rate_limit_minute == 300
    assert info.month_credits_left == 140_000 and info.day_credits_left == 5_000
    assert info.monthly_reset_at is not None


def test_budget_paces_the_month_evenly():
    mono = Mono()
    b = CmcCreditBudget(monotonic=mono, safety_factor=0.9, reserve_pct=5.0)
    b.update_plan(plan(month_left=100_000, limit=150_000, reset_days=10))
    # (100k - 7.5k reserve) * 0.9 spread over 10 days
    assert b.snapshot()["paced_credits_per_day"] == pytest.approx(8325, rel=0.01)
    assert b.allow(1)[0]
    b.record(int(b.capacity()))  # drain the bucket
    allowed, why = b.allow(1)
    assert not allowed and "pacing" in why
    mono.t += 600  # ten minutes later the bucket has refilled
    assert b.allow(1)[0]


def test_budget_keeps_headroom_for_critical_calls():
    mono = Mono()
    b = CmcCreditBudget(monotonic=mono)
    b.update_plan(plan())
    b.record(int(b.capacity() * 0.8))  # ~20% left
    assert b.allow(1, CRITICAL)[0] is True
    assert b.allow(1, ENRICHMENT)[0] is False


def test_budget_respects_daily_cap_and_monthly_reserve():
    b = CmcCreditBudget()
    b.update_plan(plan(day_left=3))
    b.record(3)
    allowed, why = b.allow(1)
    assert not allowed and "daily" in why
    reserve_only = CmcCreditBudget()
    reserve_only.update_plan(plan(month_left=7_000, limit=150_000))  # reserve is 7,500
    allowed, why = reserve_only.allow(1)
    assert not allowed and "reserve" in why


# ------------------------------------------------------------------ client: Pro path


async def test_pro_listing_detects_plan_sends_key_only_to_pro_and_records_credits():
    client, rec, health = make_client({
        "/v1/key/info": httpx.Response(200, json=envelope(key_info(), credits=0)),
        "/v3/cryptocurrency/listings/latest": httpx.Response(200, json=envelope(v3_listing(), credits=1)),
    })
    entries = parse_cmc_listings(await client.listings_latest(100))
    assert [e.symbol for e in entries] == ["BTC", "USDT"] and entries[0].price_usd == 63120.9
    assert rec.paths() == ["/v1/key/info", "/v3/cryptocurrency/listings/latest"]
    assert all(r.headers["X-CMC_PRO_API_KEY"] == KEY for r in rec.requests)
    assert client.last_access == "pro" and client.mode == "pro"
    detail = health.get(PROVIDER).details["cmc"]
    assert detail["budget"]["credit_limit_monthly"] == 150_000 and detail["pro_calls"] == 1
    assert detail["budget"]["credits_recorded_this_process"] == 1
    assert KEY not in json.dumps(health.get(PROVIDER).details)


async def test_plan_rate_limit_is_applied_to_pro_requests():
    client, _, _ = make_client({"/v1/key/info": httpx.Response(200, json=envelope(key_info(rate=120)))})
    await client.refresh_plan(force=True)
    assert client._pro.limiter._interval == pytest.approx(1 / (120 * 0.8 / 60))


async def test_budget_denial_uses_keyless_without_the_key():
    budget = CmcCreditBudget()
    budget.update_plan(plan(month_left=7_000, limit=150_000))  # only the reserve is left
    budget.mark_refresh_attempt()
    client, rec, _ = make_client(
        {"/v3/cryptocurrency/listings/latest": httpx.Response(200, json=envelope(v3_listing()))}, budget=budget
    )
    await client.listings_latest(100)
    assert rec.paths() == ["/public-api/v3/cryptocurrency/listings/latest"]
    assert "X-CMC_PRO_API_KEY" not in rec.requests[0].headers
    assert client.last_access == "keyless" and client.stats["keyless_fallbacks"] == 1


async def test_rejected_key_disables_pro_and_falls_back_to_keyless():
    def route(request):
        if request.url.path.startswith("/public-api"):
            return httpx.Response(200, json=envelope(v3_listing()))
        return httpx.Response(401, json=envelope(None, code=1001, message="This API Key is invalid."))

    client, rec, health = make_client({"/v1/key/info": route, "/v3/cryptocurrency/listings/latest": route})
    await client.listings_latest(100)
    assert client.mode == "pro_disabled" and client.last_access == "keyless"
    assert health.status_of(PROVIDER) == ProviderStatus.RESTRICTED
    assert "1001" in health.get(PROVIDER).details["cmc"]["pro_disabled_reason"]
    before = len(rec.requests)
    await client.listings_latest(100)  # no further Pro attempts
    assert all(r.url.path.startswith("/public-api") for r in rec.requests[before:])


async def test_endpoint_outside_plan_is_remembered_and_not_retried():
    client, rec, health = make_client({
        "/v1/key/info": httpx.Response(200, json=envelope(key_info(), credits=0)),
        "/v2/cryptocurrency/ohlcv/historical": httpx.Response(
            403, json=envelope(None, code=1006, message="Your API Key subscription plan doesn't support this endpoint.")
        ),
    })
    with pytest.raises(ProviderPlanLimited):
        await client.ohlcv_historical("1", "daily", 30)
    calls = len(rec.requests)
    with pytest.raises(ProviderPlanLimited):
        await client.ohlcv_historical("1", "daily", 30)
    assert len(rec.requests) == calls  # remembered: no second HTTP request
    assert health.status_of(PROVIDER) != ProviderStatus.RESTRICTED  # not an outage
    assert "/v2/cryptocurrency/ohlcv/historical" in client.unsupported_endpoints()


async def test_daily_limit_on_pro_falls_back_to_keyless_and_opens_pro_circuit():
    def route(request):
        if request.url.path.startswith("/public-api"):
            return httpx.Response(200, json=envelope(v3_listing()))
        return httpx.Response(429, json=envelope(None, code=1009, message="daily limit"))

    client, rec, health = make_client({
        "/v1/key/info": httpx.Response(200, json=envelope(key_info(), credits=0)),
        "/v3/cryptocurrency/listings/latest": route,
    })
    await client.listings_latest(100)
    assert client.last_access == "keyless"
    assert health.status_of(PROVIDER) == ProviderStatus.RATE_LIMITED
    pro_calls = sum(1 for r in rec.requests if r.url.path == "/v3/cryptocurrency/listings/latest")
    await client.listings_latest(100)  # circuit open: Pro skipped without an HTTP call
    assert sum(1 for r in rec.requests if r.url.path == "/v3/cryptocurrency/listings/latest") == pro_calls


async def test_pro_only_endpoint_without_key_raises_plan_limited_without_http():
    client, rec, _ = make_client({}, api_key=None)
    with pytest.raises(ProviderPlanLimited) as exc:
        await client.ohlcv_historical("1", "daily", 30)
    assert "CMC_API_KEY" in exc.value.message and rec.requests == []


async def test_keyless_only_mode_when_no_key():
    client, rec, health = make_client(
        {"/v3/cryptocurrency/listings/latest": httpx.Response(200, json=envelope(v3_listing()))}, api_key=None
    )
    await client.listings_latest(100)
    assert rec.paths() == ["/public-api/v3/cryptocurrency/listings/latest"]
    assert client.mode == "keyless" and health.get(PROVIDER) is not None
    assert health.get(KEYLESS_PROVIDER) is None


async def test_http_200_with_error_code_is_rejected():
    client, _, _ = make_client(
        {"/v3/cryptocurrency/listings/latest": httpx.Response(200, json=envelope(None, code="1008", message="slow"))},
        api_key=None,
    )
    with pytest.raises(ProviderRateLimited):
        await client.listings_latest(100)


async def test_no_keyless_fallback_when_disabled():
    budget = CmcCreditBudget()
    budget.update_plan(plan(month_left=1, limit=150_000))
    budget.mark_refresh_attempt()
    client, rec, _ = make_client({}, budget=budget, keyless_fallback=False)
    with pytest.raises(ProviderUnavailable):
        await client.listings_latest(100)
    assert rec.requests == []


# ------------------------------------------------------------------ parsers


def test_parse_altcoin_season():
    alt = parse_cmc_altcoin_season({"altcoin_index": 38, "snapshot_time": "2026-09-24T00:00:00Z",
                                    "yearly_high": 87, "yearly_low": 12})
    assert alt.value == 38 and alt.yearly_high == 87
    with pytest.raises(ProviderBadResponse):
        parse_cmc_altcoin_season({"altcoin_index": 140})


def _ohlcv_rows(tf, count, now):
    start = floor_to_timeframe(now, tf) - timedelta(seconds=tf.seconds * (count - 1))
    rows = []
    for i in range(count):
        t = start + timedelta(seconds=tf.seconds * i)
        rows.append({"time_open": t.isoformat().replace("+00:00", "Z"),
                     "quote": {"USD": {"open": 100, "high": 102, "low": 99, "close": 101 + i, "volume": 5}}})
    return rows


def test_parse_ohlcv_drops_forming_period_and_handles_keyed_responses():
    now = utcnow()
    rows = _ohlcv_rows(Timeframe.D1, 5, now)  # last row is today's forming day
    single = parse_cmc_ohlcv({"id": 1, "symbol": "BTC", "quotes": rows}, "1", Timeframe.D1, now)
    assert len(single) == 4 and single[-1].close_time <= now
    keyed = parse_cmc_ohlcv({"1": {"id": 1, "symbol": "BTC", "quotes": rows}}, "1", Timeframe.D1, now)
    assert [c.close for c in keyed] == [c.close for c in single]
    assert parse_cmc_ohlcv({}, "1", Timeframe.D1, now) == []


async def test_adapter_reference_candles_uses_hourly_and_daily_periods():
    seen = []

    def route(request):
        seen.append(dict(request.url.params))
        tf = Timeframe.H1 if request.url.params["time_period"] == "hourly" else Timeframe.D1
        return httpx.Response(200, json=envelope({"id": 1, "symbol": "BTC", "quotes": _ohlcv_rows(tf, 5, utcnow())}))

    client, _, _ = make_client({
        "/v1/key/info": httpx.Response(200, json=envelope(key_info(), credits=0)),
        "/v2/cryptocurrency/ohlcv/historical": route,
    })
    adapter = CoinMarketCapAdapter(client)
    hourly = await adapter.reference_candles("1", Timeframe.H1, 4)
    daily = await adapter.reference_candles("1", Timeframe.D1, 4)
    assert len(hourly) == 4 and len(daily) == 4
    assert seen[0]["time_period"] == "hourly" and seen[0]["count"] == "5" and seen[0]["id"] == "1"
    with pytest.raises(ValueError):
        await adapter.reference_candles("1", Timeframe.M5, 4)


# ------------------------------------------------------------------ candle cross-validation


def _pair(scale=1.0, count=60):
    now = utcnow()
    exchange = [c for c in make_candles(Timeframe.H1, count, now=now, end_price=100) if c.is_closed]
    from dataclasses import replace

    reference = [replace(c, close=c.close * scale) for c in exchange]
    return exchange, reference


def test_candle_history_consistent_warning_conflict_unverified():
    ex, ref = _pair(1.001)
    assert cross_validate_candles(ex, ref, Timeframe.H1, usd_rate=1.0, reference_source="cmc").status == CrossCheckStatus.CONSISTENT
    ex, ref = _pair(1.015)
    assert cross_validate_candles(ex, ref, Timeframe.H1, usd_rate=1.0, reference_source="cmc").status == CrossCheckStatus.WARNING
    ex, ref = _pair(1.05)
    conflict = cross_validate_candles(ex, ref, Timeframe.H1, usd_rate=1.0, reference_source="cmc")
    assert conflict.status == CrossCheckStatus.CONFLICT and conflict.outliers == conflict.compared
    ex, ref = _pair(1.0)
    assert cross_validate_candles(ex, ref[:5], Timeframe.H1, usd_rate=1.0, reference_source="cmc").status == CrossCheckStatus.UNVERIFIED
    assert cross_validate_candles(ex, ref, Timeframe.H1, usd_rate=None, reference_source="cmc").status == CrossCheckStatus.UNVERIFIED


def test_usdt_conversion_is_applied_before_comparing():
    ex, ref = _pair(0.98)  # reference in USD, exchange quoted in USDT worth $0.98
    assert cross_validate_candles(ex, ref, Timeframe.H1, usd_rate=0.98, reference_source="cmc").status == CrossCheckStatus.CONSISTENT


def test_single_bad_candle_is_tolerated_but_many_are_not():
    from dataclasses import replace

    ex, ref = _pair(1.0)
    ref[10] = replace(ref[10], close=ref[10].close * 1.2)
    one = cross_validate_candles(ex, ref, Timeframe.H1, usd_rate=1.0, reference_source="cmc")
    assert one.status == CrossCheckStatus.WARNING and one.outliers == 1
    for i in range(10, 30):
        ref[i] = replace(ref[i], close=ex[i].close * 1.2)
    many = cross_validate_candles(ex, ref, Timeframe.H1, usd_rate=1.0, reference_source="cmc")
    assert many.status == CrossCheckStatus.CONFLICT


async def test_container_with_key_uses_pro_and_warmup_detects_plan(test_settings, caplog):
    import logging

    from pydantic import SecretStr

    from app.services.container import build_container
    from tests.conftest import FakeSpotAdapter

    settings = test_settings.model_copy(update={"cmc_api_key": SecretStr(KEY)})
    rec = Recorder({"/v1/key/info": httpx.Response(200, json=envelope(key_info(), credits=0))})
    http = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    c = build_container(settings, http_client=http, use_database=False, spot_adapters=[FakeSpotAdapter()])
    with caplog.at_level(logging.INFO):
        await c.warmup()
    assert c.cmc is not None and c.cmc.mode == "pro"
    assert rec.requests[0].url.host == "pro-api.coinmarketcap.com"
    assert rec.requests[0].url.path == "/v1/key/info"
    budget = c.health.get(PROVIDER).details["cmc"]["budget"]
    assert budget["credit_limit_monthly"] == 150_000 and budget["rate_limit_minute"] == 300
    assert KEY not in caplog.text
    await c.aclose()
    await http.aclose()


async def test_container_without_key_is_keyless(test_settings):
    from app.services.container import build_container
    from tests.conftest import FakeSpotAdapter

    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    c = build_container(test_settings, http_client=http, use_database=False, spot_adapters=[FakeSpotAdapter()])
    await c.warmup()  # no key: nothing to verify, no request made
    assert c.cmc is not None and c.cmc.mode == "keyless"
    await c.aclose()
    await http.aclose()


async def test_both_paths_failing_reports_both_reasons():
    def route(request):
        if request.url.path.startswith("/public-api"):
            return httpx.Response(503)
        return httpx.Response(500)

    client, _, _ = make_client({
        "/v1/key/info": httpx.Response(200, json=envelope(key_info(), credits=0)),
        "/v3/cryptocurrency/listings/latest": route,
    })
    with pytest.raises(ProviderUnavailable) as exc:
        await client.listings_latest(100)
    assert "pro: HTTP 500" in exc.value.message and "keyless: HTTP 503" in exc.value.message
