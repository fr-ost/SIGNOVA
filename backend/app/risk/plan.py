"""Trade plan for a spot long: entry zone, stop, targets, net reward:risk and position size.

* Entry zone: from the live price down to half an ATR (4H) below it. Reward:risk is
  measured from the top of the zone, the least favourable fill.
* Stop: below the most recent 4H swing low under the entry zone (plus a 0.25 ATR buffer),
  kept between 1 and 3 ATRs from the entry. Without a swing low: 2 ATRs.
* Targets: TP1 is the nearest overhead resistance (4H and 1D swing clusters) however
  close, placed 0.1 ATR under the level; TP2 and TP3 are the next levels at least 0.5R
  further. Where no level exists, R-multiple projections are used and labelled
  (1.5R, 3R, 4.5R in price discovery; otherwise 1.5R beyond the previous target).
* Reward:risk is net of round-trip fees and slippage, measured at TP2 (the main target;
  TP1 is a partial profit at the first obstacle). The distance to the nearest resistance
  is reported separately in R, so a price pinned under a level can be rejected.
* Size: the portfolio share that risks `max_risk_per_signal_pct` if the stop is hit,
  capped at `max_allocation_pct`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.analysis.features import IndicatorSnapshot
from app.analysis.structure import StructureAnalysis
from app.core.formatting import fmt_price
from app.risk.params import RiskParams

DISCOVERY_PROJECTIONS_R = (1.5, 3.0, 4.5)
PROJECTION_STEP_R = 1.5


@dataclass(frozen=True)
class Target:
    price: float
    r_multiple: float
    basis: str
    allocation_pct: float
    projected: bool = False  # R-multiple projection rather than a resistance level


@dataclass
class TradePlan:
    quote_asset: str
    price: float
    entry_low: float
    entry_high: float
    entry_reference: float
    stop_loss: float
    stop_basis: str
    stop_distance_pct: float
    risk_per_unit: float
    targets: list[Target]
    reward_risk: float  # net, at TP2 (main target)
    reward_risk_tp1: float  # net, at TP1
    gross_reward_risk: float  # at TP2, before costs
    cost_pct: float
    nearest_resistance: float | None
    room_to_resistance_r: float | None  # nearest resistance above the entry, in R (None: none)
    suggested_allocation_pct: float
    risk_at_allocation_pct: float
    better_entry_below: float | None
    invalidation: str
    actionable: bool = False
    notes: list[str] = field(default_factory=list)


def overhead_resistances(price: float, atr: float, structures: Sequence[StructureAnalysis | None]) -> list[float]:
    """Resistance prices above `price` from several timeframes, ascending, merged within 0.25 ATR."""
    levels = sorted(lv.price for s in structures if s is not None for lv in s.resistances if lv.price > price)
    merged: list[float] = []
    for level in levels:
        if not merged or level - merged[-1] > 0.25 * atr:
            merged.append(level)
    return merged


def _net_reward_risk(target: float, entry: float, stop: float, cost_fraction: float) -> float:
    cost = entry * cost_fraction
    return (target - entry - cost) / (entry - stop + cost)


def build_plan(
    *,
    price: float,
    quote_asset: str,
    h4: IndicatorSnapshot,
    structure_4h: StructureAnalysis | None,
    resistances: Sequence[float],
    params: RiskParams,
) -> TradePlan | None:
    atr = h4.atr14
    if atr is None or atr <= 0 or price <= 0:
        return None
    notes: list[str] = []
    entry_high = price
    entry_low = price - params.entry_zone_atr * atr
    entry = entry_high

    swing = structure_4h.swing_low_below(entry_low) if structure_4h else None
    if swing is not None:
        raw_stop = swing.price - params.stop_buffer_atr * atr
        basis = f"{params.stop_buffer_atr:g} ATR below the 4H swing low {fmt_price(swing.price)}"
    else:
        raw_stop = entry - 2.0 * atr
        basis = "2 ATR below the entry (no 4H swing low under the entry zone)"
    highest, lowest = entry - params.min_stop_atr * atr, entry - params.max_stop_atr * atr
    if raw_stop > highest:
        stop = highest
        basis += f"; widened to the {params.min_stop_atr:g} ATR minimum"
    elif raw_stop < lowest:
        stop = lowest
        basis += f"; tightened to the {params.max_stop_atr:g} ATR maximum (above the swing low)"
        notes.append("stop sits above the swing low, so normal volatility can reach it")
    else:
        stop = raw_stop
    if stop <= 0:
        return None
    risk = entry - stop
    cost_fraction = params.round_trip_cost_pct / 100.0

    levels = [lv for lv in resistances if lv > entry]
    buffer = params.target_buffer_atr * atr
    targets: list[Target] = []
    floor = entry
    for index, allocation in enumerate(params.target_allocations):
        if index == 0:
            # The first target is always the nearest resistance, however close: a level just
            # above the price must lower the reward:risk, never be skipped.
            level = levels[0] if levels else None
        else:
            level = next((lv for lv in levels if lv - buffer > floor + 0.5 * risk), None)
        if level is not None:
            price_target = level - buffer if level - buffer > floor else level
            basis_t = f"under resistance {fmt_price(level)}"
        else:
            if not levels:
                projection = DISCOVERY_PROJECTIONS_R[index]
            else:
                projection = targets[-1].r_multiple + PROJECTION_STEP_R
            price_target = entry + projection * risk
            basis_t = f"{projection:.1f}R projection" + ("" if levels else " (no resistance above)")
        targets.append(
            Target(price_target, (price_target - entry) / risk, basis_t, allocation, projected=level is None)
        )
        floor = price_target

    main = targets[1].price
    net_rr = _net_reward_risk(main, entry, stop, cost_fraction)
    better = None
    if net_rr < params.min_reward_risk:
        # Entry at which the net reward:risk to TP2 reaches the minimum (stop and TP2 unchanged).
        m = params.min_reward_risk
        candidate = (main + m * stop) / ((1 + cost_fraction) * (1 + m))
        if stop < candidate < entry:
            better = candidate

    stop_pct = (entry - stop) / entry * 100.0
    loss_pct = stop_pct + params.round_trip_cost_pct
    allocation = min(params.max_risk_per_signal_pct / loss_pct * 100.0, params.max_allocation_pct)
    return TradePlan(
        quote_asset=quote_asset,
        price=price,
        entry_low=entry_low,
        entry_high=entry_high,
        entry_reference=entry,
        stop_loss=stop,
        stop_basis=basis,
        stop_distance_pct=stop_pct,
        risk_per_unit=risk,
        targets=targets,
        reward_risk=net_rr,
        reward_risk_tp1=_net_reward_risk(targets[0].price, entry, stop, cost_fraction),
        gross_reward_risk=(main - entry) / risk,
        cost_pct=params.round_trip_cost_pct,
        nearest_resistance=levels[0] if levels else None,
        room_to_resistance_r=(levels[0] - entry) / risk if levels else None,
        suggested_allocation_pct=allocation,
        risk_at_allocation_pct=allocation * loss_pct / 100.0,
        better_entry_below=better,
        invalidation=f"a 4H close below {fmt_price(stop)} invalidates the setup",
        notes=notes,
    )
