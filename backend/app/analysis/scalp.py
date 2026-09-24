"""Short-term (scalp) signals for spot longs, verified on each coin's own history.

Three horizons, each with a setup timeframe (where the entry is found), a trend timeframe
(which must be trending up) and a filter timeframe (which must not be trending down):

| Horizon | Setup | Trend | Filter | Time limit          |
|---------|-------|-------|--------|---------------------|
| 15m     | 5m    | 15m   | 1H     | 6 candles (30 min)  |
| 1h      | 15m   | 1H    | 4H     | 8 candles (2 hours) |
| 4h      | 1H    | 4H    | 1D     | 8 candles (8 hours) |

Two setups, both long only:

* pullback: in an uptrend the price dips back to the EMA20, RSI resets to 52 or lower, then a
  bullish candle closes above the previous candle's high (the trigger);
* breakout: a close above the 20-candle high on at least 1.8x average volume, after a quiet
  period (Bollinger width at or below its 100-candle median), closing in the top 40% of the
  candle and not overbought.

Both also need: the trend timeframe up, the filter timeframe not down, Bitcoin's trend
timeframe not down (for altcoins), the price above the day's VWAP (5m and 15m setups), the
nearest swing high at least 1R away, and a stop wide enough that fees cannot eat the trade.

The plan: entry at the signal candle's close, stop under the recent low (0.8 to 2 ATR), TP1
at 1R for half the position (then the stop moves to break-even), TP2 at 2R for the rest,
and a time exit. The exact same rules run over the coin's recent history (the backtest),
so every live signal carries its measured record on that coin: a setup that lost money
there is not shown as a buy. Nothing here uses future data: indicators are causal, swing
highs count only once confirmed, and higher timeframes only contribute closed candles.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.analysis import indicators as ind
from app.analysis.structure import find_pivots
from app.analysis.trade_sim import OPEN, SimResult, SimTarget, simulate_long
from app.core.enums import SignalLabel, Timeframe
from app.core.formatting import fmt_price as _fmt
from app.data.normalization.schemas import Candle

WARMUP = 60
PULLBACK = "pullback"
BREAKOUT = "breakout"
SETUPS = (PULLBACK, BREAKOUT)


@dataclass(frozen=True)
class ScalpProfile:
    key: str
    label: str
    setup: Timeframe
    trend: Timeframe
    filter: Timeframe
    max_hold: int  # setup candles before the time exit
    use_vwap: bool
    history: int  # setup candles fetched for the backtest

    @property
    def hold_minutes(self) -> int:
        return self.max_hold * self.setup.minutes


PROFILES: dict[str, ScalpProfile] = {
    "15m": ScalpProfile("15m", "15-minute scalp", Timeframe.M5, Timeframe.M15, Timeframe.H1, 6, True, 4000),
    "1h": ScalpProfile("1h", "1-hour trade", Timeframe.M15, Timeframe.H1, Timeframe.H4, 8, True, 4000),
    "4h": ScalpProfile("4h", "4-hour trade", Timeframe.H1, Timeframe.H4, Timeframe.D1, 8, False, 4000),
}


@dataclass(frozen=True)
class ScalpParams:
    fee_pct: float = 0.1  # per side
    slippage_pct: float = 0.02  # per side (liquid pairs, small size)
    min_risk_cost_multiple: float = 2.5  # stop distance must be >= this many round-trip costs
    stop_buffer_atr: float = 0.2
    min_stop_atr: float = 0.8
    max_stop_atr: float = 2.0
    tp1_r: float = 1.0
    tp2_r: float = 2.0
    tp1_share: float = 0.5
    min_room_r: float = 0.75  # nearest swing high above the entry, in R
    min_tp1_r: float = 0.6  # TP1 moves under a closer swing high, but never below this
    breakout_lookback: int = 20
    breakout_volume_ratio: float = 1.8
    fresh_candles: int = 2  # a setup stays actionable for this many closed candles
    max_chase_r: float = 0.25  # live price may be at most this far above the signal entry
    # evidence gate (backtest on the coin's own recent history)
    min_trades: int = 15
    min_expectancy_r: float = 0.10
    min_profit_factor: float = 1.2
    strong_min_trades: int = 25
    strong_min_expectancy_r: float = 0.25
    strong_min_profit_factor: float = 1.5
    strong_min_win_rate: float = 50.0
    setup_min_trades: int = 8  # a setup type with this many trades must itself be profitable
    pooled_min_trades: int = 40  # rules across all scanned coins, used when one coin has too few trades
    coin_max_loss_r: float = -3.0  # a coin whose own few trades lost more than this is not traded
    # rule options the strategy lab can test (defaults = the published rules)
    variant: str = "default"
    setups: tuple[str, ...] = ("pullback", "breakout")
    breakeven: bool = True  # move the stop to the entry after TP1
    pullback_rsi_max: float = 70.0
    min_adx: float = 0.0  # setup-timeframe ADX floor (0 = off)
    pullback_volume_confirm: bool = False  # trigger candle volume at least its 20-candle average
    hold_mult: float = 1.0  # time limit multiplier

    @property
    def cost_pct(self) -> float:
        return 2.0 * (self.fee_pct + self.slippage_pct)

    def max_hold(self, profile: ScalpProfile) -> int:
        return max(1, int(round(profile.max_hold * self.hold_mult)))


# ----------------------------------------------------------------------------- series


def _align(setup: Sequence[Candle], higher: Sequence[Candle]) -> list[int | None]:
    """For each setup candle, the index of the last higher-timeframe candle closed by then."""
    closes = [c.close_time for c in higher]
    out: list[int | None] = []
    for c in setup:
        j = bisect.bisect_right(closes, c.close_time) - 1
        out.append(j if j >= 0 else None)
    return out


def _trend_state(candles: Sequence[Candle]) -> tuple[list[bool], list[bool]]:
    """(up, down) per candle: close and EMA20 on the same side of EMA50."""
    closes = [c.close for c in candles]
    e20, e50 = ind.ema(closes, 20), ind.ema(closes, 50)
    up, down = [], []
    for c, a, b in zip(closes, e20, e50, strict=True):
        up.append(a is not None and b is not None and c > b and a > b)
        down.append(a is not None and b is not None and c < b and a < b)
    return up, down


def _daily_vwap(candles: Sequence[Candle]) -> list[float | None]:
    out: list[float | None] = []
    day = None
    pv = vol = 0.0
    for c in candles:
        d = c.open_time.date()
        if d != day:
            day, pv, vol = d, 0.0, 0.0
        typical = (c.high + c.low + c.close) / 3.0
        pv += typical * c.volume
        vol += c.volume
        out.append(pv / vol if vol > 0 else None)
    return out


@dataclass
class ScalpSeries:
    profile: ScalpProfile
    candles: list[Candle]
    close: list[float]
    open: list[float]
    high: list[float]
    low: list[float]
    volume: list[float]
    ema20: ind.Series
    ema50: ind.Series
    rsi: ind.Series
    atr: ind.Series
    adx: ind.Series
    vol_avg: list[float | None]  # average volume of the previous 20 candles
    bb_width: list[float | None]
    vwap: list[float | None]
    trend_up: list[bool]
    filter_down: list[bool]
    btc_down: list[bool]
    btc_known: bool
    pivot_highs: list[tuple[int, float]]  # (confirmation index, price), by confirmation index
    pivot_confirm: list[int]
    trend_rsi: list[float | None]  # trend timeframe, aligned to the setup candles
    trend_dist: list[float | None]  # trend timeframe (close - EMA50) / ATR
    trend_roc: list[float | None]  # trend timeframe 10-candle rate of change, percent
    btc_roc: list[float | None]  # Bitcoin's trend-timeframe 10-candle rate of change

    def __len__(self) -> int:
        return len(self.candles)


def build_series(
    profile: ScalpProfile,
    setup: Sequence[Candle],
    trend: Sequence[Candle],
    filter_candles: Sequence[Candle],
    btc_trend: Sequence[Candle] | None = None,
) -> ScalpSeries:
    closes = [c.close for c in setup]
    highs = [c.high for c in setup]
    lows = [c.low for c in setup]
    bands = ind.bollinger(closes, 20, 2.0)
    width = [
        (u - lo) / m * 100.0 if u is not None and lo is not None and m else None
        for u, lo, m in zip(bands.upper, bands.lower, bands.middle, strict=True)
    ]
    t_up, _ = _trend_state(trend)
    _, f_down = _trend_state(filter_candles)
    t_idx, f_idx = _align(setup, trend), _align(setup, filter_candles)
    trend_up = [j is not None and t_up[j] for j in t_idx]
    filter_down = [j is None or f_down[j] for j in f_idx]  # unknown counts as down (fail closed)
    t_close = [c.close for c in trend]
    t_rsi = ind.rsi(t_close, 14)
    t_e50 = ind.ema(t_close, 50)
    t_atr = ind.atr([c.high for c in trend], [c.low for c in trend], t_close, 14)
    t_roc = ind.roc(t_close, 10)
    t_dist = [(c - e) / a if e is not None and a else None for c, e, a in zip(t_close, t_e50, t_atr, strict=True)]
    btc_roc: list[float | None] = [None] * len(setup)
    if btc_trend:
        _, b_down = _trend_state(btc_trend)
        b_idx = _align(setup, btc_trend)
        btc_down = [j is None or b_down[j] for j in b_idx]
        b_roc = ind.roc([c.close for c in btc_trend], 10)
        btc_roc = [b_roc[j] if j is not None else None for j in b_idx]
    else:
        btc_down = [False] * len(setup)
    volumes = [c.volume for c in setup]
    vol_avg: list[float | None] = [None] * len(setup)
    window = 0.0
    for k, v in enumerate(volumes):
        if k >= 20:
            vol_avg[k] = window / 20.0
            window -= volumes[k - 20]
        window += v
    right = 3
    pivots = sorted(
        ((p.index + right, p.price) for p in find_pivots(setup, 5, right) if p.kind == "high"), key=lambda x: x[0]
    )
    return ScalpSeries(
        profile=profile,
        candles=list(setup),
        close=closes,
        open=[c.open for c in setup],
        high=highs,
        low=lows,
        volume=volumes,
        ema20=ind.ema(closes, 20),
        ema50=ind.ema(closes, 50),
        rsi=ind.rsi(closes, 14),
        atr=ind.atr(highs, lows, closes, 14),
        adx=ind.dmi(highs, lows, closes, 14).adx,
        vol_avg=vol_avg,
        bb_width=width,
        vwap=_daily_vwap(setup) if profile.use_vwap else [None] * len(setup),
        trend_up=trend_up,
        filter_down=filter_down,
        btc_down=btc_down,
        btc_known=bool(btc_trend),
        pivot_highs=pivots,
        pivot_confirm=[p[0] for p in pivots],
        trend_rsi=[t_rsi[j] if j is not None else None for j in t_idx],
        trend_dist=[t_dist[j] if j is not None else None for j in t_idx],
        trend_roc=[t_roc[j] if j is not None else None for j in t_idx],
        btc_roc=btc_roc,
    )


# ----------------------------------------------------------------------------- setups


@dataclass
class Candidate:
    kind: str
    index: int
    time: datetime  # close time of the signal candle
    entry: float
    stop: float
    tp1: float
    tp2: float
    risk_pct: float
    atr: float
    reasons: list[str] = field(default_factory=list)


def _nearest_high(s: ScalpSeries, i: int, above: float, lookback: int = 150) -> float | None:
    """Nearest confirmed swing high above `above`, from swing points confirmed by candle i."""
    end = bisect.bisect_right(s.pivot_confirm, i)
    best = None
    for confirm, price in s.pivot_highs[:end]:
        if confirm < i - lookback or price <= above:
            continue
        if best is None or price < best:
            best = price
    return best


def evaluate_at(
    s: ScalpSeries, i: int, p: ScalpParams, *, explain: bool = False, is_btc: bool = False
) -> tuple[Candidate | None, list[str]]:
    """The setup on closed candle i, or None and (with `explain`) why not."""
    why: list[str] = []

    def no(reason: str) -> tuple[None, list[str]]:
        if explain:
            why.append(reason)
        return None, why

    if i < WARMUP or i >= len(s):
        return no("not enough history on the setup timeframe")
    a = s.atr[i]
    e20, e50, r = s.ema20[i], s.ema50[i], s.rsi[i]
    if a is None or a <= 0 or e20 is None or e50 is None or r is None or s.rsi[i - 1] is None:
        return no("indicators not ready")
    prof = s.profile
    if not s.trend_up[i]:
        return no(f"{prof.trend.label} trend is not up (close and EMA20 must be above EMA50)")
    if s.filter_down[i]:
        return no(f"{prof.filter.label} trend is down")
    if not is_btc and s.btc_down[i]:
        return no(f"Bitcoin's {prof.trend.label} trend is down")
    close = s.close[i]
    vw = s.vwap[i]
    if prof.use_vwap and vw is not None and close <= vw:
        return no(f"price below today's VWAP {_fmt(vw)}")

    if p.min_adx > 0 and (s.adx[i] is None or s.adx[i] < p.min_adx):  # type: ignore[operator]
        return no(f"{prof.setup.label} ADX {s.adx[i] or 0:.0f} below {p.min_adx:g}: trend too weak")
    kind = None
    reasons: list[str] = []
    lo3 = min(s.low[i - 3 : i + 1])
    rsi_prev = [v for v in s.rsi[i - 5 : i] if v is not None]
    avg_prev = s.vol_avg[i]
    # pullback continuation
    if (
        PULLBACK in p.setups
        and e20 > e50 and close > e50
        and lo3 <= e20 + 0.3 * a and lo3 >= e50 - 0.5 * a
        and rsi_prev and min(rsi_prev) <= 52 and r > s.rsi[i - 1] and 45 <= r <= p.pullback_rsi_max  # type: ignore[operator]
        and close > s.open[i] and close > s.high[i - 1]
        and close - e20 <= 1.0 * a
        and (not p.pullback_volume_confirm or (avg_prev is not None and s.volume[i] >= avg_prev))
    ):
        kind = PULLBACK
        reasons = [
            f"{prof.setup.label} uptrend pullback to the EMA20, RSI reset to {min(rsi_prev):.0f} and turning up ({r:.0f})",
            f"trigger: bullish candle closed above the previous high {_fmt(s.high[i - 1])}",
        ]
    else:
        n = p.breakout_lookback
        prior_high = max(s.high[i - n : i])
        vols = s.volume[i - n : i]
        avg_vol = statistics.fmean(vols) if vols else 0.0
        rng = s.high[i] - s.low[i]
        widths = [w for w in s.bb_width[max(0, i - 100) : i] if w is not None]
        prev_w = s.bb_width[i - 1]
        if (
            BREAKOUT in p.setups
            and close > prior_high and close > e50
            and avg_vol > 0 and s.volume[i] >= p.breakout_volume_ratio * avg_vol
            and rng > 0 and (close - s.low[i]) / rng >= 0.6
            and prev_w is not None and len(widths) >= 20 and prev_w <= statistics.median(widths)
            and r <= 78 and close - e20 <= 2.5 * a
        ):
            kind = BREAKOUT
            reasons = [
                f"{prof.setup.label} breakout above the {n}-candle high {_fmt(prior_high)} "
                f"on {s.volume[i] / avg_vol:.1f}x average volume after a quiet period",
            ]
    if kind is None:
        if explain:
            if not (e20 > e50):
                why.append(f"{prof.setup.label} EMA20 below EMA50: no pullback setup")
            elif close - e20 > 1.0 * a:
                why.append(f"price {((close - e20) / a):.1f} ATR above the {prof.setup.label} EMA20: wait for a pullback")
            else:
                why.append(f"no {prof.setup.label} trigger yet (pullback with RSI reset and a close above the previous high, "
                           "or a volume breakout)")
        return None, why

    low_window = s.low[i - 3 : i + 1] if kind == PULLBACK else s.low[i - 2 : i + 1]
    raw = close - (min(low_window) - p.stop_buffer_atr * a)
    distance = min(max(raw, p.min_stop_atr * a), p.max_stop_atr * a)
    stop = close - distance
    if stop <= 0:
        return no("stop below zero")
    risk_pct = distance / close * 100.0
    if risk_pct < p.min_risk_cost_multiple * p.cost_pct:
        return no(f"move too small for fees: stop {risk_pct:.2f}% away, round-trip costs {p.cost_pct:.2f}% "
                  f"(needs {p.min_risk_cost_multiple:g}x costs)")
    level = _nearest_high(s, i, close)
    if level is not None and level - close < p.min_room_r * distance:
        return no(f"resistance {_fmt(level)} only {(level - close) / distance:.2f}R above: no room")
    tp1 = close + p.tp1_r * distance
    if level is not None and level - 0.1 * a < tp1:
        tp1 = max(level - 0.1 * a, close + p.min_tp1_r * distance)  # take the first profit under the swing high
    if prof.use_vwap and vw is not None:
        reasons.append(f"above today's VWAP {_fmt(vw)}")
    reasons.append(f"{prof.trend.label} trend up, {prof.filter.label} not down"
                   + ("" if is_btc or not s.btc_known else f", Bitcoin {prof.trend.label} not down"))
    reasons.append("room to the next swing high: " + (f"{(level - close) / distance:.1f}R" if level else "none overhead"))
    cand = Candidate(
        kind=kind, index=i, time=s.candles[i].close_time, entry=close, stop=stop,
        tp1=tp1, tp2=close + p.tp2_r * distance, risk_pct=risk_pct, atr=a, reasons=reasons,
    )
    return cand, why


# ----------------------------------------------------------------------------- waiting setups


@dataclass
class Pending:
    """What would make a coin a trade when its trend is right but no setup has triggered yet.
    Conditional levels only: nothing to do until the trigger happens."""

    kind: str  # pullback | dip | breakout
    trigger: float  # buy only after a candle closes above this (dip: limit price near the EMA20)
    stop: float
    tp1: float
    tp2: float
    risk_pct: float
    fees_ok: bool
    text: str


def pending_plan(s: ScalpSeries, i: int, p: ScalpParams, *, is_btc: bool = False) -> Pending | None:
    """The conditional plan at candle i, or None when the trend context is not right."""
    if i < WARMUP or i >= len(s):
        return None
    a, e20, e50 = s.atr[i], s.ema20[i], s.ema50[i]
    if a is None or a <= 0 or e20 is None or e50 is None:
        return None
    if not s.trend_up[i] or s.filter_down[i] or (not is_btc and s.btc_down[i]):
        return None
    prof = s.profile
    close = s.close[i]
    if PULLBACK in p.setups and e20 > e50 and close > e50 and close - e20 <= 1.0 * a:
        kind, trigger = PULLBACK, s.high[i]
        base_low = min(s.low[i - 3 : i + 1])
        text = f"buy only if a {prof.setup.label} candle closes above {_fmt(trigger)} (this candle's high) with RSI rising"
    elif PULLBACK in p.setups and e20 > e50 and close - e20 > 1.0 * a:
        kind, trigger = "dip", e20 + 0.3 * a
        base_low = trigger - 1.0 * a
        text = (f"extended {((close - e20) / a):.1f} ATR above the {prof.setup.label} EMA20: wait for a dip toward "
                f"{_fmt(trigger)}, then a close above the previous candle's high")
    elif BREAKOUT in p.setups:
        n = p.breakout_lookback
        kind, trigger = BREAKOUT, max(s.high[i - n + 1 : i + 1])
        base_low = min(s.low[i - 2 : i + 1])
        text = (f"buy only if a {prof.setup.label} candle closes above the {n}-candle high {_fmt(trigger)} "
                f"on {p.breakout_volume_ratio:g}x average volume")
    else:
        return None
    raw = trigger - (base_low - p.stop_buffer_atr * a)
    distance = min(max(raw, p.min_stop_atr * a), p.max_stop_atr * a)
    stop = trigger - distance
    if stop <= 0:
        return None
    risk_pct = distance / trigger * 100.0
    fees_ok = risk_pct >= p.min_risk_cost_multiple * p.cost_pct
    if not fees_ok:
        text += f"; at today's volatility the stop ({risk_pct:.2f}%) is too tight for {p.cost_pct:.2f}% costs"
    return Pending(kind, trigger, stop, trigger + p.tp1_r * distance, trigger + p.tp2_r * distance, risk_pct, fees_ok, text)


# ----------------------------------------------------------------------------- features (ML)

FEATURES = (
    "rsi", "rsi_change", "adx", "atr_pct", "dist_ema20_atr", "ema20_slope", "volume_ratio", "bb_width_rank",
    "body_position", "trend_rsi", "trend_dist_atr", "btc_roc", "relative_strength", "hour_sin", "hour_cos",
    "is_breakout", "risk_cost_ratio",
)


def features_at(s: ScalpSeries, i: int, kind: str, risk_pct: float, cost_pct: float) -> dict[str, float]:
    """Numbers describing the market at candle i, known at its close (inputs of the ML filter).
    Missing values fall back to neutral constants so every trade has the same features."""
    close, a = s.close[i], s.atr[i] or 0.0
    e20, e20_before = s.ema20[i], s.ema20[i - 5] if i >= 5 else None
    rsi, rsi_prev = s.rsi[i], s.rsi[i - 1]
    atr_pct = a / close * 100.0 if close > 0 else 0.0
    widths = [w for w in s.bb_width[max(0, i - 100) : i + 1] if w is not None]
    rng = s.high[i] - s.low[i]
    hour = s.candles[i].close_time.hour + s.candles[i].close_time.minute / 60.0
    trend_roc = s.trend_roc[i] if s.trend_roc[i] is not None else 0.0
    btc_roc = s.btc_roc[i] if s.btc_roc[i] is not None else trend_roc
    return {
        "rsi": rsi if rsi is not None else 50.0,
        "rsi_change": (rsi - rsi_prev) if rsi is not None and rsi_prev is not None else 0.0,
        "adx": s.adx[i] if s.adx[i] is not None else 20.0,
        "atr_pct": atr_pct,
        "dist_ema20_atr": (close - e20) / a if e20 is not None and a else 0.0,
        "ema20_slope": ((e20 / e20_before - 1.0) * 100.0 / atr_pct) if e20 and e20_before and atr_pct else 0.0,
        "volume_ratio": s.volume[i] / s.vol_avg[i] if s.vol_avg[i] else 1.0,
        "bb_width_rank": ind.percentile_rank(widths, widths[-1]) / 100.0 if len(widths) >= 20 else 0.5,
        "body_position": (close - s.low[i]) / rng if rng > 0 else 0.5,
        "trend_rsi": s.trend_rsi[i] if s.trend_rsi[i] is not None else 50.0,
        "trend_dist_atr": s.trend_dist[i] if s.trend_dist[i] is not None else 0.0,
        "btc_roc": btc_roc,
        "relative_strength": trend_roc - btc_roc,
        "hour_sin": math.sin(2 * math.pi * hour / 24.0),
        "hour_cos": math.cos(2 * math.pi * hour / 24.0),
        "is_breakout": 1.0 if kind == BREAKOUT else 0.0,
        "risk_cost_ratio": risk_pct / cost_pct if cost_pct > 0 else 10.0,
    }


# ----------------------------------------------------------------------------- backtest


@dataclass
class TradeRecord:
    kind: str
    entry_time: datetime
    exit_time: datetime | None
    entry: float
    stop: float
    outcome: str
    r_multiple: float
    return_pct: float
    held: int
    index: int = -1  # signal candle index in the series (for features)
    symbol: str = ""


@dataclass
class SetupStats:
    trades: int
    win_rate: float | None
    expectancy_r: float | None


@dataclass
class BacktestStats:
    horizon: str
    setup_timeframe: str
    candles: int
    period_start: datetime | None
    period_end: datetime | None
    trades: int
    wins: int
    win_rate: float | None
    expectancy_r: float | None  # average net R per trade
    profit_factor: float | None
    total_r: float
    total_return_pct: float  # sum of net returns, full position each trade, not compounded
    max_drawdown_r: float
    avg_hold_candles: float | None
    outcomes: dict[str, int]
    by_setup: dict[str, SetupStats]
    recent: list[TradeRecord]
    cost_pct: float
    coins: int = 1  # coins pooled into these statistics
    records: list[TradeRecord] = field(default_factory=list, repr=False)  # every trade (not sent to the API)


def _trade(s: ScalpSeries, cand: Candidate, p: ScalpParams) -> SimResult:
    targets = [SimTarget(cand.tp1, p.tp1_share)]
    if p.tp1_share < 1.0:
        targets.append(SimTarget(cand.tp2, 1.0 - p.tp1_share))
    return simulate_long(
        s.candles, cand.index + 1, entry=cand.entry, stop=cand.stop, targets=targets,
        cost_pct=p.cost_pct, max_hold=p.max_hold(s.profile), breakeven_after_first=p.breakeven,
    )


def candidates(s: ScalpSeries, p: ScalpParams, *, is_btc: bool = False,
               start: int = WARMUP, end: int | None = None) -> dict[int, Candidate]:
    """Every setup in [start, end) (entry rules only; exits do not change them)."""
    out: dict[int, Candidate] = {}
    for i in range(max(start, WARMUP), min(end if end is not None else len(s), len(s))):
        cand, _ = evaluate_at(s, i, p, is_btc=is_btc)
        if cand is not None:
            out[i] = cand
    return out


def trades(s: ScalpSeries, p: ScalpParams, cands: dict[int, Candidate], *, symbol: str = "") -> list[TradeRecord]:
    """One trade at a time through the candidate setups; a trade must finish inside the data."""
    records: list[TradeRecord] = []
    busy_until = -1
    for i in sorted(cands):
        if i <= busy_until:
            continue
        cand = cands[i]
        res = _trade(s, cand, p)
        if res.outcome == OPEN:
            break  # not enough candles left to finish this trade
        records.append(TradeRecord(cand.kind, s.candles[i].close_time, res.exit_time, cand.entry, cand.stop,
                                   res.outcome, res.r_multiple, res.return_pct, res.held, i, symbol))
        busy_until = res.exit_index if res.exit_index is not None else i
    return records


def backtest(
    s: ScalpSeries, p: ScalpParams, *, is_btc: bool = False, keep_recent: int = 12,
    start: int = WARMUP, end: int | None = None, symbol: str = "",
) -> BacktestStats:
    """Replay the rules over the history (or the window [start, end)): one trade at a time,
    entries at the signal close."""
    records = trades(s, p, candidates(s, p, is_btc=is_btc, start=start, end=end), symbol=symbol)
    first = max(start, WARMUP) if len(s) > WARMUP else 0
    last = min(end if end is not None else len(s), len(s)) - 1
    return summarize(
        records, horizon=s.profile.key, setup_timeframe=s.profile.setup.label, candles=max(0, last - first + 1),
        period_start=s.candles[first].open_time if 0 <= first < len(s) else None,
        period_end=s.candles[last].close_time if 0 <= last < len(s) else None, cost_pct=p.cost_pct,
        keep_recent=keep_recent,
    )


def _setup_stats(rs: Sequence[TradeRecord]) -> SetupStats:
    if not rs:
        return SetupStats(0, None, None)
    return SetupStats(len(rs), 100.0 * sum(1 for t in rs if t.r_multiple > 0) / len(rs),
                      statistics.fmean(t.r_multiple for t in rs))


def summarize(
    records: Sequence[TradeRecord],
    *,
    horizon: str,
    setup_timeframe: str,
    candles: int,
    period_start: datetime | None,
    period_end: datetime | None,
    cost_pct: float,
    keep_recent: int = 12,
    coins: int = 1,
) -> BacktestStats:
    records = sorted(records, key=lambda t: t.entry_time)
    rs = [t.r_multiple for t in records]
    wins = sum(1 for r in rs if r > 0)
    gains = math.fsum(r for r in rs if r > 0)
    losses = -math.fsum(r for r in rs if r < 0)
    equity = peak = drawdown = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    outcomes: dict[str, int] = {}
    for t in records:
        outcomes[t.outcome] = outcomes.get(t.outcome, 0) + 1
    return BacktestStats(
        horizon=horizon,
        setup_timeframe=setup_timeframe,
        candles=candles,
        period_start=period_start,
        period_end=period_end,
        trades=len(records),
        wins=wins,
        win_rate=100.0 * wins / len(records) if records else None,
        expectancy_r=statistics.fmean(rs) if rs else None,
        profit_factor=(gains / losses) if losses > 0 else (None if not rs else math.inf),
        total_r=math.fsum(rs),
        total_return_pct=math.fsum(t.return_pct for t in records),
        max_drawdown_r=drawdown,
        avg_hold_candles=statistics.fmean(t.held for t in records) if records else None,
        outcomes=outcomes,
        by_setup={k: _setup_stats([t for t in records if t.kind == k]) for k in SETUPS},
        recent=list(records[-keep_recent:]),
        cost_pct=cost_pct,
        coins=coins,
        records=list(records),
    )


def pool(stats: Sequence[BacktestStats], keep_recent: int = 12) -> BacktestStats | None:
    """The same rules over every scanned coin: a larger sample than any single coin."""
    stats = [s for s in stats if s.records or s.trades == 0]
    if not stats:
        return None
    starts = [s.period_start for s in stats if s.period_start]
    ends = [s.period_end for s in stats if s.period_end]
    return summarize(
        [t for s in stats for t in s.records], horizon=stats[0].horizon, setup_timeframe=stats[0].setup_timeframe,
        candles=sum(s.candles for s in stats), period_start=min(starts) if starts else None,
        period_end=max(ends) if ends else None, cost_pct=stats[0].cost_pct, keep_recent=keep_recent, coins=len(stats),
    )


# ----------------------------------------------------------------------------- verdict


def _describe(stats: BacktestStats) -> str:
    days = (stats.period_end - stats.period_start).total_seconds() / 86400 if stats.period_start and stats.period_end else 0
    pf = stats.profit_factor
    pf_text = "n/a" if pf is None else ("no losses" if math.isinf(pf) else f"{pf:.2f}")
    scope = f"{stats.coins} coins, " if stats.coins > 1 else ""
    return (
        f"backtest ({scope}{days:.0f} days): {stats.trades} trades, win rate {stats.win_rate or 0:.0f}%, "
        f"expectancy {stats.expectancy_r or 0:+.2f}R, profit factor {pf_text} (net of {stats.cost_pct:.2f}% costs)"
    )


def _passes(stats: BacktestStats, p: ScalpParams) -> bool:
    pf = stats.profit_factor
    return (stats.expectancy_r or 0.0) >= p.min_expectancy_r and (pf is None or pf >= p.min_profit_factor)


def evidence(
    stats: BacktestStats, kind: str, p: ScalpParams, pooled: BacktestStats | None = None
) -> tuple[SignalLabel, list[str], str]:
    """The strongest label the history supports for this setup, why, and which evidence decided
    ("coin", "pooled" or "none")."""
    coin_text = _describe(stats)
    if stats.trades < p.min_trades:
        if stats.trades and stats.total_r <= p.coin_max_loss_r:
            return SignalLabel.NO_TRADE, [f"these rules lost on this coin: {coin_text}"], "coin"
        if pooled is not None and pooled.trades >= p.pooled_min_trades:
            text = f"this coin: {stats.trades} trades (too few); same rules on all scanned coins: {_describe(pooled)}"
            if not _passes(pooled, p):
                return SignalLabel.NO_TRADE, [f"these rules did not pay across the scanned coins: {_describe(pooled)}"], "pooled"
            setup = pooled.by_setup.get(kind)
            if setup is not None and setup.trades >= p.setup_min_trades and (setup.expectancy_r or 0) <= 0:
                return SignalLabel.WATCH, [f"{text}; but the {kind} setup alone lost ({setup.trades} trades, "
                                           f"{setup.expectancy_r or 0:+.2f}R)"], "pooled"
            return SignalLabel.BUY, [text], "pooled"  # pooled evidence never supports STRONG BUY
        return SignalLabel.WATCH, [f"not enough evidence: {coin_text}; needs {p.min_trades}+ trades"], "none"
    if not _passes(stats, p):
        return SignalLabel.NO_TRADE, [f"these rules did not pay on this coin: {coin_text}"], "coin"
    setup = stats.by_setup.get(kind)
    if setup is not None and setup.trades >= p.setup_min_trades and (setup.expectancy_r or 0) <= 0:
        return SignalLabel.WATCH, [f"{coin_text}; but the {kind} setup alone lost "
                                   f"({setup.trades} trades, {setup.expectancy_r or 0:+.2f}R)"], "coin"
    exp, pf = stats.expectancy_r or 0.0, stats.profit_factor
    strong = (
        stats.trades >= p.strong_min_trades and exp >= p.strong_min_expectancy_r
        and (pf is None or pf >= p.strong_min_profit_factor) and (stats.win_rate or 0) >= p.strong_min_win_rate
    )
    return (SignalLabel.STRONG_BUY if strong else SignalLabel.BUY), [coin_text], "coin"


def net_reward_risk(entry: float, stop: float, target: float, cost_pct: float) -> float:
    cost = entry * cost_pct / 100.0
    return (target - entry - cost) / (entry - stop + cost)
