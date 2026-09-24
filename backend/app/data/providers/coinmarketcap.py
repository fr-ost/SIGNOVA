"""CoinMarketCap client (Pro API with automatic keyless fallback).

With CMC_API_KEY set:
* every call goes to the authenticated Pro API (key sent only in the request header)
* the plan and live credit usage are read from /v1/key/info (no credit cost) and the
  request rate is tuned to the plan's per-minute limit
* credits are paced across the billing month (see cmc_budget.py)
* CoinMarketCap error codes are classified precisely: an endpoint outside the plan
  (1006) is remembered and not retried for a day, a rejected key (1001-1007) disables
  Pro access, and the four different 429 limits get the right back-off
* whenever Pro cannot serve a keyless-capable endpoint (budget, limit, outage, rejected
  key), the official keyless public API serves it instead; the access path is recorded
  and exposed on /api/provider-health

Without a key, only the keyless public API is used.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.core.timeutil import utcnow
from app.data.health import ProviderHealthRegistry
from app.data.http import (
    CircuitBreaker,
    ProviderAuthError,
    ProviderBadResponse,
    ProviderClientError,
    ProviderError,
    ProviderHttpClient,
    ProviderPlanLimited,
    ProviderRateLimited,
    ProviderUnavailable,
    RetryPolicy,
)
from app.data.providers.cmc_budget import CRITICAL, ENRICHMENT, NORMAL, CmcCreditBudget, parse_key_info

log = logging.getLogger(__name__)

PROVIDER = "coinmarketcap"
KEYLESS_PROVIDER = "coinmarketcap_keyless"

_AUTH_CODES = {1001, 1002, 1003, 1004, 1005, 1007}
_PLAN_LIMITED_TTL_SECONDS = 86400.0
_DEFAULT_PRO_RATE = 0.5  # requests/second until the plan's own limit is known


def _error_code(payload: Any) -> tuple[int | None, str]:
    status = payload.get("status") if isinstance(payload, dict) else None
    if not isinstance(status, dict):
        return None, ""
    raw = status.get("error_code")
    try:
        code = int(raw) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        code = None
    return code, str(status.get("error_message") or "")


def _seconds_to_next_minute(now: datetime) -> float:
    return 60.0 - now.second - now.microsecond / 1e6 + 1.0


def _seconds_to_utc_midnight(now: datetime) -> float:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return max(60.0, (tomorrow - now).total_seconds())


def classify_code(
    code: int | None, message: str, provider: str, http_status: int | None, now: datetime | None = None
) -> ProviderError | None:
    """Map a CoinMarketCap error_code to a precise exception (None = not a CMC code)."""
    if code is None or code == 0:
        return None
    now = now or utcnow()
    text = f"CMC error {code}: {message}".strip()
    if code in _AUTH_CODES:
        return ProviderAuthError(provider, text, status_code=http_status)
    if code == 1006:
        return ProviderPlanLimited(provider, text, status_code=http_status)
    if code == 1008:  # per-minute limit
        return ProviderRateLimited(provider, text, retry_after=_seconds_to_next_minute(now), status_code=429)
    if code == 1011:  # per-IP limit
        return ProviderRateLimited(provider, text, retry_after=60.0, status_code=429)
    if code == 1009:  # daily credits spent
        return ProviderRateLimited(provider, text, retry_after=_seconds_to_utc_midnight(now), status_code=429)
    if code == 1010:  # monthly credits spent; re-check daily
        return ProviderRateLimited(provider, text, retry_after=86400.0, status_code=429)
    return None


def make_classifier(provider: str, clock: Callable[[], datetime] = utcnow) -> Callable[[httpx.Response], Any]:
    def classify(response: httpx.Response) -> ProviderError | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        code, message = _error_code(payload)
        return classify_code(code, message, provider, response.status_code, clock())

    return classify


class CoinMarketCapClient:
    def __init__(
        self,
        public_base_url: str,
        pro_base_url: str,
        client: httpx.AsyncClient,
        health: ProviderHealthRegistry,
        *,
        api_key: str | None = None,
        budget: CmcCreditBudget | None = None,
        keyless_fallback: bool = True,
        retry: RetryPolicy | None = None,
        keyless_rate_per_second: float = 0.5,
        breaker_factory: Callable[[], CircuitBreaker] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = utcnow,
        **http_kwargs: Any,
    ) -> None:
        self._api_key = (api_key or "").strip() or None
        self._health = health
        self._clock = clock
        self._wall = wall_clock
        self._public_base = public_base_url.rstrip("/")
        self._pro_base = pro_base_url.rstrip("/")
        self._unsupported: dict[str, float] = {}
        self._pro_disabled: str | None = None
        self.last_access: str | None = None
        self.stats = {"pro_calls": 0, "keyless_calls": 0, "keyless_fallbacks": 0}

        def breaker() -> CircuitBreaker | None:
            return breaker_factory() if breaker_factory else None

        self._pro: ProviderHttpClient | None = None
        self._public: ProviderHttpClient | None = None
        self.budget: CmcCreditBudget | None = None
        if self._api_key:
            health.register(PROVIDER, "listing_primary")
            self.budget = budget or CmcCreditBudget()
            self._pro = ProviderHttpClient(
                PROVIDER,
                client,
                health,
                rate_per_second=_DEFAULT_PRO_RATE,
                retry=retry,
                breaker=breaker(),
                error_classifier=make_classifier(PROVIDER, wall_clock),
                clock=clock,
                **http_kwargs,
            )
        keyless_name = KEYLESS_PROVIDER if self._api_key else PROVIDER
        if not self._api_key or keyless_fallback:
            health.register(keyless_name, "listing_fallback" if self._api_key else "listing_primary")
            self._public = ProviderHttpClient(
                keyless_name,
                client,
                health,
                rate_per_second=keyless_rate_per_second,
                retry=retry,
                breaker=breaker(),
                error_classifier=make_classifier(keyless_name, wall_clock),
                clock=clock,
                **http_kwargs,
            )
        self._publish()

    # ------------------------------------------------------------------ status

    @property
    def has_key(self) -> bool:
        return self._api_key is not None

    @property
    def mode(self) -> str:
        if not self._api_key:
            return "keyless"
        return "pro_disabled" if self._pro_disabled else "pro"

    def unsupported_endpoints(self) -> list[str]:
        now = self._clock()
        return sorted(path for path, until in self._unsupported.items() if until > now)

    def _publish(self) -> None:
        info: dict[str, Any] = {
            "mode": self.mode,
            "api_key_configured": self.has_key,
            "last_access": self.last_access,
            "unsupported_endpoints": self.unsupported_endpoints(),
            **self.stats,
        }
        if self._pro_disabled:
            info["pro_disabled_reason"] = self._pro_disabled
        if self.budget is not None:
            info["budget"] = self.budget.snapshot()
        self._health.set_detail(PROVIDER, "cmc", info)

    def _disable_pro(self, reason: str) -> None:
        if self._pro_disabled is None:
            log.error("CoinMarketCap API key rejected; using keyless public API", extra={"reason": reason})
        self._pro_disabled = reason

    # ------------------------------------------------------------------ transport

    async def _request(
        self, http: ProviderHttpClient, base: str, path: str, params: dict[str, Any] | None
    ) -> tuple[Any, int]:
        headers = {"Accept": "application/json"}
        if http is self._pro and self._api_key:
            headers["X-CMC_PRO_API_KEY"] = self._api_key
        payload = await http.get_json(base + path, params=params, headers=headers)
        if not isinstance(payload, dict) or "data" not in payload:
            raise ProviderBadResponse(http.provider, "unexpected response envelope")
        code, message = _error_code(payload)
        classified = classify_code(code, message, http.provider, 200, self._wall())
        if classified is not None:
            raise classified
        if code not in (None, 0):
            raise ProviderError(http.provider, f"CMC error {code}: {message}")
        credits = 0
        status = payload.get("status")
        if isinstance(status, dict):
            try:
                credits = int(status.get("credit_count") or 0)
            except (TypeError, ValueError):
                credits = 0
        return payload["data"], credits

    async def refresh_plan(self, *, force: bool = False) -> None:
        """Read plan limits and usage from /v1/key/info (no credit cost)."""
        if self._pro is None or self._pro_disabled or self.budget is None:
            return
        if not force and not self.budget.needs_refresh():
            return
        self.budget.mark_refresh_attempt()
        try:
            data, _ = await self._request(self._pro, self._pro_base, "/v1/key/info", None)
        except ProviderAuthError as exc:
            self._disable_pro(exc.message)
        except ProviderError as exc:
            log.warning("CoinMarketCap plan check failed", extra={"error": exc.message})
        else:
            plan = parse_key_info(data, self._wall())
            self.budget.update_plan(plan)
            if plan.rate_limit_minute:
                self._pro.limiter.set_rate(max(0.2, plan.rate_limit_minute * 0.8 / 60))
            log.info(
                "CoinMarketCap plan detected",
                extra={
                    "credit_limit_monthly": plan.credit_limit_monthly,
                    "rate_limit_minute": plan.rate_limit_minute,
                    "credits_left_month": plan.month_credits_left,
                },
            )
        finally:
            self._publish()

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        cost: int = 1,
        priority: str = CRITICAL,
        keyless: bool = True,
    ) -> Any:
        """Serve `path` from Pro when possible, otherwise from the keyless API."""
        reasons: list[str] = []
        plan_limited = False
        try:
            if self._pro is not None and self.budget is not None and not self._pro_disabled:
                if self._unsupported.get(path, 0.0) > self._clock():
                    plan_limited = True
                    reasons.append("endpoint is not included in your CoinMarketCap plan")
                else:
                    await self.refresh_plan()
                    if self._pro_disabled:
                        reasons.append(f"API key rejected ({self._pro_disabled})")
                    else:
                        allowed, why = self.budget.allow(cost, priority)
                        if not allowed:
                            reasons.append(why)
                        else:
                            try:
                                data, credits = await self._request(self._pro, self._pro_base, path, params)
                            except ProviderPlanLimited as exc:
                                self._unsupported[path] = self._clock() + _PLAN_LIMITED_TTL_SECONDS
                                plan_limited = True
                                reasons.append(exc.message)
                            except ProviderAuthError as exc:
                                self._disable_pro(exc.message)
                                reasons.append(exc.message)
                            except ProviderClientError:
                                raise  # a bad request fails the same way on the keyless API
                            except ProviderError as exc:
                                reasons.append(exc.message)
                            else:
                                self.budget.record(credits)
                                self.stats["pro_calls"] += 1
                                self.last_access = "pro"
                                return data
            elif self._pro_disabled:
                reasons.append(f"API key rejected ({self._pro_disabled})")
            if self._pro is None and not keyless:
                plan_limited = True
                reasons.append("requires a CoinMarketCap API key (CMC_API_KEY)")

            if keyless and self._public is not None:
                try:
                    data, _ = await self._request(self._public, self._public_base, path, params)
                except ProviderClientError:
                    raise
                except ProviderError as exc:
                    if not reasons:
                        raise
                    raise ProviderUnavailable(
                        PROVIDER, f"pro: {'; '.join(reasons)} | keyless: {exc.message}"
                    ) from exc
                if self._pro is not None:
                    self.stats["keyless_fallbacks"] += 1
                self.stats["keyless_calls"] += 1
                self.last_access = "keyless"
                return data
        finally:
            self._publish()

        message = "; ".join(reasons) or "CoinMarketCap unavailable"
        if plan_limited:
            raise ProviderPlanLimited(PROVIDER, message)
        raise ProviderUnavailable(PROVIDER, message)

    # ------------------------------------------------------------------ endpoints

    async def listings_latest(self, limit: int = 100) -> Any:
        return await self.get(
            "/v3/cryptocurrency/listings/latest",
            {"start": 1, "limit": limit, "convert": "USD"},
            cost=max(1, math.ceil(limit / 250)),
            priority=CRITICAL,
        )

    async def quotes_latest(self, symbols: list[str]) -> Any:
        """Latest quotes for specific symbols (watchlist coins outside the listing)."""
        return await self.get(
            "/v2/cryptocurrency/quotes/latest",
            {"symbol": ",".join(sorted(set(symbols))), "convert": "USD", "skip_invalid": "true"},
            cost=max(1, math.ceil(len(symbols) / 100)),
            priority=NORMAL,
        )

    async def global_metrics(self) -> Any:
        return await self.get("/v1/global-metrics/quotes/latest", {"convert": "USD"}, priority=NORMAL)

    async def fear_and_greed_latest(self) -> Any:
        return await self.get("/v3/fear-and-greed/latest", priority=NORMAL)

    async def altcoin_season_latest(self) -> Any:
        return await self.get("/v1/altcoin-season-index/latest", priority=NORMAL)

    async def ohlcv_historical(self, cmc_id: str, time_period: str, count: int) -> Any:
        """Aggregated OHLCV history (Startup plan or higher; never available keyless).

        `count + 1` periods are requested because the newest one is still forming.
        """
        periods = count + 1
        return await self.get(
            "/v2/cryptocurrency/ohlcv/historical",
            {"id": cmc_id, "time_period": time_period, "interval": time_period, "count": periods, "convert": "USD"},
            cost=max(1, math.ceil(periods / 100)),
            priority=ENRICHMENT,
            keyless=False,
        )
