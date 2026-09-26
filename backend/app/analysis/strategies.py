"""Strategy library (Phase 11): well-known, published long setups, tested on every coin's history.

The scalp engine's own two setups (trend pullback, volatility breakout) are joined by six
strategies that professional traders and trading books have documented for decades. Each one
is written from its published rules, uses closed candles only, has its own stop and exit, and
is never trusted on reputation: every scan backtests it on the scanned coins, the older 70% of
each coin's history must show a profit and the newer 30% (never used for that decision) must
confirm it before the strategy may produce a buy (see `research`).

| key          | strategy                        | source                                             | exit                          |
|--------------|---------------------------------|----------------------------------------------------|-------------------------------|
| donchian     | 55-candle breakout              | Turtle Traders (R. Dennis, W. Eckhardt), R. Donchian | trail: 20-candle low        |
| ema_momentum | EMA 9/21 cross with ADX 18+     | classic momentum crossover; J. Welles Wilder (ADX)   | half 1.5R, trail 3 ATR      |
| rsi2         | RSI(2) pullback above EMA200    | L. Connors & C. Alvarez, "Short Term Trading Strategies That Work" | close above SMA5 |
| bb_revert    | Bollinger lower-band reclaim    | J. Bollinger, "Bollinger on Bollinger Bands"         | middle band                   |
| inside_bar   | inside-bar breakout in trend    | price action (A. Brooks, N. Fuller)                  | half 1R, rest 2.5R          |
| bos          | break of structure, higher low  | Dow theory / market structure ("smart money")        | half 1.5R, trail 3 ATR      |

Trailing exits follow C. LeBeau's chandelier exit (highest high since entry minus 3 ATR) or
the Turtles' 20-candle low. All exits are simulated pessimistically (app.analysis.trade_sim).
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from app.analysis import indicators as ind
from app.analysis import scalp as sc
from app.analysis.trade_sim import OPEN, SimTarget, simulate_long
from app.core.enums import SignalLabel
from app.core.formatting import fmt_price as _fmt
from app.data.normalization.schemas import Candle

TRAIN_FRACTION = 0.7
# research gates (pooled over the scanned coins): enough trades, profit on the older part, the
# newer part confirms it, and the whole record is unlikely to be luck
MIN_TRAIN_TRADES = 60
MIN_TEST_TRADES = 25
MIN_TRAIN_EXPECTANCY_R = 0.05
MIN_TEST_EXPECTANCY_R = 0.02
MIN_T_STAT = 1.5
COIN_MIN_TRADES = 8  # a coin whose own record (this many trades) lost is excluded
STRONG_TEST_EXPECTANCY_R = 0.15
STRONG_COIN_TRADES = 15
STRONG_COIN_EXPECTANCY_R = 0.25
STRONG_COIN_PF = 1.5


@dataclass(frozen=True)
class ExitSpec:
    mode: str  # targets | trail | mean
    tp1_r: float | None = None  # first target in R (None: no fixed target)
    tp1_share: float = 1.0
    tp2_r: float | None = None
    trail: str | None = None  # chandelier | donchian
    trail_atr: float = 3.0
    trail_len: int = 20
    exit_ma: int | None = None  # close above SMA(n) closes the trade
    hold_mult: float = 1.0
    breakeven: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExitSpec:
        fields = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**fields)


@dataclass(frozen=True)
class Strategy:
    key: str
    name: str
    family: str  # trend | breakout | mean_reversion | structure
    source: str
    rule: str  # the entry rule in one sentence
    exit: ExitSpec


CLASSIC_EXIT = ExitSpec("targets", tp1_r=1.0, tp1_share=0.5, tp2_r=2.0)
STRATEGIES: dict[str, Strategy] = {s.key: s for s in (
    Strategy("pullback", "Trend pullback", "trend", "L. Raschke's \"Holy Grail\" family: buy the first pullback in a trend",
             "uptrend, dip to the EMA20 with RSI reset, bullish close above the previous high", CLASSIC_EXIT),
    Strategy("breakout", "Volatility squeeze breakout", "breakout",
             "J. Bollinger's squeeze / M. Minervini's volatility contraction",
             "close above the 20-candle high on 1.8x volume after a quiet period", CLASSIC_EXIT),
    Strategy("donchian", "Turtle 55-candle breakout", "trend",
             "Turtle Traders (R. Dennis, W. Eckhardt); R. Donchian's channels",
             "close above the highest high of the previous 55 candles with the higher timeframe up",
             ExitSpec("trail", trail="donchian", trail_len=20, hold_mult=4.0, breakeven=False)),
    Strategy("ema_momentum", "EMA 9/21 momentum", "trend", "classic crossover with J. Welles Wilder's ADX trend filter",
             "EMA9 crosses above EMA21, price above EMA50, ADX 18+",
             ExitSpec("trail", tp1_r=1.5, tp1_share=0.5, trail="chandelier", trail_atr=3.0, hold_mult=3.0)),
    Strategy("rsi2", "Connors RSI(2) pullback", "mean_reversion",
             "L. Connors & C. Alvarez, \"Short Term Trading Strategies That Work\"",
             "price above EMA200, 2-period RSI below 10: buy the short-term dip in a long-term uptrend",
             ExitSpec("mean", exit_ma=5, hold_mult=2.0, breakeven=False)),
    Strategy("bb_revert", "Bollinger band reclaim", "mean_reversion", "J. Bollinger, \"Bollinger on Bollinger Bands\"",
             "a close back above the lower band after closing below it, price above EMA200",
             ExitSpec("targets", tp1_share=1.0, hold_mult=2.0, breakeven=False)),
    Strategy("inside_bar", "Inside-bar breakout", "breakout", "price action (A. Brooks, N. Fuller)",
             "an inside bar in an uptrend, then a close above the mother bar's high",
             ExitSpec("targets", tp1_r=1.0, tp1_share=0.5, tp2_r=2.5, hold_mult=1.5)),
    Strategy("bos", "Break of structure", "structure", "Dow theory / market structure (\"smart money concepts\")",
             "a higher swing low, then a close above the last swing high",
             ExitSpec("trail", tp1_r=1.5, tp1_share=0.5, trail="chandelier", trail_atr=3.0, hold_mult=3.0)),
)}
LIBRARY = ("donchian", "ema_momentum", "rsi2", "bb_revert", "inside_bar", "bos")


# ----------------------------------------------------------------------------- exits


class ExitRules:
    """Trailing stops and exit signals from a list of closed candles (backtest and track record)."""

    def __init__(self, candles: Sequence[Candle]) -> None:
        self.candles = candles
        self._atr: ind.Series | None = None
        self._sma: dict[int, ind.Series] = {}

    def atr(self) -> ind.Series:
        if self._atr is None:
            c = self.candles
            self._atr = ind.atr([x.high for x in c], [x.low for x in c], [x.close for x in c], 14)
        return self._atr

    def sma(self, n: int) -> ind.Series:
        if n not in self._sma:
            self._sma[n] = ind.sma([x.close for x in self.candles], n)
        return self._sma[n]

    def functions(self, spec: ExitSpec, entry_index: int) -> tuple[Callable[[int], float | None] | None,
                                                                   Callable[[int], bool] | None]:
        """(trail, exit_signal) for a trade whose signal candle is `entry_index`."""
        trail = exit_signal = None
        c = self.candles
        if spec.trail == "chandelier":
            atr = self.atr()
            state = {"high": c[entry_index].high if 0 <= entry_index < len(c) else 0.0}

            def trail(k: int) -> float | None:
                state["high"] = max(state["high"], c[k].high)
                a = atr[k]
                return state["high"] - spec.trail_atr * a if a else None
        elif spec.trail == "donchian":
            n = spec.trail_len

            def trail(k: int) -> float | None:
                return min(x.low for x in c[max(0, k - n + 1) : k + 1]) if k >= n - 1 else None
        if spec.exit_ma:
            sma = self.sma(spec.exit_ma)

            def exit_signal(k: int) -> bool:
                return sma[k] is not None and c[k].close > sma[k]  # type: ignore[operator]
        return trail, exit_signal


def sim_targets(entry: float, stop: float, spec: ExitSpec, target: float | None = None) -> list[SimTarget]:
    r = entry - stop
    if spec.mode == "targets" and target is not None and spec.tp1_r is None:
        return [SimTarget(target, 1.0)]
    out: list[SimTarget] = []
    if spec.tp1_r is not None:
        out.append(SimTarget(entry + spec.tp1_r * r, spec.tp1_share))
    if spec.tp2_r is not None and spec.tp1_share < 1.0:
        out.append(SimTarget(entry + spec.tp2_r * r, 1.0 - spec.tp1_share))
    return out


def exit_text(spec: ExitSpec, entry: float, stop: float, target: float | None = None) -> str:
    r = entry - stop
    parts = []
    if spec.mode == "targets" and target is not None and spec.tp1_r is None:
        parts.append(f"sell all at {_fmt(target)}")
    if spec.tp1_r is not None:
        share = "all" if spec.tp1_share >= 1 else f"{spec.tp1_share * 100:.0f}%"
        parts.append(f"sell {share} at {_fmt(entry + spec.tp1_r * r)} ({spec.tp1_r:g}R)")
        if spec.breakeven:
            parts.append("then move the stop to the entry")
    if spec.tp2_r is not None and spec.tp1_share < 1:
        parts.append(f"the rest at {_fmt(entry + spec.tp2_r * r)} ({spec.tp2_r:g}R)")
    if spec.trail == "chandelier":
        parts.append(f"trail the rest: stop at the highest high since entry minus {spec.trail_atr:g} ATR (raise it after each candle)")
    elif spec.trail == "donchian":
        parts.append(f"trail: stop at the lowest low of the last {spec.trail_len} candles (raise it after each candle)")
    if spec.exit_ma:
        parts.append(f"sell when a candle closes above its {spec.exit_ma}-period average")
    return "; ".join(parts)


# ----------------------------------------------------------------------------- entries


def _context(s: sc.ScalpSeries, i: int, is_btc: bool) -> bool:
    return s.trend_up[i] and not s.filter_down[i] and (is_btc or not s.btc_down[i])


def _stop(close: float, low: float, a: float, p: sc.ScalpParams, lo_atr: float = 0.8, hi_atr: float = 2.5) -> float:
    raw = close - (low - p.stop_buffer_atr * a)
    return close - min(max(raw, lo_atr * a), hi_atr * a)


def signal_at(s: sc.ScalpSeries, i: int, key: str, p: sc.ScalpParams, *, is_btc: bool = False) -> sc.Candidate | None:
    """The library strategy `key` on closed candle i (None: no signal)."""
    if i < sc.WARMUP or i >= len(s):
        return None
    a = s.atr[i]
    if a is None or a <= 0:
        return None
    close, prof = s.close[i], s.profile
    reasons: list[str] = []
    stop: float | None = None
    target: float | None = None
    if key == "donchian":
        hh = s.hh55[i] if s.hh55 else None
        if hh is None or close <= hh or not _context(s, i, is_btc):
            return None
        rng = s.high[i] - s.low[i]
        if rng <= 0 or (close - s.low[i]) / rng < 0.5:
            return None
        stop = close - 2.0 * a
        reasons = [f"{prof.setup.label} close above the 55-candle high {_fmt(hh)} (Turtle breakout), "
                   f"{prof.trend.label} trend up"]
    elif key == "ema_momentum":
        e9, e21, e9p, e21p, e50 = s.ema9[i], s.ema21[i], s.ema9[i - 1], s.ema21[i - 1], s.ema50[i]
        adx = s.adx[i]
        if None in (e9, e21, e9p, e21p, e50, adx) or not _context(s, i, is_btc):
            return None
        if not (e9p <= e21p and e9 > e21 and close > e50 and adx >= 18):  # type: ignore[operator]
            return None
        stop = _stop(close, min(s.low[i - 4 : i + 1]), a, p, 1.0, 2.5)
        reasons = [f"{prof.setup.label} EMA9 crossed above EMA21 with ADX {adx:.0f}, price above EMA50"]
    elif key == "rsi2":
        r2, e200 = s.rsi2[i], s.ema200[i]
        if r2 is None or e200 is None or close <= e200 or r2 >= 10 or s.filter_down[i] or (not is_btc and s.btc_down[i]):
            return None
        stop = close - 2.5 * a
        reasons = [f"{prof.setup.label} 2-period RSI {r2:.0f} (below 10) while price is above EMA200: short-term dip "
                   "in a long-term uptrend"]
    elif key == "bb_revert":
        lo, lo_prev, mid, e200 = s.bb_lower[i], s.bb_lower[i - 1], s.bb_mid[i], s.ema200[i]
        if None in (lo, lo_prev, mid, e200) or s.filter_down[i] or (not is_btc and s.btc_down[i]):
            return None
        if not (s.close[i - 1] < lo_prev and close > lo and close > s.open[i] and close > e200):  # type: ignore[operator]
            return None
        stop = _stop(close, min(s.low[i - 2 : i + 1]), a, p, 0.8, 2.0)
        if mid - close < 0.8 * (close - stop):  # type: ignore[operator]
            return None  # not enough room to the middle band
        target = mid
        reasons = [f"{prof.setup.label} closed back above the lower Bollinger band after closing below it; "
                   f"target the middle band {_fmt(mid)}"]  # type: ignore[arg-type]
    elif key == "inside_bar":
        if i < 2 or not _context(s, i, is_btc):
            return None
        mh, ml = s.high[i - 2], s.low[i - 2]
        inside = s.high[i - 1] < mh and s.low[i - 1] > ml
        e20 = s.ema20[i]
        if not inside or close <= mh or e20 is None or close <= e20 or close - e20 > 1.5 * a:
            return None
        stop = _stop(close, ml, a, p, 0.8, 2.5)
        reasons = [f"{prof.setup.label} inside bar, then a close above the mother bar's high {_fmt(mh)}"]
    elif key == "bos":
        if not _context(s, i, is_btc) or not s.pivot_lows:
            return None
        end_l = bisect.bisect_right([c for c, _ in s.pivot_lows], i)
        lows = [pr for c, pr in s.pivot_lows[:end_l] if c >= i - 150]
        end_h = bisect.bisect_right(s.pivot_confirm, i)
        highs = [pr for c, pr in s.pivot_highs[:end_h] if c >= i - 150]
        if len(lows) < 2 or not highs:
            return None
        last_low, prev_low, last_high = lows[-1], lows[-2], highs[-1]
        if not (last_low > prev_low and close > last_high and s.close[i - 1] <= last_high):
            return None
        stop = _stop(close, last_low, a, p, 0.8, 3.0)
        reasons = [f"{prof.setup.label} higher low {_fmt(last_low)} (above {_fmt(prev_low)}) and a close above the last "
                   f"swing high {_fmt(last_high)}: break of structure"]
    else:
        raise ValueError(f"unknown strategy {key}")
    if stop is None or stop <= 0 or stop >= close:
        return None
    risk_pct = (close - stop) / close * 100.0
    if risk_pct < p.min_risk_cost_multiple * p.cost_pct:
        return None
    spec = STRATEGIES[key].exit
    targets = sim_targets(close, stop, spec, target)
    tp1 = targets[0].price if targets else close + 2.0 * (close - stop)
    tp2 = targets[-1].price if len(targets) > 1 else close + 3.0 * (close - stop)
    return sc.Candidate(kind=key, index=i, time=s.candles[i].close_time, entry=close, stop=stop, tp1=tp1, tp2=max(tp2, tp1),
                        risk_pct=risk_pct, atr=a, reasons=reasons, level=target)


# ----------------------------------------------------------------------------- backtest


def trade(s: sc.ScalpSeries, cand: sc.Candidate, p: sc.ScalpParams, rules: ExitRules) -> Any:
    spec = STRATEGIES[cand.kind].exit
    trail, exit_signal = rules.functions(spec, cand.index)
    return simulate_long(
        s.candles, cand.index + 1, entry=cand.entry, stop=cand.stop,
        targets=sim_targets(cand.entry, cand.stop, spec, cand.level), cost_pct=p.cost_pct,
        max_hold=max(1, int(round(s.profile.max_hold * spec.hold_mult))), breakeven_after_first=spec.breakeven,
        trail=trail, exit_signal=exit_signal,
    )


def backtest_key(s: sc.ScalpSeries, key: str, p: sc.ScalpParams, *, is_btc: bool = False,
                 symbol: str = "") -> list[sc.TradeRecord]:
    """Every trade of one library strategy over the history, one at a time."""
    rules = ExitRules(s.candles)
    records: list[sc.TradeRecord] = []
    busy_until = -1
    for i in range(sc.WARMUP, len(s)):
        if i <= busy_until:
            continue
        cand = signal_at(s, i, key, p, is_btc=is_btc)
        if cand is None:
            continue
        res = trade(s, cand, p, rules)
        if res.outcome == OPEN:
            break
        records.append(sc.TradeRecord(key, s.candles[i].close_time, res.exit_time, cand.entry, cand.stop, res.outcome,
                                      res.r_multiple, res.return_pct, res.held, i, symbol))
        busy_until = res.exit_index if res.exit_index is not None else i
    return records


def latest(s: sc.ScalpSeries, key: str, p: sc.ScalpParams, *, is_btc: bool = False) -> sc.Candidate | None:
    """A signal on one of the last `fresh_candles` closed candles."""
    last = len(s) - 1
    for k in range(last, max(sc.WARMUP - 1, last - p.fresh_candles), -1):
        cand = signal_at(s, k, key, p, is_btc=is_btc)
        if cand is not None:
            return cand
    return None


# ----------------------------------------------------------------------------- research


@dataclass
class Split:
    trades: int = 0
    win_rate: float | None = None
    expectancy_r: float | None = None
    profit_factor: float | None = None
    total_r: float = 0.0

    @classmethod
    def of(cls, rs: Sequence[float]) -> Split:
        if not rs:
            return cls()
        gains = math.fsum(r for r in rs if r > 0)
        losses = -math.fsum(r for r in rs if r < 0)
        return cls(len(rs), 100.0 * sum(1 for r in rs if r > 0) / len(rs), statistics.fmean(rs),
                   gains / losses if losses > 0 else None, math.fsum(rs))


@dataclass
class Research:
    key: str
    name: str
    family: str
    source: str
    rule: str
    exit: str
    coins: int
    train: Split
    test: Split
    all: Split
    t_stat: float | None
    validated: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def t_stat(rs: Sequence[float]) -> float | None:
    if len(rs) < 3:
        return None
    sd = statistics.stdev(rs)
    return statistics.fmean(rs) / (sd / math.sqrt(len(rs))) if sd > 0 else None


def research(key: str, per_coin: dict[str, tuple[list[sc.TradeRecord], datetime | None]]) -> Research:
    """Pooled walk-forward check of one strategy: `per_coin` maps a coin to (its trades, its split time)."""
    train: list[float] = []
    test: list[float] = []
    for records, split in per_coin.values():
        for t in records:
            (train if split is None or t.entry_time < split else test).append(t.r_multiple)
    everything = train + test
    st = Strategy(key, key, "", "", "", CLASSIC_EXIT) if key not in STRATEGIES else STRATEGIES[key]
    r = Research(key, st.name, st.family, st.source, st.rule, _exit_summary(st.exit), len(per_coin), Split.of(train),
                 Split.of(test), Split.of(everything), t_stat(everything), False)
    reasons = []
    if len(train) < MIN_TRAIN_TRADES:
        reasons.append(f"only {len(train)} trades in the older 70% (needs {MIN_TRAIN_TRADES})")
    elif (r.train.expectancy_r or 0) < MIN_TRAIN_EXPECTANCY_R:
        reasons.append(f"older 70%: {r.train.expectancy_r or 0:+.2f}R per trade after costs (needs +{MIN_TRAIN_EXPECTANCY_R:g}R)")
    if len(test) < MIN_TEST_TRADES:
        reasons.append(f"only {len(test)} trades in the newer 30% (needs {MIN_TEST_TRADES})")
    elif (r.test.expectancy_r or 0) < MIN_TEST_EXPECTANCY_R:
        reasons.append(f"newer 30% did not confirm: {r.test.expectancy_r or 0:+.2f}R per trade")
    if not reasons and (r.t_stat is None or r.t_stat < MIN_T_STAT):
        reasons.append(f"the profit could be luck (t-statistic {r.t_stat or 0:.1f}, needs {MIN_T_STAT:g})")
    r.validated = not reasons
    r.reasons = reasons or [f"validated: {r.train.expectancy_r:+.2f}R on the older part, {r.test.expectancy_r:+.2f}R "
                            f"on the newer part ({len(everything)} trades, {len(per_coin)} coins)"]
    return r


def research_order(r: Research) -> tuple[bool, bool, float]:
    """Display order: validated first, then strategies with enough trades to judge (best first), then
    the rest (closest to enough trades first), so a handful of lucky trades never tops the table."""
    enough = r.train.trades >= MIN_TRAIN_TRADES and r.test.trades >= MIN_TEST_TRADES
    if not (r.validated or enough):
        return (True, True, -float(r.all.trades))
    return (not r.validated, not enough, -(r.all.expectancy_r if r.all.expectancy_r is not None else -9.0))


def _exit_summary(spec: ExitSpec) -> str:
    if spec.trail == "donchian":
        return f"trailing stop at the {spec.trail_len}-candle low"
    if spec.trail == "chandelier" and spec.tp1_r:
        return f"half at {spec.tp1_r:g}R, trail the rest {spec.trail_atr:g} ATR under the high"
    if spec.exit_ma:
        return f"close above the {spec.exit_ma}-period average"
    if spec.mode == "targets" and spec.tp1_r is None:
        return "middle Bollinger band"
    if spec.tp2_r:
        return f"half at {spec.tp1_r:g}R, rest at {spec.tp2_r:g}R"
    return "fixed target"


def verdict(key: str, coin: Sequence[sc.TradeRecord], res: Research | None) -> tuple[SignalLabel, list[str]]:
    """The label a live signal of a library strategy may get on this coin."""
    name = STRATEGIES[key].name
    if res is None:
        return SignalLabel.WATCH, [f"{name}: not researched yet at this horizon (run a scan)"]
    if not res.validated:
        label = SignalLabel.NO_TRADE if (res.all.expectancy_r or 0) < 0 else SignalLabel.WATCH
        return label, [f"{name} is not validated on the scanned coins: {res.reasons[0]}"]
    own = Split.of([t.r_multiple for t in coin])
    if own.trades >= COIN_MIN_TRADES and (own.expectancy_r or 0) < 0:
        return SignalLabel.WATCH, [f"{name} is validated across coins but lost on this coin "
                                   f"({own.trades} trades, {own.expectancy_r:+.2f}R)"]
    text = (f"{name} validated on the scanned coins: {res.train.expectancy_r:+.2f}R (older) and "
            f"{res.test.expectancy_r:+.2f}R (newer) per trade after costs; this coin: {own.trades} trades"
            + (f", {own.expectancy_r:+.2f}R" if own.trades else ""))
    strong = ((res.test.expectancy_r or 0) >= STRONG_TEST_EXPECTANCY_R and own.trades >= STRONG_COIN_TRADES
              and (own.expectancy_r or 0) >= STRONG_COIN_EXPECTANCY_R
              and (own.profit_factor is None or own.profit_factor >= STRONG_COIN_PF))
    return (SignalLabel.STRONG_BUY if strong else SignalLabel.BUY), [text]
