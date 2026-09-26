"""Futures signals (Phase 12): long AND short setups for USDT perpetuals, and safe leverage.

Shorts use a mirror: the candles are inverted (price -> K / price, so the high becomes the low
and a downtrend becomes an uptrend) and the market's taker buying becomes its taker selling.
Every long strategy of the library (app.analysis.strategies), its backtest and its exits then
describe the mirrored short exactly: a "close above the 55-candle high" on the mirror is a close
below the 55-candle low on the real chart, a trailing stop under the mirror's highs is a stop
above the real lows. Levels are mapped back with the same K. Returns on the mirror differ from
a real short only in the second order (a 1% move: 0.01%), far below the costs.

Signals are computed on the spot price (the index the perpetual's mark price follows), with
futures costs: taker fees and slippage on both sides plus a funding allowance for the holding
time. Each side of each strategy must pass the same walk-forward research as the spot engine.

Leverage never changes how much a stop loses: the position is sized so the stop costs the
risk you set (1% of equity by default); leverage only sets the margin. The plan chooses the
highest leverage (up to your cap) whose estimated liquidation price stays clearly beyond the
stop, so the stop always triggers long before liquidation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from app.analysis import scalp as sc
from app.analysis import strategies as st
from app.core.enums import SignalLabel
from app.core.formatting import fmt_price as _fmt
from app.data.normalization.schemas import Candle

SIDES = ("long", "short")
BASE_FUNDING_PCT_8H = 0.01  # the usual perpetual funding rate, charged in backtests (pessimistic for both sides)
LIQ_BUFFER_MIN_PCT = 1.0  # the liquidation price must be at least this far beyond the stop...
LIQ_BUFFER_STOP_SHARE = 0.5  # ...and at least half the stop distance beyond it

SHORT_RULES = {
    "pullback": "downtrend rally to the EMA20 with RSI back up, then a bearish close below the previous candle's low",
    "breakout": "close below the 20-candle low on 1.8x volume after a quiet period (breakdown)",
    "donchian": "close below the lowest low of the previous 55 candles with the higher timeframe down (Turtle breakdown)",
    "ema_momentum": "EMA9 crosses below EMA21, price below EMA50, ADX 18+",
    "rsi2": "price below EMA200, 2-period RSI above 90: sell the short-term bounce in a long-term downtrend",
    "bb_revert": "a close back below the upper Bollinger band after closing above it, price below EMA200",
    "inside_bar": "an inside bar in a downtrend, then a close below the mother bar's low",
    "bos": "a lower swing high, then a close below the last swing low (break of structure)",
}


def label_text(side: str, label: SignalLabel) -> str:
    """Futures wording: LONG / STRONG LONG / SHORT / STRONG SHORT, else WATCH / NO TRADE."""
    if label == SignalLabel.BUY:
        return "LONG" if side == "long" else "SHORT"
    if label == SignalLabel.STRONG_BUY:
        return "STRONG LONG" if side == "long" else "STRONG SHORT"
    return label.value


# ----------------------------------------------------------------------------- the mirror


def mirror_k(candles: Sequence[Candle]) -> float:
    """A constant that keeps mirrored prices near real prices (the last close maps to itself)."""
    last = candles[-1].close if candles else 1.0
    return last * last


def invert_candles(candles: Sequence[Candle], k: float) -> list[Candle]:
    out = []
    for c in candles:
        if c.low <= 0 or c.high <= 0 or c.open <= 0 or c.close <= 0:
            continue
        sell = (c.volume - c.taker_buy_base) if c.taker_buy_base is not None else None
        out.append(replace(c, open=k / c.open, high=k / c.low, low=k / c.high, close=k / c.close,
                           taker_buy_base=max(0.0, sell) if sell is not None else None))
    return out


def mirror_price(value: float | None, k: float) -> float | None:
    return k / value if value else value


def unmirror(cand: sc.Candidate, k: float) -> sc.Candidate:
    """A short found on the mirror, in real prices (stop above the entry, targets below)."""
    entry, stop = k / cand.entry, k / cand.stop
    return replace(cand, entry=entry, stop=stop, tp1=k / cand.tp1, tp2=k / cand.tp2,
                   level=(k / cand.level) if cand.level else None,
                   risk_pct=(stop - entry) / entry * 100.0, atr=cand.atr * entry / cand.entry)


# ----------------------------------------------------------------------------- one side of one coin


@dataclass
class SideAnalysis:
    side: str
    series: sc.ScalpSeries
    k: float  # mirror constant (1.0 for longs)
    records: dict[str, list[sc.TradeRecord]]  # strategy -> trades over the history (mirror space for shorts)
    now: dict[str, sc.Candidate]  # strategy -> a fresh signal, in REAL prices
    context_ok: bool  # the trend context allows this side now
    pending: dict[str, Any] | None = None  # conditional levels while waiting (real prices)


def analyze_side(side: str, series: sc.ScalpSeries, k: float, p: sc.ScalpParams, *, is_btc: bool = False,
                 symbol: str = "") -> SideAnalysis:
    classic = sc.backtest(series, p, is_btc=is_btc, symbol=symbol)
    records: dict[str, list[sc.TradeRecord]] = {key: [t for t in classic.records if t.kind == key] for key in sc.SETUPS}
    for key in st.LIBRARY:
        records[key] = st.backtest_key(series, key, p, is_btc=is_btc, symbol=symbol)
    now: dict[str, sc.Candidate] = {}
    last = len(series) - 1
    for i in range(last, max(sc.WARMUP - 1, last - p.fresh_candles), -1):
        cand, _ = sc.evaluate_at(series, i, p, is_btc=is_btc)
        if cand is not None:
            now[cand.kind] = cand
            break
    for key in st.LIBRARY:
        cand = st.latest(series, key, p, is_btc=is_btc)
        if cand is not None:
            now[key] = cand
    if side == "short":
        now = {key: unmirror(c, k) for key, c in now.items()}
    context_ok = bool(series.trend_up[last] and not series.filter_down[last] and (is_btc or not series.btc_down[last]))
    pending = None
    if not now:
        pend = sc.pending_plan(series, last, p, is_btc=is_btc)
        if pend is not None:
            pending = pending_out(side, pend, k, series.profile)
    return SideAnalysis(side, series, k, records, now, context_ok, pending)


def pending_out(side: str, pend: sc.Pending, k: float, prof: sc.ScalpProfile) -> dict[str, Any]:
    tf = prof.setup.label
    if side == "long":
        trigger, stop, tp1, tp2 = pend.trigger, pend.stop, pend.tp1, pend.tp2
    else:
        trigger, stop, tp1, tp2 = k / pend.trigger, k / pend.stop, k / pend.tp1, k / pend.tp2
    t = _fmt(trigger)
    if pend.kind == "dip":
        text = (f"extended above the {tf} EMA20: wait for a dip toward {t}, then a close above the previous candle's high"
                if side == "long" else
                f"extended below the {tf} EMA20: wait for a bounce toward {t}, then a close below the previous candle's low")
    elif pend.kind == "breakout":
        text = (f"long only if a {tf} candle closes above the 20-candle high {t} on strong volume" if side == "long"
                else f"short only if a {tf} candle closes below the 20-candle low {t} on strong volume")
    else:
        text = (f"long only if a {tf} candle closes above {t} (this candle's high)" if side == "long"
                else f"short only if a {tf} candle closes below {t} (this candle's low)")
    if not pend.fees_ok:
        text += f"; at today's volatility the stop ({pend.risk_pct:.2f}%) is too tight for the costs"
    kind = pend.kind if side == "long" else {"dip": "bounce", "pullback": "rally", "breakout": "breakdown"}[pend.kind]
    return {"side": side, "kind": kind, "setup": pend.kind, "trigger": trigger, "stop": stop, "tp1": tp1, "tp2": tp2,
            "risk_pct": abs(stop - trigger) / trigger * 100.0, "fees_ok": pend.fees_ok, "text": text}


def reasons_for(side: str, cand: sc.Candidate, prof: sc.ScalpProfile) -> list[str]:
    """Why the setup fired, in real prices (the mirror's own wording would be upside down)."""
    if side == "long":
        return list(cand.reasons)
    return [f"{prof.setup.label} {SHORT_RULES.get(cand.kind, cand.kind)}",
            f"{prof.trend.label} trend down, {prof.filter.label} not up; short entry {_fmt(cand.entry)}, stop {_fmt(cand.stop)}"]


def exit_text(side: str, key: str, cand: sc.Candidate, p: sc.ScalpParams) -> str:
    """How to take profit on the real chart."""
    if side == "long":
        if key in st.LIBRARY:
            return st.exit_text(st.STRATEGIES[key].exit, cand.entry, cand.stop, cand.level)
        return (f"sell {p.tp1_share * 100:.0f}% at TP1 {_fmt(cand.tp1)}" + (", then move the stop to the entry" if p.breakeven else "")
                + (f"; the rest at TP2 {_fmt(cand.tp2)}" if p.tp1_share < 1 else ""))
    spec = st.STRATEGIES[key].exit if key in st.LIBRARY else st.CLASSIC_EXIT
    r = cand.stop - cand.entry
    parts = []
    if key in st.LIBRARY and spec.mode == "targets" and spec.tp1_r is None and cand.level:
        parts.append(f"buy back all at {_fmt(cand.level)} (the middle band)")
    tp1_r = spec.tp1_r if key in st.LIBRARY else p.tp1_r
    share = spec.tp1_share if key in st.LIBRARY else p.tp1_share
    if tp1_r is not None:
        parts.append(f"buy back {'all' if share >= 1 else f'{share * 100:.0f}%'} at {_fmt(cand.tp1)}")
        if (spec.breakeven if key in st.LIBRARY else p.breakeven):
            parts.append("then move the stop to the entry")
    tp2_r = spec.tp2_r if key in st.LIBRARY else p.tp2_r
    if tp2_r is not None and share < 1:
        parts.append(f"the rest at {_fmt(cand.tp2)}")
    if key in st.LIBRARY and spec.trail == "chandelier":
        parts.append(f"trail the rest: stop at the lowest low since entry plus {spec.trail_atr:g} ATR (lower it after each candle)")
    elif key in st.LIBRARY and spec.trail == "donchian":
        parts.append(f"trail: stop at the highest high of the last {spec.trail_len} candles (lower it after each candle)")
    if key in st.LIBRARY and spec.exit_ma:
        parts.append(f"buy back when a candle closes below its {spec.exit_ma}-period average")
    if not parts:
        parts.append(f"target {_fmt(cand.entry - 2 * r)}")
    return "; ".join(parts)


# ----------------------------------------------------------------------------- leverage


@dataclass
class LeveragePlan:
    side: str
    entry: float
    stop: float
    stop_distance_pct: float
    cost_pct: float  # fees and slippage, round trip
    funding_pct: float  # expected funding over the holding time (+ you pay, - you receive)
    loss_at_stop_pct: float  # of the position, stop distance + costs (+ funding you pay)
    risk_pct_of_equity: float
    notional_pct_of_equity: float  # position size
    max_safe_leverage: float
    leverage: int
    margin_pct_of_equity: float
    liquidation_price: float
    liquidation_distance_pct: float
    maintenance_margin_pct: float
    equity: float | None = None
    notional_usd: float | None = None
    margin_usd: float | None = None
    loss_at_stop_usd: float | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def liquidation_price(side: str, entry: float, leverage: float, mmr_pct: float) -> float:
    """Isolated margin estimate: long entry x (1 - 1/L + MMR), short entry x (1 + 1/L - MMR)."""
    m = mmr_pct / 100.0
    return entry * (1.0 - 1.0 / leverage + m) if side == "long" else entry * (1.0 + 1.0 / leverage - m)


def leverage_plan(side: str, entry: float, stop: float, *, cost_pct: float, funding_rate_pct_8h: float | None,
                  hold_hours: float, risk_pct_equity: float, max_leverage: int, mmr_pct: float,
                  equity: float | None = None) -> LeveragePlan:
    stop_pct = abs(entry - stop) / entry * 100.0
    rate = funding_rate_pct_8h if funding_rate_pct_8h is not None else BASE_FUNDING_PCT_8H
    funding = rate * hold_hours / 8.0 * (1.0 if side == "long" else -1.0)
    loss = stop_pct + cost_pct + max(0.0, funding)
    notional = risk_pct_equity / loss * 100.0 if loss > 0 else 0.0
    buffer = max(LIQ_BUFFER_STOP_SHARE * stop_pct, LIQ_BUFFER_MIN_PCT)
    safe = 1.0 / ((stop_pct + buffer) / 100.0 + mmr_pct / 100.0)
    lev = int(max(1, min(max_leverage, math.floor(safe))))
    notes: list[str] = []
    margin = notional / lev
    if margin > 100.0:  # even the full equity as margin is not enough at this leverage: smaller position
        notional = 100.0 * lev
        margin = 100.0
        notes.append(f"position capped by margin: the stop now costs {notional * loss / 100.0:.2f}% of equity")
    liq = liquidation_price(side, entry, lev, mmr_pct)
    liq_dist = abs(liq - entry) / entry * 100.0
    if lev < max_leverage and lev == math.floor(safe):
        notes.append(f"leverage limited to {lev}x so the liquidation price stays well beyond the stop")
    if funding > 0.05:
        notes.append(f"funding costs about {funding:.3f}% over the holding time (you pay it as a {side})")
    elif funding < -0.05:
        notes.append(f"funding pays you about {-funding:.3f}% over the holding time")
    plan = LeveragePlan(
        side=side, entry=entry, stop=stop, stop_distance_pct=stop_pct, cost_pct=cost_pct, funding_pct=funding,
        loss_at_stop_pct=loss, risk_pct_of_equity=notional * loss / 100.0, notional_pct_of_equity=notional,
        max_safe_leverage=safe, leverage=lev, margin_pct_of_equity=margin, liquidation_price=liq,
        liquidation_distance_pct=liq_dist, maintenance_margin_pct=mmr_pct, equity=equity, notes=notes,
    )
    if equity:
        plan.notional_usd = equity * notional / 100.0
        plan.margin_usd = plan.notional_usd / lev
        plan.loss_at_stop_usd = plan.notional_usd * loss / 100.0
    return plan
