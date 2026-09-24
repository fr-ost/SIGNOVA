"""Final deterministic validation: the last check before a signal reaches the dashboard.

It re-verifies invariants that every earlier layer should already guarantee. A violation
means a bug upstream, so the signal becomes NO TRADE instead of shipping a bad plan.
"""

from __future__ import annotations

import math

from app.core.enums import SignalLabel
from app.risk.params import RiskParams
from app.risk.plan import TradePlan

_EPSILON = 1e-9


def final_validation(
    label: SignalLabel,
    plan: TradePlan | None,
    *,
    integrity_passed: bool,
    analysis_passed: bool,
    risk_blocked: bool,
    params: RiskParams,
) -> list[str]:
    if label not in (SignalLabel.BUY, SignalLabel.STRONG_BUY):
        return []
    problems: list[str] = []
    if not integrity_passed:
        problems.append("buy signal despite a failed data integrity gate")
    if not analysis_passed:
        problems.append("buy signal without a complete analysis")
    if risk_blocked:
        problems.append("buy signal despite a blocking risk check")
    if plan is None:
        problems.append("buy signal without a trade plan")
        return problems
    prices = [plan.entry_low, plan.entry_high, plan.stop_loss, *(t.price for t in plan.targets)]
    if not all(math.isfinite(v) and v > 0 for v in prices):
        problems.append("trade plan has a non-finite or non-positive price")
        return problems
    if not plan.stop_loss < plan.entry_low <= plan.entry_high:
        problems.append("stop loss must be below the entry zone")
    targets = [t.price for t in plan.targets]
    if not targets or targets[0] <= plan.entry_high or any(b <= a for a, b in zip(targets, targets[1:], strict=False)):
        problems.append("targets must rise above the entry zone")
    minimum = params.strong_min_reward_risk if label == SignalLabel.STRONG_BUY else params.min_reward_risk
    if plan.reward_risk + _EPSILON < minimum:
        problems.append(f"reward:risk {plan.reward_risk:.2f} below the {minimum:g} minimum")
    min_room = params.strong_min_room_r if label == SignalLabel.STRONG_BUY else params.min_room_r
    if plan.room_to_resistance_r is not None and plan.room_to_resistance_r + _EPSILON < min_room:
        problems.append("resistance closer than the minimum room")
    if plan.stop_distance_pct > params.max_stop_pct + _EPSILON:
        problems.append("stop distance above the limit")
    if plan.suggested_allocation_pct > params.max_allocation_pct + _EPSILON:
        problems.append("allocation above the per-signal limit")
    if plan.risk_at_allocation_pct > params.max_risk_per_signal_pct + _EPSILON:
        problems.append("risk at the suggested allocation above the per-signal limit")
    return problems
