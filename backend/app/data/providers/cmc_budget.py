"""CoinMarketCap plan detection and credit pacing.

The plan is read from /v1/key/info (costs no credits). Credits are then spent through a
token bucket whose refill rate spreads the remaining monthly credits evenly until the
monthly reset, minus a safety reserve. CoinMarketCap's own daily limit is respected too.

When a Pro call is not affordable, the client uses the keyless public API instead, so
the dashboard stays fresh without ever exhausting the paid plan.

Priorities:
* critical   - data the fail-safe layer depends on (Top-20 listing, reference prices)
* normal     - market context (global metrics, Fear & Greed, Altcoin Season)
* enrichment - extra verification (historical OHLCV cross-checks); only spent when the
               bucket is comfortably full, so critical calls always keep a reserve
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.timeutil import parse_iso, utcnow
from app.data.adapters._parse import opt_int

CRITICAL = "critical"
NORMAL = "normal"
ENRICHMENT = "enrichment"

_BUCKET_SHARE = {CRITICAL: 0.0, NORMAL: 0.10, ENRICHMENT: 0.35}


@dataclass(frozen=True, slots=True)
class CmcPlanInfo:
    credit_limit_monthly: int | None
    monthly_reset_at: datetime | None
    rate_limit_minute: int | None
    day_credits_used: int | None
    day_credits_left: int | None
    month_credits_used: int | None
    month_credits_left: int | None
    fetched_at: datetime


def parse_key_info(data: Any, fetched_at: datetime | None = None) -> CmcPlanInfo:
    data = data if isinstance(data, dict) else {}
    plan = data.get("plan") or {}
    usage = data.get("usage") or {}
    day = usage.get("current_day") or {}
    month = usage.get("current_month") or {}
    return CmcPlanInfo(
        credit_limit_monthly=opt_int(plan.get("credit_limit_monthly")),
        monthly_reset_at=parse_iso(plan.get("credit_limit_monthly_reset_timestamp")),
        rate_limit_minute=opt_int(plan.get("rate_limit_minute")),
        day_credits_used=opt_int(day.get("credits_used")),
        day_credits_left=opt_int(day.get("credits_left")),
        month_credits_used=opt_int(month.get("credits_used")),
        month_credits_left=opt_int(month.get("credits_left")),
        fetched_at=fetched_at or utcnow(),
    )


def _next_month_start(now: datetime) -> datetime:
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, tzinfo=UTC)
    return datetime(now.year, now.month + 1, 1, tzinfo=UTC)


class CmcCreditBudget:
    def __init__(
        self,
        *,
        safety_factor: float = 0.9,
        reserve_pct: float = 5.0,
        min_bucket_credits: float = 25.0,
        bucket_minutes: float = 10.0,
        refresh_seconds: float = 300.0,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._safety = safety_factor
        self._reserve_pct = reserve_pct
        self._min_bucket = min_bucket_credits
        self._bucket_minutes = bucket_minutes
        self._refresh_seconds = refresh_seconds
        self._clock = clock
        self._mono = monotonic
        self._plan: CmcPlanInfo | None = None
        self._plan_mono = 0.0
        self._last_refresh_attempt = -1e18
        self._since_refresh = 0
        self._tokens = 0.0
        self._last_refill = monotonic()
        self.credits_recorded = 0
        self.denied = 0

    # -- plan -------------------------------------------------------------------------

    @property
    def plan(self) -> CmcPlanInfo | None:
        return self._plan

    def needs_refresh(self) -> bool:
        return self._mono() - self._last_refresh_attempt >= self._refresh_seconds

    def mark_refresh_attempt(self) -> None:
        self._last_refresh_attempt = self._mono()

    def update_plan(self, plan: CmcPlanInfo) -> None:
        first = self._plan is None
        self._plan = plan
        self._plan_mono = self._mono()
        self._since_refresh = 0
        if first:
            self._tokens = self.capacity()
            self._last_refill = self._mono()

    # -- pacing math ------------------------------------------------------------------

    def _reserve(self) -> float:
        limit = self._plan.credit_limit_monthly if self._plan else None
        return (limit or 0) * self._reserve_pct / 100.0

    def spendable_month(self) -> float | None:
        if self._plan is None or self._plan.month_credits_left is None:
            return None
        return max(0.0, self._plan.month_credits_left - self._since_refresh - self._reserve())

    def rate_per_second(self) -> float | None:
        """Credits per second that exactly spends the budget by the monthly reset."""
        spendable = self.spendable_month()
        if spendable is None:
            return None
        now = self._clock()
        reset = (self._plan.monthly_reset_at if self._plan else None) or _next_month_start(now)
        seconds_left = max(3600.0, (reset - now).total_seconds())
        return spendable * self._safety / seconds_left

    def capacity(self) -> float:
        rate = self.rate_per_second()
        if rate is None:
            return self._min_bucket
        return max(self._min_bucket, rate * self._bucket_minutes * 60)

    def _refill(self) -> None:
        now = self._mono()
        rate = self.rate_per_second() or 0.0
        self._tokens = min(self.capacity(), self._tokens + (now - self._last_refill) * rate)
        self._last_refill = now

    def allow(self, cost: float, priority: str = CRITICAL) -> tuple[bool, str]:
        if self._plan is None:
            return True, "plan not yet known"
        day_left = self._plan.day_credits_left
        if day_left is not None and day_left - self._since_refresh < cost:
            self.denied += 1
            return False, "CoinMarketCap daily credit limit reached"
        spendable = self.spendable_month()
        if spendable is not None and spendable < cost:
            self.denied += 1
            return False, "monthly credits down to the safety reserve"
        self._refill()
        needed = cost + self.capacity() * _BUCKET_SHARE.get(priority, 0.0)
        if self._tokens >= needed:
            return True, "within pacing"
        self.denied += 1
        return False, "credit pacing (spreading the monthly budget evenly)"

    def record(self, credits: int) -> None:
        if credits <= 0:
            return
        self.credits_recorded += credits
        self._since_refresh += credits
        self._refill()
        self._tokens = max(0.0, self._tokens - credits)

    # -- reporting ----------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        plan = self._plan
        rate = self.rate_per_second()
        return {
            "plan_known": plan is not None,
            "credit_limit_monthly": plan.credit_limit_monthly if plan else None,
            "monthly_reset_at": plan.monthly_reset_at.isoformat() if plan and plan.monthly_reset_at else None,
            "rate_limit_minute": plan.rate_limit_minute if plan else None,
            "credits_used_today": (plan.day_credits_used or 0) + self._since_refresh if plan else None,
            "credits_left_today": (plan.day_credits_left - self._since_refresh)
            if plan and plan.day_credits_left is not None
            else None,
            "credits_left_month": (plan.month_credits_left - self._since_refresh)
            if plan and plan.month_credits_left is not None
            else None,
            "reserve_credits": round(self._reserve()),
            "paced_credits_per_day": round(rate * 86400) if rate is not None else None,
            "bucket_tokens": round(self._tokens, 1) if plan else None,
            "bucket_capacity": round(self.capacity(), 1) if plan else None,
            "credits_recorded_this_process": self.credits_recorded,
            "pro_calls_deferred_by_budget": self.denied,
            "plan_checked_at": plan.fetched_at.isoformat() if plan else None,
        }
