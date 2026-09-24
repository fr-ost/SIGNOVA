"""Risk checks and their effect on the signal label.

BLOCK (NO TRADE): the market cannot be traded safely at all (no order book, wide spread,
thin depth, tiny volume).
CAP (at most WATCH): the setup may be valid but not now (reward:risk too low, stop too
wide, price extended or overbought, trend filter, bear market, no 1H entry trigger yet,
Bitcoin's 4H trend down for an altcoin).
DOWNGRADE (at most BUY): conditions required for STRONG BUY are missing (including sellers
dominating the order book).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.analysis.features import IndicatorSnapshot
from app.analysis.regime import MarketRegimeResult
from app.core.enums import RiskSeverity, SignalLabel, TrendDirection
from app.core.formatting import fmt_price
from app.data.validation.orderbook import OrderBookSummary
from app.risk.params import RiskParams
from app.risk.plan import TradePlan

_TOLERANCE = 1e-9


@dataclass(frozen=True)
class RiskCheck:
    key: str
    name: str
    passed: bool
    severity: RiskSeverity
    detail: str


@dataclass
class RiskInputs:
    price: float | None
    quote_usd_rate: float | None
    order_book: OrderBookSummary | None
    volume_24h_quote: float | None
    pct_change_24h: float | None
    h4: IndicatorSnapshot | None
    d1: IndicatorSnapshot | None
    m15: IndicatorSnapshot | None
    trend_1d: TrendDirection | None
    trend_4h: TrendDirection | None
    plan: TradePlan | None
    market: MarketRegimeResult
    h1: IndicatorSnapshot | None = None
    is_btc: bool = False


def _usd(value: float | None, rate: float | None) -> float | None:
    return value * rate if value is not None and rate is not None else None


def evaluate_risk(i: RiskInputs, p: RiskParams) -> list[RiskCheck]:
    checks: list[RiskCheck] = []

    def add(key: str, name: str, passed: bool, severity: RiskSeverity, detail: str) -> None:
        checks.append(RiskCheck(key, name, passed, severity, detail))

    # -- liquidity (block) -------------------------------------------------------------
    book = i.order_book
    if book is None or not book.valid:
        reason = "order book unavailable" if book is None else f"order book invalid ({'; '.join(book.issues) or 'failed sanity checks'})"
        add("order_book", "Order book", False, RiskSeverity.BLOCK, reason)
    else:
        spread = book.spread_bps
        add(
            "spread",
            "Spread",
            spread is not None and spread <= p.max_spread_bps,
            RiskSeverity.BLOCK,
            f"spread {spread:.1f} bps (limit {p.max_spread_bps:g})" if spread is not None else "spread unknown",
        )
        depth = min(book.bid_depth_quote or 0.0, book.ask_depth_quote or 0.0)
        depth_usd = _usd(depth, i.quote_usd_rate)
        add(
            "depth",
            "Order book depth",
            depth_usd is not None and depth_usd >= p.min_depth_usd,
            RiskSeverity.BLOCK,
            f"thinnest side ${depth_usd:,.0f} within +/-{book.band_pct:g}% (minimum ${p.min_depth_usd:,.0f})"
            if depth_usd is not None
            else "order book depth in USD unknown",
        )
    volume_usd = _usd(i.volume_24h_quote, i.quote_usd_rate)
    if volume_usd is None:
        add("volume_24h", "24h volume", False, RiskSeverity.CAP, "24h exchange volume unknown")
    else:
        add(
            "volume_24h",
            "24h volume",
            volume_usd >= p.min_volume_24h_usd,
            RiskSeverity.BLOCK,
            f"${volume_usd:,.0f} traded in 24h on this market (minimum ${p.min_volume_24h_usd:,.0f})",
        )

    # -- trend and market (cap / downgrade) ----------------------------------------------
    down = [label for label, trend in (("1D", i.trend_1d), ("4H", i.trend_4h)) if trend == TrendDirection.DOWN]
    add(
        "trend_filter",
        "Trend filter",
        not down and i.trend_1d is not None and i.trend_4h is not None,
        RiskSeverity.CAP,
        f"{' and '.join(down)} trend is down: no spot long against it" if down
        else ("trend unavailable" if i.trend_1d is None or i.trend_4h is None else "1D and 4H trends are not down"),
    )
    aligned = i.trend_1d == TrendDirection.UP and i.trend_4h == TrendDirection.UP
    add(
        "trend_alignment",
        "Trend alignment",
        aligned,
        RiskSeverity.DOWNGRADE,
        "1D and 4H both trend up" if aligned else "STRONG BUY needs both 1D and 4H trending up",
    )
    cap = i.market.max_signal
    add(
        "market_regime",
        "Market regime",
        cap == SignalLabel.STRONG_BUY,
        RiskSeverity.CAP if cap.rank <= SignalLabel.WATCH.rank else RiskSeverity.DOWNGRADE,
        f"{i.market.regime.value} market: signals limited to {cap.value}" if cap != SignalLabel.STRONG_BUY
        else "bull market: no regime limit",
    )
    if not i.is_btc:
        btc4 = i.market.btc_trend_4h
        add(
            "btc_4h",
            "Bitcoin 4H trend",
            btc4 != TrendDirection.DOWN,
            RiskSeverity.CAP,
            "Bitcoin 4H trend is down: altcoins rarely rise against it, wait for Bitcoin to stabilise"
            if btc4 == TrendDirection.DOWN
            else (f"Bitcoin 4H trend {btc4.value.lower()}" if btc4 is not None else "Bitcoin 4H trend unavailable"),
        )
    percentile = i.h4.atr_pct_percentile if i.h4 else None
    add(
        "volatility",
        "Volatility",
        percentile is None or percentile < p.max_atr_percentile_strong,
        RiskSeverity.DOWNGRADE,
        f"4H ATR at the {percentile:.0f}th percentile of the last 200 candles" if percentile is not None
        else "4H volatility percentile unavailable",
    )

    # -- entry quality (cap) --------------------------------------------------------------
    if i.price is not None and i.h4 is not None:
        distance = i.h4.distance_atr(i.price, i.h4.ema20)
        if distance is not None:
            add(
                "extension",
                "Extension",
                distance <= p.max_extension_atr,
                RiskSeverity.CAP,
                f"price {distance:+.1f} ATR from the 4H EMA20 (limit +{p.max_extension_atr:g})",
            )
    h1 = i.h1
    confirmations: list[str] = []
    if h1 is not None:
        if h1.ema20 is not None and h1.close > h1.ema20:
            confirmations.append("1H close above EMA20")
        if h1.macd_hist is not None and h1.macd_hist_prev is not None and h1.macd_hist > h1.macd_hist_prev:
            confirmations.append("1H MACD histogram rising")
        if h1.rsi14 is not None and h1.rsi14_prev is not None and h1.rsi14 > h1.rsi14_prev:
            confirmations.append("1H RSI rising")
    add(
        "entry_trigger",
        "Entry trigger",
        len(confirmations) >= p.min_trigger_confirmations,
        RiskSeverity.CAP,
        (f"1H momentum confirms the entry: {', '.join(confirmations)}" if len(confirmations) >= p.min_trigger_confirmations
         else "no 1H entry trigger yet: wait until the 1H closes above its EMA20 with MACD or RSI turning up"
         + (f" (only {', '.join(confirmations)})" if confirmations else "")),
    )
    if book is not None and book.valid and book.imbalance is not None:
        add(
            "book_pressure",
            "Order book pressure",
            book.imbalance > p.min_book_imbalance_strong,
            RiskSeverity.DOWNGRADE,
            f"order book imbalance {book.imbalance:+.2f} within +/-{book.band_pct:g}%"
            + ("" if book.imbalance > p.min_book_imbalance_strong else ": sellers dominate, no STRONG BUY"),
        )
    rsi4 = i.h4.rsi14 if i.h4 else None
    rsi1d = i.d1.rsi14 if i.d1 else None
    overbought = (rsi4 is not None and rsi4 > p.max_rsi_4h) or (rsi1d is not None and rsi1d > p.max_rsi_1d)
    add(
        "overbought",
        "Overbought",
        not overbought,
        RiskSeverity.CAP,
        f"RSI 4H {rsi4 or 0:.0f} / 1D {rsi1d or 0:.0f} (limits {p.max_rsi_4h:g} / {p.max_rsi_1d:g})",
    )
    rsi15 = i.m15.rsi14 if i.m15 else None
    add(
        "short_term_spike",
        "Short-term spike",
        rsi15 is None or rsi15 <= p.max_rsi_15m_strong,
        RiskSeverity.DOWNGRADE,
        f"15m RSI {rsi15:.0f}: wait for a cool-off or use the lower part of the entry zone"
        if rsi15 is not None and rsi15 > p.max_rsi_15m_strong
        else f"15m RSI {rsi15 or 0:.0f} (STRONG BUY limit {p.max_rsi_15m_strong:g})",
    )
    change = i.pct_change_24h
    add(
        "move_24h",
        "24h move",
        change is None or change <= p.max_change_24h_pct,
        RiskSeverity.CAP,
        f"{change:+.1f}% in 24h (limit +{p.max_change_24h_pct:g}%)" if change is not None else "24h change unknown",
    )

    # -- trade plan (cap / downgrade) -----------------------------------------------------
    plan = i.plan
    if plan is None:
        add("plan", "Trade plan", False, RiskSeverity.CAP, "no valid long setup to plan (entry, stop and targets)")
        return checks
    room = plan.room_to_resistance_r
    add(
        "resistance_room",
        "Room to resistance",
        room is None or room >= p.min_room_r,
        RiskSeverity.CAP,
        "no resistance overhead (price discovery)" if room is None
        else f"nearest resistance {fmt_price(plan.nearest_resistance)} is {room:.2f}R above the entry "
        f"(minimum {p.min_room_r:g}R){'' if room >= p.min_room_r else ': wait for a breakout above it'}",
    )
    add(
        "resistance_room_strong",
        "Room for STRONG BUY",
        room is None or room >= p.strong_min_room_r,
        RiskSeverity.DOWNGRADE,
        "no resistance overhead" if room is None
        else f"nearest resistance {room:.2f}R above the entry (STRONG BUY needs {p.strong_min_room_r:g}R)",
    )
    tp2 = plan.targets[1]
    rr_detail = (
        f"net {plan.reward_risk:.2f}R to TP2 {fmt_price(tp2.price)} ({tp2.basis}); minimum {p.min_reward_risk:g}R"
        + (f"; reaches the minimum at an entry of {fmt_price(plan.better_entry_below)} or lower"
           if plan.better_entry_below else "")
    )
    add("reward_risk", "Reward:risk", plan.reward_risk >= p.min_reward_risk - _TOLERANCE, RiskSeverity.CAP, rr_detail)
    add(
        "reward_risk_strong",
        "Reward:risk for STRONG BUY",
        plan.reward_risk >= p.strong_min_reward_risk - _TOLERANCE,
        RiskSeverity.DOWNGRADE,
        f"net {plan.reward_risk:.2f}R (STRONG BUY needs {p.strong_min_reward_risk:g}R)",
    )
    add(
        "stop_distance",
        "Stop distance",
        plan.stop_distance_pct <= p.max_stop_pct,
        RiskSeverity.CAP,
        f"stop {plan.stop_distance_pct:.1f}% below the entry (limit {p.max_stop_pct:g}%)",
    )
    return checks


def apply_risk(label: SignalLabel, checks: Sequence[RiskCheck]) -> SignalLabel:
    for check in checks:
        if check.passed:
            continue
        if check.severity == RiskSeverity.BLOCK:
            label = SignalLabel.NO_TRADE
        elif check.severity == RiskSeverity.CAP:
            label = label.cap(SignalLabel.WATCH)
        else:
            label = label.cap(SignalLabel.BUY)
    return label
