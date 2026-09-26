"""Signova AI analyst (Phase 13): an OpenAI model reads everything and trades like an expert.

The rule engines (swing, scalp, futures) decide from fixed equations. The AI analyst works the way a
discretionary trader does: it reads a full dossier for one coin and forms its own view.

The dossier contains:
- price, spread and order-book depth;
- several timeframes of candles, with indicators, regimes, swing structure and support/resistance;
- futures positioning: funding, open interest, long/short ratios, top traders, taker flow;
- liquidations and the estimated liquidation map;
- the evidence board;
- news and its AI reading, sentiment and the market backdrop (Bitcoin, breadth, fear & greed);
- what the rule engines see, with their measured research.

The model answers in strict JSON with a decision (LONG, SHORT or NO_TRADE), its conviction, an exact
plan (entry, stop, two targets, hold time) and its reasoning.

The model decides direction and levels; this module never invents a trade. It only checks that a plan
is executable and worth its costs, the checks any desk applies before an order goes out:
- the stop and targets are on the right sides of the entry;
- the entry is reachable from the current price;
- the stop is not inside normal noise, nor too tight for fees;
- the reward after costs is at least the minimum reward:risk.
A plan that fails a check is shown as WATCH with the reason, never silently changed.

This file is pure (no I/O): the service in app.services.ai_analyst gathers the data and calls OpenAI.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from statistics import fmean
from typing import Any

from app.analysis.features import IndicatorSnapshot, compute_snapshot
from app.analysis.regime import classify_timeframe
from app.analysis.structure import analyze_structure
from app.core.enums import SignalLabel, Timeframe
from app.data.normalization.schemas import Candle

PROMPT_VERSION = "analyst-1"
DECISIONS = ("LONG", "SHORT", "NO_TRADE")
ENTRY_TYPES = ("market", "limit", "stop")
VERDICTS = ("approve", "reduce", "reject")
MARKETS = ("spot", "futures")
HORIZONS = ("15m", "1h", "4h", "1d")

# what the analyst reads per horizon: (timeframe, candles shown); the first is the setup timeframe
VIEWS: dict[str, list[tuple[Timeframe, int]]] = {
    "15m": [(Timeframe.M5, 72), (Timeframe.M15, 64), (Timeframe.H1, 48), (Timeframe.H4, 30)],
    "1h": [(Timeframe.M15, 72), (Timeframe.H1, 60), (Timeframe.H4, 42), (Timeframe.D1, 30)],
    "4h": [(Timeframe.H1, 72), (Timeframe.H4, 60), (Timeframe.D1, 60)],
    "1d": [(Timeframe.H4, 72), (Timeframe.D1, 90)],
}
HOLD_TEXT = {
    "15m": "a scalp: held from 15 minutes to about 2 hours",
    "1h": "an intraday trade: held from 1 to about 8 hours",
    "4h": "a swing trade: held from 4 hours to about 2 days",
    "1d": "a position trade: held from 1 to about 5 days",
}
MAX_HOLD_HOURS = {"15m": 2.0, "1h": 8.0, "4h": 48.0, "1d": 120.0}
MAX_ENTRY_WAIT_HOURS = {"15m": 1.0, "1h": 4.0, "4h": 12.0, "1d": 36.0}
MAX_STOP_PCT = {"15m": 4.0, "1h": 6.0, "4h": 12.0, "1d": 20.0}


def setup_timeframe(horizon: str) -> Timeframe:
    return VIEWS[horizon][0][0]


def _num(value: Any, digits: int = 6) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return float(f"{v:.{digits}g}")


def _val(x: Any) -> Any:
    return getattr(x, "value", x)


def _t(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M") if value else None


# ----------------------------------------------------------------------------- the dossier


def candle_rows(candles: Sequence[Candle], n: int) -> list[list[Any]]:
    """Compact OHLCV rows (UTC open time, 6 significant digits) plus the taker-buy share of volume."""
    rows = []
    for c in candles[-n:]:
        buy = round(100.0 * c.taker_buy_base / c.volume) if c.taker_buy_base is not None and c.volume > 0 else None
        rows.append([c.open_time.strftime("%m-%d %H:%M"), _num(c.open), _num(c.high), _num(c.low), _num(c.close),
                     _num(c.volume, 3), buy])
    return rows


def _indicators(s: IndicatorSnapshot) -> dict[str, Any]:
    def dist(ref: float | None) -> float | None:
        return _num((s.close / ref - 1.0) * 100.0, 3) if ref else None

    return {
        "close": _num(s.close), "ema20": _num(s.ema20), "ema50": _num(s.ema50), "ema200": _num(s.ema200),
        "close_vs_ema20_pct": dist(s.ema20), "close_vs_ema50_pct": dist(s.ema50), "close_vs_ema200_pct": dist(s.ema200),
        "ema20_slope_pct": _num(s.ema20_slope_pct, 3), "ema50_slope_pct": _num(s.ema50_slope_pct, 3),
        "rsi14": _num(s.rsi14, 3), "rsi14_prev": _num(s.rsi14_prev, 3),
        "macd_hist": _num(s.macd_hist, 4), "macd_hist_prev": _num(s.macd_hist_prev, 4),
        "atr14": _num(s.atr14, 4), "atr_pct": _num(s.atr_pct, 3), "atr_pct_percentile": _num(s.atr_pct_percentile, 3),
        "bb_upper": _num(s.bb_upper), "bb_lower": _num(s.bb_lower), "bb_pct_b": _num(s.bb_pct_b, 3),
        "bb_width_percentile": _num(s.bb_width_percentile, 3),
        "adx14": _num(s.adx14, 3), "plus_di": _num(s.plus_di, 3), "minus_di": _num(s.minus_di, 3),
        "volume_ratio": _num(s.volume_ratio, 3), "up_down_volume_ratio": _num(s.up_down_volume_ratio, 3),
        "obv_slope": _num(s.obv_slope, 3), "roc10_pct": _num(s.roc10, 3), "roc20_pct": _num(s.roc20, 3),
        "realized_vol_pct": _num(s.realized_vol_pct, 3),
    }


def timeframe_view(tf: Timeframe, candles: Sequence[Candle], n: int) -> dict[str, Any] | None:
    """One timeframe as a chart reader sees it: candles, indicators, regime, structure and levels."""
    if not candles:
        return None
    snap = compute_snapshot(tf, candles)
    out: dict[str, Any] = {"timeframe": tf.label, "last_closed": _t(candles[-1].close_time)}
    if snap is not None:
        out["indicators"] = _indicators(snap)
        reg = classify_timeframe(snap)
        out["regime"] = {"trend": _val(reg.trend), "label": _val(reg.label), "strength": reg.strength,
                         "volatility": _val(reg.volatility)}
        structure = analyze_structure(tf, candles, snap.atr14)
        if structure is not None:
            out["structure"] = {
                "trend": _val(structure.trend), "reason": structure.reason,
                "swing_highs": [[_t(p.time), _num(p.price)] for p in structure.highs[-4:]],
                "swing_lows": [[_t(p.time), _num(p.price)] for p in structure.lows[-4:]],
                "last_break": ({"direction": structure.last_break.direction, "level": _num(structure.last_break.level),
                                "candles_ago": structure.last_break.candles_ago} if structure.last_break else None),
                "supports": [{"price": _num(lv.price), "touches": lv.touches, "distance_pct": _num(lv.distance_pct, 3)}
                             for lv in structure.supports],
                "resistances": [{"price": _num(lv.price), "touches": lv.touches, "distance_pct": _num(lv.distance_pct, 3)}
                                for lv in structure.resistances],
            }
    window = candles[-n:]
    out["window"] = {"candles": len(window), "high": _num(max(c.high for c in window)),
                     "low": _num(min(c.low for c in window))}
    out["candles"] = {"columns": ["open_time_utc", "open", "high", "low", "close", "volume", "taker_buy_pct"],
                      "rows": candle_rows(candles, n)}
    return out


def _value_at(points: Sequence[Any], when: datetime) -> float | None:
    earlier = [p for p in points if p.time <= when]
    return earlier[-1].value if earlier else None


def _change_pct(points: Sequence[Any], hours: float) -> float | None:
    if len(points) < 2:
        return None
    last = points[-1]
    before = _value_at(points, last.time - timedelta(hours=hours))
    if not before:
        return None
    return _num((last.value / before - 1.0) * 100.0, 3)


def derivatives_view(snap: Any, now: datetime) -> dict[str, Any]:
    """Futures positioning as numbers: funding, open interest, crowd and top-trader ratios, flow, liquidations."""
    if snap is None or not getattr(snap, "available", False):
        return {"available": False, "errors": list(getattr(snap, "errors", []) or [])[:3]}
    funding = [p.value for p in snap.funding_history[-21:]]
    liq = list(snap.liquidations)

    def liq_sum(hours: float) -> dict[str, float]:
        since = now - timedelta(hours=hours)
        return {side: _num(sum(x.usd for x in liq if x.side == side and x.time >= since), 4) or 0.0 for side in ("long", "short")}

    def share(points: Sequence[Any]) -> dict[str, Any] | None:
        if not points:
            return None
        last = points[-1]
        ago = _value_at(points, last.time - timedelta(hours=24))
        return {"now": _num(last.value, 3), "24h_ago": _num(ago, 3)}

    taker = [p.value for p in snap.taker_ratio[-6:]]
    return {
        "available": True, "listed_on": list(snap.listed), "sources": dict(snap.sources),
        "funding_pct_8h": _num(snap.funding_pct, 4),
        "funding_avg_7d_pct_8h": _num(fmean(funding), 4) if funding else None,
        "funding_recent_pct_8h": [_num(v, 4) for v in funding[-6:]],
        "open_interest_usd": _num(snap.open_interest_usd, 4),
        "open_interest_change_pct": {h: _change_pct(snap.oi_history, n) for h, n in (("1h", 1), ("4h", 4), ("24h", 24), ("72h", 72))},
        "long_account_share": share(snap.long_share),
        "top_trader_long_share": share(snap.top_long_share),
        "taker_buy_sell_ratio": {"last": _num(taker[-1], 3) if taker else None,
                                 "avg_last_6": _num(fmean(taker), 3) if taker else None},
        "liquidations_usd": {"last_1h": liq_sum(1), "last_4h": liq_sum(4), "last_24h": liq_sum(24),
                             "largest_24h": [{"time": _t(x.time), "side": x.side, "price": _num(x.price), "usd": _num(x.usd, 3)}
                                             for x in sorted((x for x in liq if x.time >= now - timedelta(hours=24)),
                                                             key=lambda x: x.usd, reverse=True)[:3]]},
    }


def liq_map_view(liq_map: dict[str, Any] | None) -> dict[str, Any] | None:
    """The estimated liquidation map as its largest clusters on each side of the price."""
    if not liq_map or not liq_map.get("bands"):
        return None
    bands = liq_map["bands"]
    below = sorted((b for b in bands if b.get("long_usd", 0) > 0), key=lambda b: b["long_usd"], reverse=True)[:3]
    above = sorted((b for b in bands if b.get("short_usd", 0) > 0), key=lambda b: b["short_usd"], reverse=True)[:3]
    return {
        "method": liq_map.get("method"), "hours": liq_map.get("hours"),
        "long_liquidations_below": [{"low": _num(b["low"]), "high": _num(b["high"]), "usd": _num(b["long_usd"], 3)} for b in below],
        "short_liquidations_above": [{"low": _num(b["low"]), "high": _num(b["high"]), "usd": _num(b["short_usd"], 3)} for b in above],
        "total_long_usd": _num(liq_map.get("long_total_usd"), 3), "total_short_usd": _num(liq_map.get("short_total_usd"), 3),
    }


def board_view(board: dict[str, Any] | None) -> dict[str, Any] | None:
    """The evidence board's measured factors (values and what they mean), without its score arithmetic."""
    if not board or not isinstance(board.get("factors"), list):
        return None
    return {
        "factors": [f"{f.get('label')}: {f.get('value')} ({f.get('detail')})" for f in board["factors"] if isinstance(f, dict)],
        "vetoes": board.get("vetoes") or [],
        "notes": (board.get("notes") or [])[:4],
        "missing": board.get("missing") or [],
    }


@dataclass
class DossierInputs:
    symbol: str
    name: str
    market: str  # spot | futures
    horizon: str  # 15m | 1h | 4h | 1d
    now: datetime
    price: float
    candles: dict[Timeframe, Sequence[Candle]]
    change_24h_pct: float | None = None
    volume_24h_usd: float | None = None
    spread_bps: float | None = None
    depth_usd: dict[str, float | None] | None = None
    book_imbalance: float | None = None
    data_notes: list[str] = field(default_factory=list)
    derivatives: Any = None  # DerivativesSnapshot
    mark_price: float | None = None
    board: dict[str, Any] | None = None  # the evidence board (as_dict) for a long
    news: list[dict[str, Any]] = field(default_factory=list)  # this coin's headlines
    ai_news: dict[str, Any] | None = None
    market_news: list[dict[str, Any]] = field(default_factory=list)
    sentiment: dict[str, Any] | None = None
    events: list[str] = field(default_factory=list)
    market_context: dict[str, Any] | None = None
    btc: dict[str, Any] | None = None  # Bitcoin's own view (for altcoins)
    quant: dict[str, Any] | None = None  # what the rule engines see, with their measured research
    cost_pct: float = 0.2  # round trip
    min_reward_risk: float = 1.5
    risk_per_trade_pct: float = 1.0
    max_leverage: int | None = None


def build_dossier(i: DossierInputs) -> dict[str, Any]:
    views = [v for tf, n in VIEWS[i.horizon] if (v := timeframe_view(tf, i.candles.get(tf) or [], n)) is not None]
    dossier: dict[str, Any] = {
        "task": {
            "market": i.market, "horizon": i.horizon, "holding": HOLD_TEXT[i.horizon],
            "max_hold_hours": MAX_HOLD_HOURS[i.horizon],
            "allowed_decisions": ["LONG", "NO_TRADE"] if i.market == "spot" else list(DECISIONS),
            "round_trip_cost_pct": _num(i.cost_pct, 3),
            "min_net_reward_risk": i.min_reward_risk,
            "risk_per_trade_pct_of_equity": i.risk_per_trade_pct,
            **({"max_leverage": i.max_leverage, "note": "position size and leverage are computed by the dashboard from your stop"}
               if i.market == "futures" else {}),
        },
        "coin": {
            "symbol": i.symbol, "name": i.name, "time_utc": _t(i.now), "price": _num(i.price),
            "change_24h_pct": _num(i.change_24h_pct, 3), "volume_24h_usd": _num(i.volume_24h_usd, 3),
            "spread_bps": _num(i.spread_bps, 3), "order_book_depth_usd_within_1pct": i.depth_usd,
            "order_book_imbalance": _num(i.book_imbalance, 3),
            "perp_mark_price": _num(i.mark_price),
            "perp_basis_pct": _num((i.mark_price / i.price - 1.0) * 100.0, 3) if i.mark_price and i.price else None,
            "data_notes": i.data_notes[:4],
        },
        "timeframes": views,
        "derivatives": derivatives_view(i.derivatives, i.now),
        "liquidation_map_estimate": liq_map_view((i.board or {}).get("liq_map")),
        "evidence_factors": board_view(i.board),
        "news": {"coin_headlines": i.news[:12], "ai_reading": i.ai_news, "market_headlines": i.market_news[:8],
                 "sentiment": i.sentiment, "scheduled_events": i.events[:4]},
        "market": i.market_context,
        "bitcoin": i.btc,
        "rule_engines": i.quant,
    }
    return dossier


def dossier_text(dossier: dict[str, Any]) -> str:
    return json.dumps(dossier, default=str, separators=(",", ":"))


# ----------------------------------------------------------------------------- the prompts

ANALYST_PROMPT = """You are Signova's senior crypto trader: fifteen years of discretionary trading, spot and
perpetual futures, known for protecting capital first. You receive a DOSSIER (JSON) for one coin with
live market data and decide whether there is a trade for the given market and horizon, exactly as
you would before risking your own money.

How you work (top down, every time):
1. Market backdrop: Bitcoin's trend and momentum, breadth, fear & greed, market-wide funding. Most alts
   follow Bitcoin; do not buy an alt into a falling Bitcoin, or short one into a Bitcoin squeeze, without a clear reason.
2. Higher timeframe first: trend, market structure (higher highs/lows or lower), where price sits in the
   range, the major support/resistance and whether they were just broken or rejected.
3. Setup timeframe: the actual trigger. Good setups: a pullback to value in a trend (EMA20/50, prior
   breakout level, demand/supply) with a reaction; a breakout from compression with volume and acceptance;
   a failed breakdown/breakout that traps traders (liquidity sweep and reclaim); a range-edge fade with
   rejection. Bad setups: chasing an extended candle, entering in the middle of a range, fighting the
   higher-timeframe trend without a structure break, buying straight into resistance.
4. Positioning and flow: funding extremes and open interest (crowded longs or shorts are fuel for the
   opposite move), rising open interest with price (new money) versus falling (short covering or
   liquidation), long/short and top-trader ratios, taker flow, recent liquidations and where the estimated
   liquidation clusters sit (price is often drawn to them; a stop just before a cluster gets hunted).
5. News and catalysts: hacks, delistings, unlocks and regulation override charts; strong positive news
   can squeeze shorts. Weigh the AI news reading and sentiment, but never invent news.
6. The rule engines: read what they see and their measured research as one more opinion. Their
   backtests are real statistics on these coins; respect them, but you decide.
7. The plan: entry at a level (market only when the trigger is happening now), stop beyond the level
   that proves you wrong plus a buffer (about 0.5 to 1 ATR of the setup timeframe, and beyond nearby
   liquidation clusters or obvious stop pools), first target at the next real level, second target at
   the next major level or liquidity. The net reward:risk after costs must be at least the minimum in
   the task. Prefer a limit entry at a level over chasing.

Discipline:
- NO_TRADE is the right answer most of the time. Only trade when several independent reasons line up
  (trend, structure, level, momentum, positioning, flow) and nothing major argues against it.
- Conviction (0-100) is your honest probability-weighted confidence. 80+ only for A+ setups where
  timeframes, structure, flow and positioning all agree; 60-79 good setups; below 60 means no trade.
- probability_tp1_before_stop: your calibrated estimate, not a hope. Be realistic (most good setups are 0.5-0.65).
- Use only numbers from the DOSSIER. Every price you give must be consistent with the candles and
  levels in it. Never invent data.
- Spot market: only LONG or NO_TRADE (describe a bearish view in no_trade_reason).
- Keep every text field short and concrete (numbers, levels, timeframes).

Answer with the JSON object only (the schema is enforced)."""

REVIEW_PROMPT = """You are Signova's risk manager, reviewing a trade proposed by the senior trader
before any money is committed. You get the same DOSSIER (JSON) and the PLAN (JSON).

Try hard to find what is wrong, as an independent second trader would:
- is the direction against the higher-timeframe trend or Bitcoin without a real structure break?
- is the entry chasing an extended move, or in the middle of a range?
- is the stop inside normal noise (less than about 0.5 ATR of the setup timeframe), in front of an
  obvious stop pool or estimated liquidation cluster, or not at the level that proves the idea wrong?
- is a target beyond a major level that price is unlikely to break within the holding time?
- does positioning (funding, open interest, crowding), flow or news argue against it?
- are the plan's numbers consistent with the DOSSIER?

verdict: "approve" (sound, keep it), "reduce" (tradeable but weaker than claimed: give a lower
adjusted_conviction), "reject" (a concrete problem visible in the data). Be specific and brief.
Use only the DOSSIER; never invent data. Answer with the JSON object only."""


def _nullable(kind: str) -> dict[str, Any]:
    return {"type": [kind, "null"]}


ANALYST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "bias": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "conviction": {"type": "integer"},
        "setup": {"type": "string"},
        "entry_type": {"type": "string", "enum": list(ENTRY_TYPES)},
        "entry": _nullable("number"),
        "entry_zone_low": _nullable("number"),
        "entry_zone_high": _nullable("number"),
        "stop_loss": _nullable("number"),
        "take_profit_1": _nullable("number"),
        "take_profit_2": _nullable("number"),
        "expected_hold_hours": _nullable("number"),
        "entry_valid_hours": _nullable("number"),
        "probability_tp1_before_stop": _nullable("number"),
        "thesis": {"type": "string"},
        "market_context": {"type": "string"},
        "trend_and_structure": {"type": "string"},
        "key_levels": {"type": "string"},
        "positioning_and_flow": {"type": "string"},
        "news_and_catalysts": {"type": "string"},
        "confluences": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "invalidation": {"type": "string"},
        "what_to_watch": {"type": "array", "items": {"type": "string"}},
        "no_trade_reason": _nullable("string"),
    },
    "required": [
        "decision", "bias", "conviction", "setup", "entry_type", "entry", "entry_zone_low", "entry_zone_high",
        "stop_loss", "take_profit_1", "take_profit_2", "expected_hold_hours", "entry_valid_hours",
        "probability_tp1_before_stop", "thesis", "market_context", "trend_and_structure", "key_levels",
        "positioning_and_flow", "news_and_catalysts", "confluences", "risks", "invalidation", "what_to_watch",
        "no_trade_reason",
    ],
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "adjusted_conviction": {"type": "integer"},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "adjusted_conviction", "summary", "issues"],
}


# ----------------------------------------------------------------------------- parsing


def _json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start : end + 1]) if 0 <= start < end else {}
        except ValueError:
            data = {}
    return data if isinstance(data, dict) else {}


def _str(value: Any, limit: int = 600) -> str:
    return str(value).strip()[:limit] if value is not None else ""


def _strings(value: Any, count: int = 6, limit: int = 240) -> list[str]:
    items = value if isinstance(value, list) else []
    return [_str(x, limit) for x in items if _str(x)][:count]


@dataclass
class Decision:
    decision: str
    bias: str
    conviction: int
    setup: str
    entry_type: str
    entry: float | None
    entry_zone_low: float | None
    entry_zone_high: float | None
    stop_loss: float | None
    take_profit_1: float | None
    take_profit_2: float | None
    expected_hold_hours: float | None
    entry_valid_hours: float | None
    probability_tp1_before_stop: float | None
    thesis: str
    market_context: str
    trend_and_structure: str
    key_levels: str
    positioning_and_flow: str
    news_and_catalysts: str
    confluences: list[str]
    risks: list[str]
    invalidation: str
    what_to_watch: list[str]
    no_trade_reason: str | None
    valid_json: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_decision(text: str) -> Decision:
    """The model's answer, validated: unknown values become a NO_TRADE, numbers must be finite."""
    d = _json(text)
    decision = _str(d.get("decision")).upper().replace(" ", "_")
    valid = decision in DECISIONS
    entry_type = _str(d.get("entry_type")).lower()
    bias = _str(d.get("bias")).lower()
    try:
        conviction = int(round(float(d.get("conviction", 0))))
    except (TypeError, ValueError):
        conviction = 0
    prob = _num(d.get("probability_tp1_before_stop"), 3)
    if prob is not None and prob > 1.0:
        prob = prob / 100.0  # "62" meant 62%
    return Decision(
        decision=decision if valid else "NO_TRADE",
        bias=bias if bias in ("bullish", "bearish", "neutral") else "neutral",
        conviction=max(0, min(100, conviction)),
        setup=_str(d.get("setup"), 120),
        entry_type=entry_type if entry_type in ENTRY_TYPES else "market",
        entry=_num(d.get("entry")), entry_zone_low=_num(d.get("entry_zone_low")),
        entry_zone_high=_num(d.get("entry_zone_high")), stop_loss=_num(d.get("stop_loss")),
        take_profit_1=_num(d.get("take_profit_1")), take_profit_2=_num(d.get("take_profit_2")),
        expected_hold_hours=_num(d.get("expected_hold_hours"), 4), entry_valid_hours=_num(d.get("entry_valid_hours"), 4),
        probability_tp1_before_stop=max(0.0, min(1.0, prob)) if prob is not None else None,
        thesis=_str(d.get("thesis"), 700), market_context=_str(d.get("market_context")),
        trend_and_structure=_str(d.get("trend_and_structure")), key_levels=_str(d.get("key_levels")),
        positioning_and_flow=_str(d.get("positioning_and_flow")), news_and_catalysts=_str(d.get("news_and_catalysts")),
        confluences=_strings(d.get("confluences")), risks=_strings(d.get("risks")),
        invalidation=_str(d.get("invalidation"), 300), what_to_watch=_strings(d.get("what_to_watch"), 4),
        no_trade_reason=_str(d.get("no_trade_reason"), 400) or None,
        valid_json=valid,
    )


def parse_review(text: str) -> dict[str, Any]:
    d = _json(text)
    verdict = _str(d.get("verdict")).lower()
    try:
        adjusted = int(round(float(d.get("adjusted_conviction", 0))))
    except (TypeError, ValueError):
        adjusted = 0
    return {
        "verdict": verdict if verdict in VERDICTS else "reduce",
        "adjusted_conviction": max(0, min(100, adjusted)),
        "summary": _str(d.get("summary"), 400) or "no summary",
        "issues": _strings(d.get("issues"), 6),
        "valid_json": verdict in VERDICTS,
    }


# ----------------------------------------------------------------------------- plan checks


@dataclass
class PlanCheck:
    label: SignalLabel  # BUY / STRONG BUY = a trade in `side`; WATCH = an idea that failed a check; NO TRADE
    side: str | None  # long | short
    status: str  # trade | wait | idea | no_trade
    notes: list[str]
    plan: dict[str, Any] | None = None


def net_reward_risk(entry: float, stop: float, target: float, cost_pct: float) -> float:
    risk = abs(entry - stop) / entry * 100.0 + cost_pct
    reward = abs(target - entry) / entry * 100.0 - cost_pct
    return reward / risk if risk > 0 else 0.0


def check_plan(
    d: Decision,
    *,
    market: str,
    horizon: str,
    price: float,
    atr: float | None,
    cost_pct: float,
    now: datetime,
    min_reward_risk: float = 1.5,
    min_conviction: int = 60,
    strong_conviction: int = 75,
    min_stop_cost_multiple: float = 2.5,
) -> PlanCheck:
    """Execution checks on the analyst's plan. Never changes the direction or invents levels."""
    if d.decision == "NO_TRADE":
        return PlanCheck(SignalLabel.NO_TRADE, None, "no_trade", [d.no_trade_reason or "the analyst sees no trade"])
    side = "long" if d.decision == "LONG" else "short"
    if market == "spot" and side == "short":
        return PlanCheck(SignalLabel.NO_TRADE, None, "no_trade",
                         ["bearish view: spot cannot be shorted (see the futures market for a short)"])
    entry, stop, tp1, tp2 = d.entry, d.stop_loss, d.take_profit_1, d.take_profit_2
    if not entry or not stop or not tp1 or entry <= 0 or stop <= 0 or tp1 <= 0:
        return PlanCheck(SignalLabel.WATCH, side, "idea", ["the plan is incomplete (entry, stop or target missing)"])
    sign = 1.0 if side == "long" else -1.0
    notes: list[str] = []
    entry_type = d.entry_type
    # an order type that would fill at once is a market order
    if entry_type == "limit" and sign * (entry - price) >= 0:
        entry_type = "market"
    if entry_type == "stop" and sign * (entry - price) <= 0:
        entry_type = "market"
    if entry_type == "market":
        if abs(entry - price) > 0.25 * abs(entry - stop):
            notes.append(f"the analyst's entry {_num(entry)} differs from the price {_num(price)}; a market order fills at the price")
        entry = price
    if sign * (stop - entry) >= 0:
        return PlanCheck(SignalLabel.NO_TRADE, side, "no_trade",
                         [f"the price is already beyond the stop ({_num(stop)})" if entry_type == "market"
                          else "the stop is on the wrong side of the entry"])
    if tp2 is not None and sign * (tp2 - tp1) <= 0:
        tp2 = None
    if sign * (tp1 - entry) <= 0:
        return PlanCheck(SignalLabel.WATCH, side, "idea",
                         ["the price is already at the first target" if entry_type == "market"
                          else "the first target is on the wrong side of the entry"])
    risk_pct = abs(entry - stop) / entry * 100.0
    final = tp2 if tp2 is not None else tp1
    rr1 = net_reward_risk(entry, stop, tp1, cost_pct)
    rr_final = net_reward_risk(entry, stop, final, cost_pct)
    hold = min(MAX_HOLD_HOURS[horizon], d.expected_hold_hours or MAX_HOLD_HOURS[horizon]) if (d.expected_hold_hours or 0) > 0 \
        else MAX_HOLD_HOURS[horizon]
    wait = min(MAX_ENTRY_WAIT_HOURS[horizon], d.entry_valid_hours or MAX_ENTRY_WAIT_HOURS[horizon]) \
        if (d.entry_valid_hours or 0) > 0 else MAX_ENTRY_WAIT_HOURS[horizon]
    if entry_type == "market":
        zone = (min(entry, entry - sign * 0.15 * abs(entry - stop)), max(entry, entry - sign * 0.15 * abs(entry - stop)))
    else:
        low = d.entry_zone_low if d.entry_zone_low and d.entry_zone_low > 0 else entry
        high = d.entry_zone_high if d.entry_zone_high and d.entry_zone_high > 0 else entry
        zone = (min(low, high, entry), max(low, high, entry))
    plan = {
        "side": side, "entry_type": entry_type, "entry": entry, "entry_low": zone[0], "entry_high": zone[1],
        "stop": stop, "tp1": tp1, "tp2": tp2, "risk_pct": risk_pct, "cost_pct": cost_pct,
        "reward_risk_tp1": rr1, "reward_risk_final": rr_final, "hold_hours": hold,
        "entry_valid_until": (now + timedelta(hours=wait)) if entry_type != "market" else None,
        "exit_until": now + timedelta(hours=(wait if entry_type != "market" else 0.0) + hold),
        "price_at_signal": price,
    }
    problems: list[str] = []
    if entry_type != "market" and atr and abs(entry - price) > 3.0 * atr:
        problems.append(f"the entry is {abs(entry - price) / atr:.1f} ATR from the price: a plan for later, not an order now")
    if risk_pct < min_stop_cost_multiple * cost_pct:
        problems.append(f"the stop ({risk_pct:.2f}%) is too tight for the costs ({cost_pct:.2f}% round trip)")
    if atr and abs(entry - stop) < 0.3 * atr:
        problems.append(f"the stop is {abs(entry - stop) / atr:.2f} ATR away: inside normal noise")
    if risk_pct > MAX_STOP_PCT[horizon]:
        problems.append(f"the stop ({risk_pct:.1f}%) is wider than {MAX_STOP_PCT[horizon]:g}% for this horizon")
    if rr_final < min_reward_risk:
        problems.append(f"net reward:risk {rr_final:.2f} after costs is below {min_reward_risk:g}")
    if d.conviction < min_conviction:
        problems.append(f"conviction {d.conviction} is below {min_conviction}")
    if problems:
        return PlanCheck(SignalLabel.WATCH, side, "idea", notes + problems, plan)
    label = SignalLabel.STRONG_BUY if d.conviction >= strong_conviction and rr_final >= 2.0 else SignalLabel.BUY
    return PlanCheck(label, side, "trade" if entry_type == "market" else "wait", notes, plan)


def apply_review(check: PlanCheck, conviction: int, review: dict[str, Any] | None, *, min_conviction: int = 60,
                 strong_conviction: int = 75) -> tuple[PlanCheck, int]:
    """The risk manager can lower a trade (reduce: lower conviction; reject: WATCH), never raise it."""
    if review is None or check.label.rank < SignalLabel.BUY.rank:
        return check, conviction
    if review["verdict"] == "reject":
        check.label, check.status = SignalLabel.WATCH, "idea"
        check.notes = [f"risk manager rejected it: {review['summary']}"] + check.notes
        return check, conviction
    if review["verdict"] == "reduce":
        conviction = min(conviction, review["adjusted_conviction"] or conviction)
        if conviction < min_conviction:
            check.label, check.status = SignalLabel.WATCH, "idea"
            check.notes = [f"risk manager lowered conviction to {conviction}: {review['summary']}"] + check.notes
        elif check.label == SignalLabel.STRONG_BUY and conviction < strong_conviction:
            check.label = SignalLabel.BUY
    return check, conviction


def label_text(market: str, side: str | None, label: SignalLabel) -> str:
    if label in (SignalLabel.BUY, SignalLabel.STRONG_BUY):
        strong = "STRONG " if label == SignalLabel.STRONG_BUY else ""
        if market == "spot":
            return f"{strong}BUY"
        return f"{strong}{'LONG' if side == 'long' else 'SHORT'}"
    return label.value
