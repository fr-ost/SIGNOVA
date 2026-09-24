"""Extreme-volatility detection on 5-minute candles (fail-safe, not a trading signal)."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from app.data.normalization.schemas import Candle


def realized_volatility(returns: Sequence[float]) -> float:
    """Root-mean-square of log returns (measured around zero, not the mean).

    Standard deviation would under-report a steady one-directional crash, because
    consistent -1% candles have low dispersion. Realized volatility does not.
    """
    return math.sqrt(sum(r * r for r in returns) / len(returns)) if returns else 0.0


@dataclass(frozen=True, slots=True)
class VolatilityCheck:
    available: bool
    extreme: bool
    ratio: float | None
    recent_vol_pct: float | None
    baseline_vol_pct: float | None
    move_pct: float | None
    reason: str


def check_volatility(
    closed_candles: Sequence[Candle],
    *,
    recent: int = 12,
    baseline_candles: int = 288,
    ratio_threshold: float = 4.0,
    min_move_pct: float = 3.0,
    absolute_move_pct: float = 8.0,
) -> VolatilityCheck:
    """Compare realized volatility of the last `recent` candles with the typical level.

    Extreme when either:
    * volatility is far above normal *and* price actually moved (relative rule), or
    * price moved more than `absolute_move_pct` within the window (absolute rule),
      which catches violent moves in assets that are already volatile.
    """
    needed = recent * 5 + 1
    if len(closed_candles) < needed:
        return VolatilityCheck(False, False, None, None, None, None, f"needs {needed} closed candles")
    window = list(closed_candles[-(baseline_candles + recent + 1) :])
    returns = [math.log(b.close / a.close) for a, b in zip(window, window[1:], strict=False)]
    recent_returns = returns[-recent:]
    history = returns[:-recent]
    blocks = [history[i : i + recent] for i in range(0, len(history) - recent + 1, recent)]
    block_vols = [realized_volatility(block) for block in blocks if len(block) == recent]
    if len(block_vols) < 4:
        return VolatilityCheck(False, False, None, None, None, None, "insufficient baseline history")
    baseline = statistics.median(block_vols)
    current = realized_volatility(recent_returns)
    ratio = current / baseline if baseline > 0 else (math.inf if current > 0 else 1.0)
    move = abs(window[-1].close / window[-recent - 1].close - 1.0) * 100.0
    relative = ratio >= ratio_threshold and move >= min_move_pct
    absolute = move >= absolute_move_pct
    extreme = relative or absolute
    if absolute and not relative:
        reason = f"{move:.2f}% move in {recent} candles (limit {absolute_move_pct}%)"
    elif extreme:
        reason = f"volatility {ratio:.1f}x normal with a {move:.2f}% move"
    else:
        reason = f"volatility {ratio:.1f}x normal, {move:.2f}% move"
    return VolatilityCheck(
        available=True,
        extreme=extreme,
        ratio=round(ratio, 3) if math.isfinite(ratio) else None,
        recent_vol_pct=round(current * 100, 4),
        baseline_vol_pct=round(baseline * 100, 4),
        move_pct=round(move, 3),
        reason=reason,
    )
