"""Trade simulator for spot longs, shared by the backtest and the live track record.

Given an entry, a stop and targets (each closing a share of the position), it walks the
candles after the entry and reports what a trader following the plan would have got, net
of round-trip costs. Candles only tell us the high and low, not their order, so the
simulation is deliberately pessimistic:

* if a candle reaches both the stop and a target, the stop counts (loss first);
* after the first target the stop moves to the entry (break-even), effective from the next
  candle, so a candle cannot both pay a target and stop out the rest at break-even;
* a trade still open after `max_hold` candles is closed at that candle's close (time exit);
* a trade that runs out of candles before any of this is reported as OPEN.

Managed exits (the strategy library): a trailing stop is computed from a closed candle and
applies from the next one, and only ever rises; an exit signal (for example "close above the
5-period average") closes the rest at that candle's close. Both use closed candles only.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.data.normalization.schemas import Candle

STOP = "STOP"  # full loss at the stop
BREAKEVEN = "BREAKEVEN"  # first target paid, rest closed at the entry
TARGETS = "TARGETS"  # every target reached
TIME = "TIME"  # closed at the time limit
TRAIL = "TRAIL"  # the trailing stop closed the (rest of the) position
EXIT = "EXIT"  # the strategy's exit signal closed the (rest of the) position
OPEN = "OPEN"  # not finished within the available candles


@dataclass(frozen=True)
class SimTarget:
    price: float
    share: float  # fraction of the position closed at this target (shares sum to 1)


@dataclass
class SimResult:
    outcome: str
    exit_index: int | None  # index in `candles` of the candle where the trade finished
    exit_time: datetime | None
    exit_price: float | None  # last exit price
    r_multiple: float  # net of costs, in units of the initial risk
    return_pct: float  # net of costs, on the whole position
    hit_targets: list[int] = field(default_factory=list)  # 1-based target numbers reached
    mfe_pct: float = 0.0  # best unrealised move, percent
    mae_pct: float = 0.0  # worst unrealised move, percent
    held: int = 0  # candles held

    @property
    def win(self) -> bool:
        return self.r_multiple > 0


def simulate_long(
    candles: Sequence[Candle],
    start: int,
    *,
    entry: float,
    stop: float,
    targets: Sequence[SimTarget],
    cost_pct: float,
    max_hold: int | None = None,
    breakeven_after_first: bool = True,
    trail: Callable[[int], float | None] | None = None,
    exit_signal: Callable[[int], bool] | None = None,
) -> SimResult:
    """Simulate a long entered at `entry` before candle `start` (candles[start] is the first
    candle after the entry). `trail(k)` gives a stop level from closed candle k (applied from
    k + 1, never lowered); `exit_signal(k)` closes the rest at candle k's close."""
    if not (0 < stop < entry) or (not targets and trail is None and exit_signal is None):
        raise ValueError("a long needs 0 < stop < entry and a target, a trailing stop or an exit signal")
    risk_pct = (entry - stop) / entry * 100.0
    remaining = 1.0
    realized = 0.0  # sum of share * price change, as a fraction
    current_stop = stop
    pending_stop: float | None = None
    next_target = 0
    hit: list[int] = []
    mfe = mae = 0.0
    held = 0
    exit_index: int | None = None
    exit_price: float | None = None
    outcome = OPEN
    trailed = False

    k = start
    while k < len(candles):
        if max_hold is not None and held >= max_hold:
            break
        c = candles[k]
        held += 1
        if pending_stop is not None:
            current_stop, pending_stop = pending_stop, None
        mfe = max(mfe, (c.high / entry - 1.0) * 100.0)
        mae = min(mae, (c.low / entry - 1.0) * 100.0)
        if c.low <= current_stop:
            fill = min(current_stop, c.open)  # a gap below the stop fills at the open
            realized += remaining * (fill / entry - 1.0)
            remaining = 0.0
            exit_index, exit_price = k, fill
            if trailed and current_stop > stop:
                outcome = TRAIL
            else:
                outcome = STOP if not hit else (TARGETS if next_target >= len(targets) else BREAKEVEN)
            break
        while next_target < len(targets) and c.high >= targets[next_target].price:
            t = targets[next_target]
            share = min(t.share, remaining)
            realized += share * (t.price / entry - 1.0)
            remaining -= share
            next_target += 1
            hit.append(next_target)
            if next_target == 1 and breakeven_after_first:
                pending_stop = max(current_stop, entry)
        managed = trail is not None or exit_signal is not None
        if targets and (remaining <= 1e-9 or (next_target >= len(targets) and not managed)):
            if remaining > 1e-9:  # shares did not sum to 1: close the rest at the last target
                realized += remaining * (targets[-1].price / entry - 1.0)
                remaining = 0.0
            exit_index, exit_price = k, targets[next_target - 1].price
            outcome = TARGETS
            break
        if exit_signal is not None and exit_signal(k):
            realized += remaining * (c.close / entry - 1.0)
            remaining = 0.0
            exit_index, exit_price = k, c.close
            outcome = EXIT
            break
        if trail is not None:
            level = trail(k)
            if level is not None and math.isfinite(level) and level > max(current_stop, pending_stop or 0.0) and level < c.close:
                pending_stop = level
                trailed = True
        k += 1

    if outcome == OPEN and max_hold is not None and held >= max_hold and held > 0:
        last = candles[start + held - 1]
        realized += remaining * (last.close / entry - 1.0)
        remaining = 0.0
        exit_index, exit_price = start + held - 1, last.close
        outcome = TIME

    if outcome == OPEN:
        # mark to market at the last available close (not a finished trade)
        last_close = candles[-1].close if start < len(candles) else entry
        realized += remaining * (last_close / entry - 1.0)
    return_pct = realized * 100.0 - cost_pct
    return SimResult(
        outcome=outcome,
        exit_index=exit_index,
        exit_time=candles[exit_index].close_time if exit_index is not None else None,
        exit_price=exit_price,
        r_multiple=return_pct / risk_pct,
        return_pct=return_pct,
        hit_targets=hit,
        mfe_pct=mfe,
        mae_pct=mae,
        held=held,
    )
