"""Evidence board (Phase 10): what, besides the chart setup, speaks for or against a long now.

Groups and factors (each says +1 supports buying, -1 argues against, 0 neutral, with a
strength 0..1):

* derivatives: funding, open interest versus price, the crowd's long/short ratio, top traders'
  ("whales'") positioning, futures taker flow, recent liquidations, the estimated liquidation map;
* order flow: spot taker buying, CVD divergence, volume versus its average, order book,
  strength against Bitcoin;
* news and hype: critical headlines (hack, delisting...), headline tone or the AI's reading,
  catalysts, buzz and trending;
* market: Bitcoin's trend, breadth, leverage across the market, Fear & Greed, stablecoin
  supply, altcoin season;
* events: token unlocks, whale exchange flows (BTC/ETH).

The confluence score is the weighted average x 100 (-100..+100) of the factors that have data;
missing data counts neither way. The board can only lower a signal, never raise it (the
backtest decides how high a label can go). In "filter" mode, with at least MIN_FACTORS factors:
a veto or a strongly negative board caps a buy at WATCH, and a negative board turns STRONG BUY
into BUY. The weights are documented priors; app.analysis.evidence_learn measures every
factor on real outcomes and can take over as the filter once its model validates.

Futures shorts (Phase 12) use the same board with side="short": every factor's direction
flips (crowded longs, extreme greed or a critical headline now argue FOR the trade), the
long-only vetoes do not apply, and short vetoes do: crowded shorts while open interest rises,
a short squeeze in progress, and a major positive catalyst. The liquidation map is read from
the short's side (short liquidations above are the squeeze risk, long liquidations below the fuel).
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.analysis import liqmap as lm
from app.analysis.regime import MarketRegimeResult
from app.core.enums import SignalLabel, TrendDirection
from app.core.formatting import fmt_price
from app.data.derivatives import Point
from app.data.normalization.schemas import Candle

EVIDENCE_VERSION = "evidence-1.0"
MIN_FACTORS = 4
STRONG_AGAINST = -25.0
AGAINST = -8.0
SUPPORTIVE = 8.0
STRONG_FOR = 25.0

# Prior weights: how much each factor counts before the learner has measured anything.
PRIOR_WEIGHTS: dict[str, float] = {
    "funding": 1.0, "oi_trend": 1.0, "crowd": 0.8, "whales": 1.0, "futures_flow": 0.8, "liquidations": 0.8,
    "liq_map": 0.7, "spot_flow": 1.0, "cvd": 0.8, "volume": 0.6, "book": 0.4, "rel_strength": 1.0,
    "news_critical": 1.5, "news_tone": 0.6, "catalyst": 0.4, "hype": 0.7, "btc_trend": 1.0, "breadth": 0.6,
    "market_leverage": 0.6, "fear_greed": 0.5, "stablecoins": 0.3, "altseason": 0.4, "unlock": 1.0,
    "exchange_flow": 0.5,
}
GROUPS = {
    "derivatives": "Futures positioning", "flow": "Order flow", "news": "News & hype", "market": "Market", "events": "Events",
}
LABELS = {
    "funding": "Funding rate", "oi_trend": "Open interest vs price", "crowd": "Crowd long/short", "whales": "Top traders (whales)",
    "futures_flow": "Futures taker flow", "liquidations": "Recent liquidations", "liq_map": "Liquidation map (est.)",
    "spot_flow": "Spot taker buying", "cvd": "Volume delta (CVD)", "volume": "Volume vs average", "book": "Order book",
    "rel_strength": "Strength vs Bitcoin", "news_critical": "Critical news", "news_tone": "News tone", "catalyst": "Catalysts",
    "hype": "Hype & attention", "btc_trend": "Bitcoin trend", "breadth": "Market breadth", "market_leverage": "Market leverage",
    "fear_greed": "Fear & Greed", "stablecoins": "Stablecoin supply", "altseason": "Altcoin season", "unlock": "Token unlock",
    "exchange_flow": "Whale exchange flows",
}

CRITICAL = re.compile(
    r"\b(hack(ed|s)?|exploit(ed|s)?|breach(ed)?|drain(ed)?|stolen|theft|delist(s|ed|ing)?|insolven\w*|bankrupt\w*|"
    r"rug ?pull\w*|halt(s|ed)? withdrawals?|withdrawals? (halted|paused|suspended)|suspend(s|ed)? (trading|withdrawals)|"
    r"depeg\w*|chain halt\w*|network (outage|halt\w*)|sec (sues|charges)|charged by|ponzi|fraud)\b",
    re.IGNORECASE,
)
CATALYST = re.compile(
    r"\b(etf (approv\w*|launch\w*|inflows?)|approv(es|ed|al) (of )?(an? )?etf|lists?|listing|listed|mainnet|"
    r"upgrade|hard ?fork|partnership|partners with|buyback|token burn|burns?|integrat(es|ion)|adopt(s|ion)|"
    r"treasury (buys|adds)|launch(es|ed)?)\b",
    re.IGNORECASE,
)
MAJOR_EXCHANGES = re.compile(r"\b(binance|coinbase|upbit|robinhood|kraken|okx|bybit)\b", re.IGNORECASE)


# ----------------------------------------------------------------------------- data classes


@dataclass
class Factor:
    key: str
    group: str
    label: str
    direction: int  # +1 supports buying, -1 argues against, 0 neutral
    strength: float  # 0..1
    weight: float
    value: str  # the measured value, short
    detail: str  # what it means
    source: str
    veto: str | None = None

    @property
    def signed(self) -> float:
        return float(self.direction) * self.strength


@dataclass
class Evidence:
    version: str
    computed_at: datetime
    symbol: str
    horizon: str
    score: float | None  # -100..+100; None without data
    grade: str  # strong_for | for | neutral | against | strong_against | thin
    factors: list[Factor]
    missing: list[str]  # factor keys without data
    vetoes: list[str]
    liq_map: dict[str, Any] | None = None
    stop_hint: float | None = None  # a stop beyond the nearest estimated liquidation zone at the stop
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    side: str = "long"  # long | short (futures): the trade this board judges

    @property
    def thin(self) -> bool:
        return len(self.factors) < MIN_FACTORS

    def features(self) -> dict[str, float]:
        """Signed factor values (0 when missing): the learner's inputs."""
        values = {k: 0.0 for k in PRIOR_WEIGHTS}
        for f in self.factors:
            values[f.key] = f.signed
        return values

    def top(self, direction: int, limit: int = 2) -> list[Factor]:
        rows = [f for f in self.factors if f.direction == direction]
        return sorted(rows, key=lambda f: f.weight * f.strength, reverse=True)[:limit]

    def summary(self) -> str:
        if self.score is None:
            return "no evidence data"
        head = f"evidence {self.score:+.0f} ({GRADE_TEXT[self.grade]})"
        pro = ", ".join(f.label.lower() for f in self.top(1))
        con = ", ".join(f.label.lower() for f in self.top(-1))
        parts = [head] + ([f"for: {pro}"] if pro else []) + ([f"against: {con}"] if con else [])
        return "; ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["computed_at"] = self.computed_at
        data["thin"] = self.thin
        data["summary"] = self.summary()
        data["groups"] = GROUPS
        return data


GRADE_TEXT = {
    "strong_for": "strong support", "for": "supportive", "neutral": "mixed", "against": "headwinds",
    "strong_against": "strong headwinds", "thin": "too little data",
}


@dataclass
class EvidenceInputs:
    symbol: str
    now: datetime
    price: float | None
    horizon: str = "swing"  # swing | 15m | 1h | 4h | 1d
    side: str = "long"  # long | short (futures)
    entry: float | None = None
    stop: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    h1: Sequence[Candle] = ()
    setup: Sequence[Candle] = ()
    d1: Sequence[Candle] = ()
    btc_h1: Sequence[Candle] = ()
    volume_24h_quote: float | None = None  # in the quote asset, like the daily candles' quote volume
    change_24h_pct: float | None = None
    rsi: float | None = None
    atr_pct: float | None = None
    book_imbalance: float | None = None
    deriv: Any = None  # app.services.derivatives.DerivativesSnapshot
    market_deriv: dict[str, Any] | None = None
    headlines: Sequence[tuple[datetime | None, str, str]] = ()  # (published, title, keyword tone)
    ai_news: dict[str, Any] | None = None  # {"impact": -2..2, "critical": bool, "reason": str}
    mentions_24h: int | None = None
    mentions_per_day: float | None = None
    trending_rank: int | None = None
    market: MarketRegimeResult | None = None
    stablecoin_change_7d: float | None = None
    altseason: int | None = None
    unlock: dict[str, Any] | None = None  # {"days": float, "pct": float, "usd": float | None}
    exchange_flow: dict[str, Any] | None = None  # {"inflow": usd, "outflow": usd}


# ----------------------------------------------------------------------------- helpers


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _usd(x: float | None) -> str:
    if x is None:
        return "n/a"
    ax = abs(x)
    if ax >= 1e9:
        return f"${x / 1e9:.2f}B"
    if ax >= 1e6:
        return f"${x / 1e6:.1f}M"
    if ax >= 1e3:
        return f"${x / 1e3:.0f}K"
    return f"${x:.0f}"


def _change(points: Sequence[Point], hours: float) -> tuple[float, float] | None:
    """(latest, percent change over `hours`) of a time series."""
    if len(points) < 2:
        return None
    last = points[-1]
    target = last.time - timedelta(hours=hours)
    earlier = [p for p in points if p.time <= target]
    ref = earlier[-1] if earlier else None
    if ref is None or ref.value == 0 or (last.time - ref.time) < timedelta(hours=hours * 0.75):
        return None
    return last.value, (last.value / ref.value - 1.0) * 100.0


def _at_or_before(points: Sequence[Point], t: datetime) -> Point | None:
    earlier = [p for p in points if p.time <= t]
    return earlier[-1] if earlier else None


def taker_share(candles: Sequence[Candle], n: int) -> float | None:
    rows = [c for c in candles[-n:] if c.taker_buy_base is not None and c.volume > 0]
    if len(rows) < max(2, n // 2):
        return None
    vol = math.fsum(c.volume for c in rows)
    return math.fsum(c.taker_buy_base or 0.0 for c in rows) / vol if vol > 0 else None


def cvd_norm(candles: Sequence[Candle], n: int) -> float | None:
    """Net taker buying over the last n candles as a share of volume (-1..+1)."""
    rows = [c for c in candles[-n:] if c.taker_buy_base is not None and c.volume > 0]
    if len(rows) < max(2, n // 2):
        return None
    vol = math.fsum(c.volume for c in rows)
    return math.fsum(2.0 * (c.taker_buy_base or 0.0) - c.volume for c in rows) / vol if vol > 0 else None


def pct_change(candles: Sequence[Candle], n: int) -> float | None:
    if len(candles) <= n or candles[-n - 1].close <= 0:
        return None
    return (candles[-1].close / candles[-n - 1].close - 1.0) * 100.0


def oi_in_coins(deriv: Any, h1: Sequence[Candle]) -> list[Point]:
    """Open-interest history in coins (OKX publishes USD: divided by the hour's close)."""
    points = list(getattr(deriv, "oi_history", []) or [])
    if getattr(deriv, "oi_unit", "coin") != "usd":
        return points
    if not h1:
        return []
    closes = sorted(h1, key=lambda c: c.close_time)
    out = []
    k = 0
    for p in points:
        while k + 1 < len(closes) and closes[k + 1].close_time <= p.time:
            k += 1
        if closes[k].close_time <= p.time + timedelta(hours=1) and closes[k].close > 0:
            out.append(Point(p.time, p.value / closes[k].close))
    return out


# ----------------------------------------------------------------------------- the board


class _Board:
    def __init__(self, i: EvidenceInputs, weights: dict[str, float]) -> None:
        self.i = i
        self.w = weights
        self.factors: list[Factor] = []
        self.vetoes: list[str] = []
        self.notes: list[str] = []
        self.map_data: dict[str, Any] | None = None
        self.stop_hint: float | None = None
        self.short = i.side == "short"

    def add(self, key: str, group: str, direction: int, strength: float, value: str, detail: str, source: str,
            veto: str | None = None, short_veto: str | None = None) -> None:
        """`direction` is from a buyer's point of view; a short board flips it. `veto` blocks longs,
        `short_veto` blocks shorts."""
        if self.short:
            direction, veto = -direction, short_veto
        strength = _clip(strength) if direction else 0.0
        self.factors.append(Factor(key, group, LABELS[key], direction, round(strength, 3), self.w.get(key, 0.5), value,
                                   detail, source, veto))
        if veto:
            self.vetoes.append(veto)

    @property
    def is_btc(self) -> bool:
        return self.i.symbol.upper() == "BTC"

    def price_change_24h(self) -> float | None:
        ch = pct_change(self.i.h1, 24)
        return ch if ch is not None else self.i.change_24h_pct

    # --- derivatives ---------------------------------------------------------------

    def funding(self) -> None:
        d = self.i.deriv
        f = getattr(d, "funding_pct", None)
        if f is None:
            return
        src = f"{d.sources.get('funding', 'futures')} perpetual"
        hist = [p.value for p in (d.funding_history or [])[-21:]]
        avg = statistics.fmean(hist) if hist else None
        value = f"{f:+.4f}%/8h" + (f", 7d avg {avg:+.4f}%" if avg is not None else "")
        oi = _change(oi_in_coins(d, self.i.h1), 24)
        leverage_up = oi is not None and oi[1] >= 15
        if f >= 0.10:
            veto = "funding extreme with open interest up {:.0f}% in 24h: crowded, leveraged longs".format(oi[1]) if leverage_up and oi else None
            self.add("funding", "derivatives", -1, 1.0, value, "longs pay a very high rate to stay in: crowded, prone to a long squeeze", src, veto)
        elif f >= 0.05:
            self.add("funding", "derivatives", -1, 0.6, value, "funding high: longs crowded", src)
        elif f >= 0.03:
            self.add("funding", "derivatives", -1, 0.3, value, "funding above normal (0.01%)", src)
        elif f <= -0.03:
            up = (self.price_change_24h() or 0.0) >= 0
            shorts_piling = f <= -0.05 and oi is not None and oi[1] >= 10
            self.add("funding", "derivatives", 1 if up else 0, 0.5 if up else 0.0, value,
                     "shorts pay to stay short while the price holds: fuel for a short squeeze" if up
                     else "shorts pay to stay short in a falling market: bearish crowd, but squeeze-prone", src,
                     short_veto=(f"funding {f:+.3f}% with open interest up {oi[1]:.0f}% in 24h: crowded, leveraged shorts "
                                 "(squeeze risk)") if shorts_piling and oi else None)
        else:
            self.add("funding", "derivatives", 0, 0.0, value, "funding normal: no crowding", src)

    def oi_trend(self) -> None:
        d = self.i.deriv
        oi = _change(oi_in_coins(d, self.i.h1), 24) if d is not None else None
        if oi is None:
            return
        _, chg = oi
        price = self.price_change_24h()
        if price is None:
            return
        src = f"{d.sources.get('oi_history', 'futures')} open interest"
        value = f"OI {chg:+.1f}% / price {price:+.1f}% (24h)"
        if chg >= 25:
            self.add("oi_trend", "derivatives", -1, 0.6, value, "leverage building fast: moves like this often unwind sharply", src)
        elif price >= 0.5 and chg >= 3:
            self.add("oi_trend", "derivatives", 1, chg / 15, value, "price and open interest rising together: new money behind the move", src)
        elif price >= 0.5 and chg <= -3:
            self.add("oi_trend", "derivatives", -1, 0.3, value, "rally while open interest falls: mostly short covering, weaker follow-through", src)
        elif price <= -0.5 and chg >= 3:
            self.add("oi_trend", "derivatives", -1, chg / 15, value, "shorts adding into the drop", src)
        elif price <= -0.5 and chg <= -3:
            self.add("oi_trend", "derivatives", 0, 0.0, value, "longs closing into the drop (deleveraging)", src)
        else:
            self.add("oi_trend", "derivatives", 0, 0.0, value, "open interest steady", src)

    def crowd(self) -> None:
        d = self.i.deriv
        pts = getattr(d, "long_share", None) or []
        if not pts:
            return
        share = pts[-1].value
        src = f"{d.sources.get('long_share', 'futures')} accounts"
        value = f"{share * 100:.0f}% of accounts long"
        if share >= 0.70:
            self.add("crowd", "derivatives", -1, (share - 0.62) / 0.15, value, "the crowd is heavily long: contrarian warning", src)
        elif share >= 0.62:
            self.add("crowd", "derivatives", -1, 0.3, value, "the crowd leans long", src)
        elif share <= 0.40:
            self.add("crowd", "derivatives", 1, (0.48 - share) / 0.15, value, "the crowd is short: fuel for a squeeze if the price rises", src)
        else:
            self.add("crowd", "derivatives", 0, 0.0, value, "the crowd is balanced", src)

    def whales(self) -> None:
        d = self.i.deriv
        top = getattr(d, "top_long_share", None) or []
        if len(top) < 2:
            return
        t_now = top[-1].value
        ref = _at_or_before(top, top[-1].time - timedelta(hours=24))
        delta = t_now - ref.value if ref is not None else 0.0
        crowd = (getattr(d, "long_share", None) or [])
        gap = t_now - crowd[-1].value if crowd else 0.0
        s = _clip(delta / 0.08 + gap / 0.15, -1.0, 1.0)
        direction = 1 if s >= 0.15 else -1 if s <= -0.15 else 0
        value = f"top traders {t_now * 100:.0f}% long ({delta * 100:+.0f} pts in 24h)"
        if direction > 0:
            detail = "the largest accounts are adding longs" + (" while the crowd is less long" if gap > 0.03 else "")
        elif direction < 0:
            detail = "the largest accounts are cutting longs" + (" while the crowd stays long" if gap < -0.03 else "")
        else:
            detail = "the largest accounts are not moving"
        self.add("whales", "derivatives", direction, abs(s), value, detail, f"{d.sources.get('top_long_share', 'futures')} top-trader positions")

    def futures_flow(self) -> None:
        d = self.i.deriv
        pts = getattr(d, "taker_ratio", None) or []
        if len(pts) < 2:
            return
        r = statistics.fmean(p.value for p in pts[-4:])
        value = f"buy/sell {r:.2f} (4h)"
        src = f"{d.sources.get('taker_ratio', 'futures')} taker volume"
        if r >= 1.08:
            self.add("futures_flow", "derivatives", 1, (r - 1.0) / 0.25, value, "futures traders are buying at market", src)
        elif r <= 0.92:
            self.add("futures_flow", "derivatives", -1, (1.0 - r) / 0.25, value, "futures traders are selling at market", src)
        else:
            self.add("futures_flow", "derivatives", 0, 0.0, value, "futures taker flow balanced", src)

    def liquidations(self) -> None:
        d = self.i.deriv
        if d is None or "liquidations" not in getattr(d, "sources", {}):
            return
        now = self.i.now
        recent = [x for x in d.liquidations if now - x.time <= timedelta(hours=4)]
        last_hour = [x for x in recent if now - x.time <= timedelta(hours=1)]
        longs = math.fsum(x.usd for x in recent if x.side == "long")
        shorts = math.fsum(x.usd for x in recent if x.side == "short")
        big = max(250_000.0, 0.002 * (d.open_interest_usd or 0.0))
        value = f"longs {_usd(longs)} / shorts {_usd(shorts)} (4h, OKX)"
        src = "OKX liquidation orders (one exchange's sample)"
        if longs >= big and longs >= 3 * max(shorts, 1.0):
            h1 = self.i.h1
            still_falling = False
            if len(h1) >= 4:
                still_falling = h1[-1].close <= min(c.low for c in h1[-4:-1]) or h1[-1].close < h1[-1].open
            hour_longs = math.fsum(x.usd for x in last_hour if x.side == "long")
            if hour_longs >= big / 2 and still_falling:
                self.add("liquidations", "derivatives", -1, 1.0, value, "long liquidations still hitting and the price is still falling",
                         src, veto="long liquidation cascade in progress: wait for it to end")
            else:
                self.add("liquidations", "derivatives", 1, 0.4, value, "leveraged longs were flushed out and the selling paused: leverage reset", src)
        elif shorts >= big and shorts >= 3 * max(longs, 1.0):
            h1 = self.i.h1
            still_rising = False
            if len(h1) >= 4:
                still_rising = h1[-1].close >= max(c.high for c in h1[-4:-1]) or h1[-1].close > h1[-1].open
            hour_shorts = math.fsum(x.usd for x in last_hour if x.side == "short")
            squeeze = hour_shorts >= big / 2 and still_rising
            self.add("liquidations", "derivatives", -1, 0.3, value, "a short squeeze just ran: buying after it often means chasing", src,
                     short_veto="short squeeze in progress: wait for it to end" if squeeze else None)
        else:
            self.add("liquidations", "derivatives", 0, 0.0, value, "no liquidation wave", src)

    def liq_map(self) -> None:
        i, d = self.i, self.i.deriv
        if d is None or i.price is None or not i.h1:
            return
        oi = oi_in_coins(d, i.h1)
        m = lm.build(oi, i.h1, i.price, long_share=getattr(d, "long_share", ()) or ())
        if m is None:
            return
        self.map_data = m.as_dict()
        price = i.price
        long_plan = bool(i.entry and i.stop and i.entry > i.stop and not self.short)
        short_plan = bool(i.entry and i.stop and i.stop > i.entry and self.short)
        if long_plan:
            r = i.entry - i.stop  # type: ignore[operator]
            below_lo, below_hi = i.stop - 0.75 * r, i.entry  # type: ignore[operator]
            above_lo, above_hi = i.entry, i.tp2 or i.entry + 2 * r  # type: ignore[operator]
        elif short_plan:
            r = i.stop - i.entry  # type: ignore[operator]
            above_lo, above_hi = i.entry, i.stop + 0.75 * r  # type: ignore[operator]
            below_lo, below_hi = i.tp2 or i.entry - 2 * r, i.entry  # type: ignore[operator]
        else:
            span = price * max(1.0, 2.0 * (i.atr_pct or 1.5)) / 100.0
            below_lo, below_hi, above_lo, above_hi = price - span, price, price, price + span
        below = m.usd_between(below_lo, below_hi, "long")
        above = m.usd_between(above_lo, above_hi, "short")
        total = below + above
        value = f"shorts above {_usd(above)} / longs below {_usd(below)}"
        src = "estimate from open interest and leverage"
        if total <= 0:
            self.add("liq_map", "derivatives", 0, 0.0, value, "no large estimated liquidation zones near the price", src)
            return
        balance = (above - below) / total  # a buyer's point of view (flipped for shorts in add)
        detail = ("more estimated short liquidations above than long liquidations below: the path of least resistance is up"
                  if balance > 0 else "more estimated long liquidations below than short liquidations above: downside flush risk")
        self.add("liq_map", "derivatives", 1 if balance >= 0.2 else -1 if balance <= -0.2 else 0, abs(balance), value, detail, src)
        if long_plan:
            r = i.entry - i.stop  # type: ignore[operator]
            zone = [b for b in m.bands if b.long_usd > 0 and i.stop - 0.75 * r <= b.mid <= i.entry]  # type: ignore[operator]
            if zone:
                band = max(zone, key=lambda b: b.long_usd)
                if band.long_usd >= 0.25 * max(below, 1.0) and band.long_usd >= 0.05 * max(m.long_total_usd, 1.0):
                    near_stop = band.mid <= i.stop + 0.25 * r  # type: ignore[operator]  # a flush into this zone tags the stop
                    if near_stop and band.low * 0.997 < i.stop:  # type: ignore[operator]
                        self.stop_hint = band.low * 0.997
                    self.notes.append(
                        f"estimated long liquidations near {fmt_price(band.mid)} (~{_usd(band.long_usd)}): a flush there can "
                        f"run through a stop at {fmt_price(i.stop)}"
                        + (f"; a stop under {fmt_price(self.stop_hint)} clears that zone (costs more risk per coin)"
                           if self.stop_hint else ""))
            targets = [b for b in m.bands if b.short_usd > 0 and i.entry < b.mid <= (i.tp2 or i.entry + 2 * r)]  # type: ignore[operator]
            if targets:
                band = max(targets, key=lambda b: b.short_usd)
                if band.short_usd >= 0.05 * max(m.short_total_usd, 1.0):
                    self.notes.append(f"estimated short liquidations near {fmt_price(band.mid)} (~{_usd(band.short_usd)}) "
                                      "can fuel a move toward the targets")
        elif short_plan:
            r = i.stop - i.entry  # type: ignore[operator]
            zone = [b for b in m.bands if b.short_usd > 0 and i.entry <= b.mid <= i.stop + 0.75 * r]  # type: ignore[operator]
            if zone:
                band = max(zone, key=lambda b: b.short_usd)
                if band.short_usd >= 0.25 * max(above, 1.0) and band.short_usd >= 0.05 * max(m.short_total_usd, 1.0):
                    near_stop = band.mid >= i.stop - 0.25 * r  # type: ignore[operator]  # a squeeze into this zone tags the stop
                    if near_stop and band.high * 1.003 > i.stop:  # type: ignore[operator]
                        self.stop_hint = band.high * 1.003
                    self.notes.append(
                        f"estimated short liquidations near {fmt_price(band.mid)} (~{_usd(band.short_usd)}): a squeeze there can "
                        f"run through a stop at {fmt_price(i.stop)}"
                        + (f"; a stop above {fmt_price(self.stop_hint)} clears that zone (costs more risk per coin)"
                           if self.stop_hint else ""))
            low_end = i.tp2 or i.entry - 2 * r  # type: ignore[operator]
            targets = [b for b in m.bands if b.long_usd > 0 and low_end <= b.mid < i.entry]  # type: ignore[operator]
            if targets:
                band = max(targets, key=lambda b: b.long_usd)
                if band.long_usd >= 0.05 * max(m.long_total_usd, 1.0):
                    self.notes.append(f"estimated long liquidations near {fmt_price(band.mid)} (~{_usd(band.long_usd)}) "
                                      "can fuel a drop toward the targets")

    # --- order flow -------------------------------------------------------------------

    def spot_flow(self) -> None:
        i = self.i
        candles = i.setup if any(c.taker_buy_base is not None for c in i.setup[-6:]) else i.h1
        share = taker_share(candles, 6)
        if share is None:
            return
        value = f"{share * 100:.0f}% of volume bought at market (last 6 candles)"
        if share >= 0.54:
            self.add("spot_flow", "flow", 1, (share - 0.5) / 0.08, value, "buyers are in control of the spot tape", "exchange taker volume")
        elif share <= 0.46:
            self.add("spot_flow", "flow", -1, (0.5 - share) / 0.08, value, "sellers are in control of the spot tape", "exchange taker volume")
        else:
            self.add("spot_flow", "flow", 0, 0.0, value, "spot flow balanced", "exchange taker volume")

    def cvd(self) -> None:
        h1 = self.i.h1
        cvd, price = cvd_norm(h1, 24), pct_change(h1, 24)
        if cvd is None or price is None:
            return
        value = f"CVD {cvd * 100:+.1f}% of volume, price {price:+.1f}% (24h)"
        src = "hourly taker volume"
        if price >= 1.0 and cvd <= -0.03:
            self.add("cvd", "flow", -1, abs(cvd) / 0.1, value, "price rose but sellers hit the market more: the move lacks real buying", src)
        elif price <= -1.0 and cvd >= 0.03:
            self.add("cvd", "flow", 1, cvd / 0.1, value, "price fell but buyers absorbed it: possible accumulation", src)
        elif price >= 1.0 and cvd >= 0.03:
            self.add("cvd", "flow", 1, 0.4, value, "the rise is backed by market buying", src)
        elif price <= -1.0 and cvd <= -0.03:
            self.add("cvd", "flow", -1, 0.4, value, "the drop is driven by market selling", src)
        else:
            self.add("cvd", "flow", 0, 0.0, value, "no divergence", src)

    def volume(self) -> None:
        i = self.i
        days = [c for c in i.d1[-21:-1] if c.volume > 0]
        if len(days) < 10 or not i.volume_24h_quote:
            return
        avg = statistics.fmean((c.quote_volume if c.quote_volume else c.volume * c.close) for c in days)
        if avg <= 0:
            return
        ratio = i.volume_24h_quote / avg
        if ratio > 50 or ratio < 0.02:  # not comparable (different units or a broken candle): skip rather than guess
            return
        change = i.change_24h_pct if i.change_24h_pct is not None else (self.price_change_24h() or 0.0)
        value = f"{ratio:.1f}x the 20-day average"
        if ratio >= 1.5 and change >= 0:
            self.add("volume", "flow", 1, (ratio - 1.0) / 2.0, value, "heavy volume on a rising day: participation", "24h volume")
        elif ratio >= 1.5:
            self.add("volume", "flow", -1, (ratio - 1.0) / 2.0, value, "heavy volume on a falling day: distribution", "24h volume")
        elif ratio <= 0.6:
            self.add("volume", "flow", -1, 0.3, value, "interest fading: volume well below average", "24h volume")
        else:
            self.add("volume", "flow", 0, 0.0, value, "normal volume", "24h volume")

    def book(self) -> None:
        b = self.i.book_imbalance
        if b is None:
            return
        value = f"imbalance {b:+.2f}"
        if b >= 0.25:
            self.add("book", "flow", 1, b, value, "more resting bids than asks near the price", "order book")
        elif b <= -0.25:
            self.add("book", "flow", -1, -b, value, "more resting asks than bids near the price", "order book")
        else:
            self.add("book", "flow", 0, 0.0, value, "order book balanced", "order book")

    def rel_strength(self) -> None:
        if self.is_btc:
            return
        coin, btc = pct_change(self.i.h1, 168), pct_change(self.i.btc_h1, 168)
        if coin is None or btc is None:
            return
        diff = coin - btc
        value = f"{coin:+.1f}% vs Bitcoin {btc:+.1f}% (7 days)"
        if diff >= 3:
            self.add("rel_strength", "flow", 1, diff / 12, value, "outperforming Bitcoin: money is rotating in", "7-day returns")
        elif diff <= -3:
            self.add("rel_strength", "flow", -1, -diff / 12, value, "underperforming Bitcoin", "7-day returns")
        else:
            self.add("rel_strength", "flow", 0, 0.0, value, "moving with Bitcoin", "7-day returns")

    # --- news and hype ----------------------------------------------------------------

    def news(self) -> None:
        i = self.i
        recent = [(t, title, tone) for t, title, tone in i.headlines if t is None or i.now - t <= timedelta(hours=48)]
        ai = i.ai_news
        critical = [title for _, title, _ in recent if CRITICAL.search(title)]
        if critical or (ai and ai.get("critical")):
            src = "headlines (48h)" + (" + AI reading" if ai else "")
            title = critical[0] if critical else str(ai.get("reason") or "AI flagged critical news")
            if ai is not None and not ai.get("critical") and (ai.get("impact") or 0) >= 0:
                self.add("news_critical", "news", -1, 0.5, f"{len(critical)} flagged headline(s)",
                         f"keywords flagged “{title[:120]}”, but the AI reading does not see it as critical", src)
            else:
                self.add("news_critical", "news", -1, 1.0, f"{max(1, len(critical))} critical headline(s)",
                         f"“{title[:160]}”", src, veto=f"critical news: {title[:120]}")
        elif recent:
            self.add("news_critical", "news", 0, 0.0, "none", "no hack, delisting or similar headline in 48 hours", "headlines (48h)")
        if ai is not None and ai.get("impact") is not None:
            impact = max(-2, min(2, int(ai.get("impact") or 0)))
            self.add("news_tone", "news", (impact > 0) - (impact < 0), abs(impact) / 2.0, f"AI impact {impact:+d} of ±2",
                     str(ai.get("reason") or "")[:200], f"AI reading of {len(recent)} headline(s)")
        elif len(recent) >= 2:
            pos = sum(1 for _, _, tone in recent if tone == "positive")
            neg = sum(1 for _, _, tone in recent if tone == "negative")
            tone = (pos - neg) / len(recent)
            value = f"{pos} positive / {neg} negative of {len(recent)}"
            if tone >= 0.3:
                self.add("news_tone", "news", 1, tone, value, "headlines lean positive", "headline keywords (48h)")
            elif tone <= -0.3:
                self.add("news_tone", "news", -1, -tone, value, "headlines lean negative", "headline keywords (48h)")
            else:
                self.add("news_tone", "news", 0, 0.0, value, "headlines mixed", "headline keywords (48h)")
        catalysts = [title for _, title, _ in recent if CATALYST.search(title) and not CRITICAL.search(title)]
        if catalysts:
            major = [t for t in catalysts if MAJOR_EXCHANGES.search(t) or re.search(r"\betf\b", t, re.IGNORECASE)]
            pick = (major or catalysts)[0]
            self.add("catalyst", "news", 1, 0.7 if major else 0.4, f"{len(catalysts)} headline(s)", f"“{pick[:160]}”",
                     "headlines (48h)", short_veto=f"major positive catalyst: do not short into it ({pick[:100]})" if major else None)

    def hype(self) -> None:
        i = self.i
        if i.mentions_24h is None and i.trending_rank is None:
            return
        buzz = (i.mentions_24h or 0) / max(0.5, i.mentions_per_day or 0.0) if i.mentions_per_day is not None else None
        trending = i.trending_rank is not None and i.trending_rank <= 15
        hot = trending or (buzz is not None and buzz >= 3 and (i.mentions_24h or 0) >= 3)
        extended = (i.rsi is not None and i.rsi >= 72) or (i.change_24h_pct is not None and i.change_24h_pct >= 12)
        parts = []
        if i.mentions_24h is not None:
            parts.append(f"{i.mentions_24h} headline(s) in 24h" + (f" ({buzz:.1f}x usual)" if buzz is not None else ""))
        if trending:
            parts.append(f"trending #{i.trending_rank} on CoinGecko")
        value = ", ".join(parts) or "quiet"
        src = "news mentions + CoinGecko trending"
        if hot and extended:
            self.add("hype", "news", -1, 0.7, value, "hype while the price is stretched: late buyers often become exit liquidity", src)
        elif hot:
            self.add("hype", "news", 1, 0.4, value, "attention is rising before the price is stretched", src)
        else:
            self.add("hype", "news", 0, 0.0, value, "no unusual attention", src)

    # --- market -----------------------------------------------------------------------

    def market(self) -> None:
        m = self.i.market
        if m is None:
            return
        if not self.is_btc and (m.btc_trend_4h is not None or m.btc_trend is not None):
            t4, t1 = m.btc_trend_4h, m.btc_trend
            value = f"4H {getattr(t4, 'value', 'n/a')}, 1D {getattr(t1, 'value', 'n/a')}"
            if t4 == TrendDirection.DOWN:
                self.add("btc_trend", "market", -1, 0.8, value, "Bitcoin falling on 4H: altcoins rarely hold up", "market regime")
            elif t1 == TrendDirection.DOWN:
                self.add("btc_trend", "market", -1, 0.4, value, "Bitcoin's daily trend is down", "market regime")
            elif t4 == TrendDirection.UP and t1 == TrendDirection.UP:
                self.add("btc_trend", "market", 1, 0.6, value, "Bitcoin trending up on 4H and 1D: tailwind", "market regime")
            elif t4 == TrendDirection.UP:
                self.add("btc_trend", "market", 1, 0.4, value, "Bitcoin rising on 4H", "market regime")
            else:
                self.add("btc_trend", "market", 0, 0.0, value, "Bitcoin sideways", "market regime")
        if m.breadth_pct is not None and m.breadth_sample >= 5:
            b = m.breadth_pct
            value = f"{b:.0f}% of top coins in uptrends"
            if b >= 60:
                self.add("breadth", "market", 1, (b - 50) / 40, value, "most coins are rising: broad market support", "market regime")
            elif b <= 40:
                self.add("breadth", "market", -1, (50 - b) / 40, value, "most coins are weak", "market regime")
            else:
                self.add("breadth", "market", 0, 0.0, value, "breadth mixed", "market regime")
        fg = m.fear_greed
        if fg is not None:
            v = fg.value
            value = f"{v} ({fg.classification})"
            if v >= 80:
                self.add("fear_greed", "market", -1, 0.5, value, "extreme greed: tops form here more often", fg.source)
            elif v >= 70:
                self.add("fear_greed", "market", -1, 0.2, value, "greed", fg.source)
            elif v <= 20:
                self.add("fear_greed", "market", 1, 0.3, value, "extreme fear: good entries for trend-following buys", fg.source)
            else:
                self.add("fear_greed", "market", 0, 0.0, value, "no extreme", fg.source)

    def market_leverage(self) -> None:
        md = self.i.market_deriv
        if not md or md.get("avg_funding_pct") is None:
            return
        f = float(md["avg_funding_pct"])
        hot = md.get("hot") or []
        value = f"avg funding {f:+.4f}%/8h over {md.get('coins', 0)} coins" + (f", {len(hot)} crowded" if hot else "")
        if f >= 0.03:
            self.add("market_leverage", "market", -1, (f - 0.01) / 0.05, value, "the whole market is leveraged long: flush risk", "futures funding")
        elif f <= -0.01:
            self.add("market_leverage", "market", 1, 0.3, value, "the market leans short: little leverage to unwind", "futures funding")
        else:
            self.add("market_leverage", "market", 0, 0.0, value, "market leverage normal", "futures funding")

    def macro(self) -> None:
        i = self.i
        if i.stablecoin_change_7d is not None:
            c = i.stablecoin_change_7d
            value = f"{c:+.2f}% in 7 days"
            if c >= 0.5:
                self.add("stablecoins", "market", 1, 0.3, value, "fresh stablecoin supply: buying power entering crypto", "DefiLlama")
            elif c <= -0.5:
                self.add("stablecoins", "market", -1, 0.3, value, "stablecoin supply shrinking: liquidity leaving", "DefiLlama")
            else:
                self.add("stablecoins", "market", 0, 0.0, value, "stablecoin supply flat", "DefiLlama")
        if i.altseason is not None and not self.is_btc:
            a = i.altseason
            value = f"index {a}/100"
            if a >= 75:
                self.add("altseason", "market", 1, 0.4, value, "altcoin season: alts outperform Bitcoin", "CoinMarketCap")
            elif a <= 25:
                self.add("altseason", "market", -1, 0.3, value, "Bitcoin season: most alts lag", "CoinMarketCap")
            else:
                self.add("altseason", "market", 0, 0.0, value, "neither season", "CoinMarketCap")

    # --- events -----------------------------------------------------------------------

    def events(self) -> None:
        u = self.i.unlock
        if u and u.get("days") is not None and u.get("pct") is not None:
            days, pct = float(u["days"]), float(u["pct"])
            value = f"{pct:.2f}% of supply in {days:.0f} days" + (f" ({_usd(u.get('usd'))})" if u.get("usd") else "")
            if days <= 3 and pct >= 1:
                self.add("unlock", "events", -1, 1.0, value, "a large unlock within days: supply hits the market", "Mobula",
                         veto=f"token unlock of {pct:.1f}% of supply in {days:.0f} days")
            elif days <= 7 and pct >= 1:
                self.add("unlock", "events", -1, 0.6, value, "a large unlock this week", "Mobula")
            elif pct >= 1:
                self.add("unlock", "events", -1, 0.3, value, "a large unlock within two weeks", "Mobula")
            else:
                self.add("unlock", "events", 0, 0.0, value, "small unlock", "Mobula")
        flow = self.i.exchange_flow
        if flow:
            inflow, outflow = flow.get("inflow") or 0.0, flow.get("outflow") or 0.0
            value = f"in {_usd(inflow)} / out {_usd(outflow)}"
            if inflow >= 10e6 and inflow > 2 * outflow:
                self.add("exchange_flow", "events", -1, 0.5, value, "whales moving coins onto exchanges: often ahead of selling", "on-chain transfers")
            elif outflow >= 10e6 and outflow > 2 * inflow:
                self.add("exchange_flow", "events", 1, 0.4, value, "whales withdrawing from exchanges: accumulation", "on-chain transfers")
            elif inflow or outflow:
                self.add("exchange_flow", "events", 0, 0.0, value, "no clear direction", "on-chain transfers")


def build_evidence(i: EvidenceInputs, weights: dict[str, float] | None = None) -> Evidence:
    board = _Board(i, {**PRIOR_WEIGHTS, **(weights or {})})
    for step in (board.funding, board.oi_trend, board.crowd, board.whales, board.futures_flow, board.liquidations,
                 board.liq_map, board.spot_flow, board.cvd, board.volume, board.book, board.rel_strength, board.news,
                 board.hype, board.market, board.market_leverage, board.macro, board.events):
        step()
    factors = board.factors
    total_w = math.fsum(f.weight for f in factors)
    score = round(100.0 * math.fsum(f.weight * f.signed for f in factors) / total_w, 1) if total_w > 0 else None
    if score is None or len(factors) < MIN_FACTORS:
        grade = "thin"
    elif score >= STRONG_FOR:
        grade = "strong_for"
    elif score >= SUPPORTIVE:
        grade = "for"
    elif score <= STRONG_AGAINST:
        grade = "strong_against"
    elif score <= AGAINST:
        grade = "against"
    else:
        grade = "neutral"
    present = {f.key for f in factors}
    errors = list(getattr(i.deriv, "errors", []) or [])[:6]
    return Evidence(
        version=EVIDENCE_VERSION, computed_at=i.now, symbol=i.symbol.upper(), horizon=i.horizon, score=score, grade=grade,
        factors=factors, missing=[k for k in PRIOR_WEIGHTS if k not in present], vetoes=board.vetoes,
        liq_map=board.map_data, stop_hint=board.stop_hint, notes=board.notes, errors=errors, side=i.side,
    )


def apply_to_label(label: SignalLabel, ev: Evidence | None, mode: str) -> tuple[SignalLabel, list[str]]:
    """The label after the evidence board (filter mode only lowers it) and the reasons."""
    if ev is None or mode != "filter" or label not in (SignalLabel.BUY, SignalLabel.STRONG_BUY):
        return label, []
    if ev.vetoes:
        return SignalLabel.WATCH, [f"evidence veto: {ev.vetoes[0]}"]
    if ev.thin or ev.score is None:
        return label, []
    against = ", ".join(f"{f.label.lower()} ({f.value})" for f in ev.top(-1))
    if ev.score <= STRONG_AGAINST:
        return SignalLabel.WATCH, [f"evidence board strongly against ({ev.score:+.0f}): {against}"]
    if ev.score <= AGAINST and label == SignalLabel.STRONG_BUY:
        return SignalLabel.BUY, [f"not STRONG BUY: evidence board shows headwinds ({ev.score:+.0f}): {against}"]
    return label, []
