"""Strategy lab (Phase 8): which rule variant works on recent real data, checked out of sample.

For one horizon and the selected coins, every combination of an entry filter and an exit style
is backtested with the scalp engine. Each coin's history is split in time: the older part
(70%) chooses the variant, the newer part (30%) - never used for choosing - tests it. The best
training variant is recommended only if it also made money on the newer data and did at least
as well there as the published rules. The grid is deliberately small (8 x 6): the more variants
one tries, the more likely the best-looking one is luck.

The chosen variant's trades also train the statistical filter (app.analysis.ml), with the same
time split.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any

from app.analysis import ml
from app.analysis import scalp as sc


@dataclass(frozen=True)
class Variant:
    key: str
    label: str
    changes: dict[str, Any]


FILTERS: tuple[Variant, ...] = (
    Variant("base", "published entry rules", {}),
    Variant("vol", "pullback trigger needs average volume", {"pullback_volume_confirm": True}),
    Variant("adx", "setup ADX 20 or more", {"min_adx": 20.0}),
    Variant("cost", "stop at least 3.5x costs away", {"min_risk_cost_multiple": 3.5}),
    Variant("pull", "pullbacks only", {"setups": ("pullback",)}),
    Variant("brk", "breakouts only", {"setups": ("breakout",)}),
    Variant("flow", "buyers in control at the trigger (taker buying 52%+)", {"min_taker_ratio": 0.52}),
    Variant("rs", "coin stronger than Bitcoin", {"min_relative_strength": 0.0}),
)
EXITS: tuple[Variant, ...] = (
    Variant("x1", "half at 1R, rest at 2R, break-even", {}),
    Variant("x2", "half at 1R, rest at 3R, break-even", {"tp2_r": 3.0}),
    Variant("x3", "half at 1.5R, rest at 3R, break-even", {"tp1_r": 1.5, "tp2_r": 3.0}),
    Variant("x4", "half at 1R, rest at 2R, no break-even", {"breakeven": False}),
    Variant("x5", "all at 1.5R", {"tp1_r": 1.5, "tp1_share": 1.0}),
    Variant("x6", "half at 1R, rest at 2R, double time limit", {"hold_mult": 2.0}),
)
FILTER_BY_KEY = {v.key: v for v in FILTERS}
EXIT_BY_KEY = {v.key: v for v in EXITS}
BASELINE = ("base", "x1")
TRAIN_FRACTION = 0.7
MIN_TRAIN_TRADES = 30
MIN_TEST_TRADES = 20
MIN_TRAIN_EXPECTANCY_R = 0.05  # the choosing data must itself show an edge after costs


def params_for(base: sc.ScalpParams, filter_key: str, exit_key: str) -> sc.ScalpParams:
    f, e = FILTER_BY_KEY[filter_key], EXIT_BY_KEY[exit_key]
    return replace(base, variant=f"{filter_key}/{exit_key}", **f.changes, **e.changes)


@dataclass
class CoinSeries:
    symbol: str
    series: sc.ScalpSeries
    is_btc: bool

    @property
    def split(self) -> int:
        n = len(self.series)
        return sc.WARMUP + int((n - sc.WARMUP) * TRAIN_FRACTION)


@dataclass
class SplitStats:
    trades: int
    win_rate: float | None
    expectancy_r: float | None
    profit_factor: float | None
    total_r: float

    @classmethod
    def of(cls, records: Sequence[sc.TradeRecord]) -> SplitStats:
        rs = [t.r_multiple for t in records]
        gains = math.fsum(r for r in rs if r > 0)
        losses = -math.fsum(r for r in rs if r < 0)
        return cls(
            trades=len(rs),
            win_rate=100.0 * sum(1 for r in rs if r > 0) / len(rs) if rs else None,
            expectancy_r=statistics.fmean(rs) if rs else None,
            profit_factor=(gains / losses) if losses > 0 else None,
            total_r=math.fsum(rs),
        )


@dataclass
class ComboResult:
    filter: str
    exit: str
    label: str
    train: SplitStats
    test: SplitStats


@dataclass
class LabResult:
    horizon: str
    generated_at: datetime
    coins: list[str]
    period_start: datetime | None
    split_time: datetime | None
    period_end: datetime | None
    combos: list[ComboResult]
    chosen: tuple[str, str]
    accepted: bool
    recommendation: tuple[str, str]  # what the scalp engine should use (chosen if accepted, else the rules as they are)
    reasons: list[str]
    stats: dict[str, Any]
    model: ml.Model | None = None
    cost_pct: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["model"] = self.model.as_dict() if self.model else None
        return _finite(data)


def _finite(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(v) for v in value]
    return value


def _stats(records: Sequence[sc.TradeRecord], split_time: datetime | None) -> dict[str, Any]:
    """Breakdowns of the recommended variant's trades (all of them, train and test)."""
    ordered = sorted(records, key=lambda t: t.entry_time)
    equity, points = 0.0, []
    step = max(1, len(ordered) // 300)
    for k, t in enumerate(ordered):
        equity += t.r_multiple
        if k % step == 0 or k == len(ordered) - 1:
            points.append({"time": t.entry_time, "r": round(equity, 3)})

    def group(key: Callable[[sc.TradeRecord], Any]) -> dict[str, dict[str, Any]]:
        buckets: dict[Any, list[sc.TradeRecord]] = {}
        for t in ordered:
            buckets.setdefault(key(t), []).append(t)
        return {str(k): asdict(SplitStats.of(v)) for k, v in sorted(buckets.items(), key=lambda kv: kv[0])}

    return {
        "all": asdict(SplitStats.of(ordered)),
        "train": asdict(SplitStats.of([t for t in ordered if split_time is None or t.entry_time < split_time])),
        "test": asdict(SplitStats.of([t for t in ordered if split_time is not None and t.entry_time >= split_time])),
        "equity": points,
        "by_setup": group(lambda t: t.kind),
        "by_hour_utc": group(lambda t: f"{(t.entry_time.hour // 4) * 4:02d}-{(t.entry_time.hour // 4) * 4 + 4:02d}"),
        "by_weekday": group(lambda t: ["1 Mon", "2 Tue", "3 Wed", "4 Thu", "5 Fri", "6 Sat", "7 Sun"][t.entry_time.weekday()]),
        "by_coin": group(lambda t: t.symbol),
        "by_outcome": {k: sum(1 for t in ordered if t.outcome == k) for k in sorted({t.outcome for t in ordered})},
    }


def _rows(coins: dict[str, CoinSeries], records: Sequence[sc.TradeRecord], cost_pct: float) -> list[ml.Row]:
    out = []
    for t in records:
        cs = coins[t.symbol]
        risk_pct = (t.entry - t.stop) / t.entry * 100.0
        out.append(ml.Row(sc.features_at(cs.series, t.index, t.kind, risk_pct, cost_pct), t.r_multiple, t.entry_time, t.symbol))
    return out


def choose(combos: list[ComboResult]) -> tuple[ComboResult, bool, list[str]]:
    """Pick the best TRAINING variant (sorting `combos` for display), then accept it only if the
    newer data confirms it. Returns (chosen, accepted, reasons)."""
    baseline = next(c for c in combos if (c.filter, c.exit) == BASELINE)
    min_trades = max(MIN_TRAIN_TRADES, int(0.25 * baseline.train.trades))
    # variants with enough training trades first, each group by training expectancy
    combos.sort(key=lambda c: (c.train.trades >= min_trades,
                               c.train.expectancy_r if c.train.expectancy_r is not None else -99, c.train.trades),
                reverse=True)
    eligible = [c for c in combos if c.train.trades >= min_trades]
    chosen = eligible[0] if eligible else baseline
    reasons: list[str] = []
    test_exp = chosen.test.expectancy_r
    base_test = baseline.test.expectancy_r
    if not eligible:
        reasons.append(f"no variant had {min_trades}+ training trades: not enough history to choose")
    if (chosen.train.expectancy_r or 0) < MIN_TRAIN_EXPECTANCY_R:
        reasons.append(f"even the best variant earned {chosen.train.expectancy_r or 0:+.2f}R per trade on the older data "
                       f"(needs {MIN_TRAIN_EXPECTANCY_R:+.2f}R): no rule set has an edge here after costs")
    if chosen.test.trades < MIN_TEST_TRADES:
        reasons.append(f"only {chosen.test.trades} trades in the newer 30% (needs {MIN_TEST_TRADES})")
    if test_exp is None or test_exp <= 0:
        reasons.append(f"the best training variant lost on the newer data ({test_exp or 0:+.2f}R per trade)")
    if (chosen.filter, chosen.exit) != BASELINE and base_test is not None and test_exp is not None and test_exp < base_test:
        reasons.append(f"on the newer data it did worse than the published rules ({test_exp:+.2f}R vs {base_test:+.2f}R)")
    accepted = not reasons
    if accepted:
        reasons = [f"chosen on the older 70% ({chosen.train.expectancy_r or 0:+.2f}R over {chosen.train.trades} trades) and "
                   f"confirmed on the newer 30% ({test_exp or 0:+.2f}R over {chosen.test.trades} trades)"]
    return chosen, accepted, reasons


def run_lab(
    coins: Sequence[CoinSeries],
    base: sc.ScalpParams,
    horizon: str,
    now: datetime,
    progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> LabResult:
    """Pure computation (run it in a worker thread): the variant grid, the choice, the model."""
    report = progress or (lambda _msg: None)
    stop = should_stop or (lambda: False)
    by_symbol = {c.symbol: c for c in coins}
    trades: dict[tuple[str, str], tuple[list[sc.TradeRecord], list[sc.TradeRecord]]] = {}
    for fi, f in enumerate(FILTERS):
        report(f"testing entry filter {fi + 1}/{len(FILTERS)}: {f.label}")
        filter_params = params_for(base, f.key, "x1")
        cands = {
            c.symbol: (
                sc.candidates(c.series, filter_params, is_btc=c.is_btc, end=c.split),
                sc.candidates(c.series, filter_params, is_btc=c.is_btc, start=c.split),
            )
            for c in coins
        }
        for e in EXITS:
            if stop():
                raise InterruptedError("lab stopped")
            p = params_for(base, f.key, e.key)
            train, test = [], []
            for c in coins:
                cand_train, cand_test = cands[c.symbol]
                train += sc.trades(c.series, p, {i: sc.retarget(x, p) for i, x in cand_train.items()}, symbol=c.symbol)
                test += sc.trades(c.series, p, {i: sc.retarget(x, p) for i, x in cand_test.items()}, symbol=c.symbol)
            trades[(f.key, e.key)] = (train, test)

    combos = [
        ComboResult(fk, ek, f"{FILTER_BY_KEY[fk].label}; {EXIT_BY_KEY[ek].label}", SplitStats.of(tr), SplitStats.of(te))
        for (fk, ek), (tr, te) in trades.items()
    ]
    chosen, accepted, reasons = choose(combos)
    recommended = (chosen.filter, chosen.exit) if accepted else BASELINE
    rec_train, rec_test = trades[recommended]

    splits = sorted(c.series.candles[c.split].open_time for c in coins if c.split < len(c.series))
    split_time = splits[len(splits) // 2] if splits else None
    starts = [c.series.candles[sc.WARMUP].open_time for c in coins if len(c.series) > sc.WARMUP]
    ends = [c.series.candles[-1].close_time for c in coins if c.series.candles]

    model = None
    if not stop():
        report("training the statistical filter")
        model = ml.train_and_validate(
            _rows(by_symbol, rec_train, base.cost_pct), _rows(by_symbol, rec_test, base.cost_pct), horizon, now
        )
    return LabResult(
        horizon=horizon, generated_at=now, coins=[c.symbol for c in coins],
        period_start=min(starts) if starts else None, split_time=split_time, period_end=max(ends) if ends else None,
        combos=combos, chosen=(chosen.filter, chosen.exit), accepted=accepted, recommendation=recommended,
        reasons=reasons, stats=_stats(rec_train + rec_test, split_time), model=model, cost_pct=base.cost_pct,
    )
