"""Market and per-coin sentiment (Phase 5).

Market mood combines three transparent components, each scaled to -1 (fear) .. +1 (greed):
* Fear & Greed index (alternative.me, 30-day history for the trend), weight 0.5
* Perpetual funding across the universe (Binance futures, public), weight 0.2. Funding
  above the 0.01%/8h baseline means traders pay to stay long.
* News tone of the last 48 hours (keyword method, see news.py), weight 0.3. News is fetched
  only when the user loads it; sentiment reads whatever headlines are cached.

Per coin: news tone, funding rate and labelled exchange flows from on-chain data.

Sentiment is context. It adds risk notes to signals but never changes a label or score,
because there is no measured evidence yet that it improves them (see Phase 8).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from statistics import fmean
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.timeutil import utcnow
from app.data.health import ProviderHealthRegistry
from app.models import SentimentReading
from app.services.cache import AsyncTTLCache
from app.services.news import NewsDigest, NewsService
from app.services.onchain import OnChainDigest, OnChainService, SourceError, fetch_json

log = logging.getLogger(__name__)

FUNDING_BASELINE_PCT = 0.01
FUNDING_HOT_PCT = 0.05
FUNDING_COLD_PCT = -0.03
NEWS_WINDOW = timedelta(hours=48)
WEIGHTS = {"fear_greed": 0.5, "news": 0.3, "funding": 0.2}


def mood_state(score: float) -> str:
    if score <= -0.6:
        return "EXTREME_FEAR"
    if score <= -0.2:
        return "FEAR"
    if score < 0.2:
        return "NEUTRAL"
    if score < 0.6:
        return "GREED"
    return "EXTREME_GREED"


def _clip(value: float) -> float:
    return max(-1.0, min(1.0, value))


@dataclass
class AssetSentiment:
    symbol: str
    state: str  # POSITIVE | NEGATIVE | MIXED | QUIET
    news_score: float | None
    headlines: int
    positive: int
    negative: int
    funding_rate_pct: float | None
    exchange_inflow_usd: float | None
    exchange_outflow_usd: float | None
    notes: list[str] = field(default_factory=list)
    recent_titles: list[str] = field(default_factory=list)


@dataclass
class SentimentDigest:
    computed_at: datetime
    state: str
    score: float | None
    trend: str
    components: dict[str, Any]
    reasons: list[str]
    assets: dict[str, AssetSentiment]
    errors: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def fear_greed_component(payload: Any) -> dict[str, Any] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    values = []
    for item in data or []:
        try:
            values.append((int(item["timestamp"]), int(item["value"]), str(item.get("value_classification", ""))))
        except (KeyError, TypeError, ValueError):
            continue
    if not values:
        return None
    values.sort(reverse=True)  # newest first
    current = values[0][1]
    week = [v for _, v, _ in values[:7]]
    week_ago = values[7][1] if len(values) > 7 else values[-1][1]
    return {
        "source": "alternative.me",
        "value": current,
        "classification": values[0][2],
        "avg_7d": round(fmean(week), 1),
        "change_7d": current - week_ago,
        "avg_30d": round(fmean(v for _, v, _ in values[:30]), 1),
        "score": _clip((current - 50) / 50),
    }


def funding_rates(payload: Any, symbols: dict[str, str]) -> dict[str, float]:
    """Latest funding per base asset in percent per 8h. `symbols`: futures symbol -> base."""
    out: dict[str, float] = {}
    for item in payload if isinstance(payload, list) else []:
        base = symbols.get(item.get("symbol")) if isinstance(item, dict) else None
        try:
            if base:
                out[base] = float(item["lastFundingRate"]) * 100
        except (KeyError, TypeError, ValueError):
            continue
    return out


def news_by_asset(news: NewsDigest | None, now: datetime) -> tuple[dict[str, list[Any]], list[Any]]:
    if news is None:
        return {}, []
    recent = [n for n in news.items if n.published_at is None or now - n.published_at <= NEWS_WINDOW]
    grouped: dict[str, list[Any]] = {}
    for item in recent:
        for symbol in item.assets:
            grouped.setdefault(symbol, []).append(item)
    return grouped, recent


def tone(items: list[Any]) -> tuple[float | None, int, int]:
    if not items:
        return None, 0, 0
    pos = sum(1 for i in items if i.sentiment == "positive")
    neg = sum(1 for i in items if i.sentiment == "negative")
    return round((pos - neg) / len(items), 3), pos, neg


class SentimentService:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        health: ProviderHealthRegistry,
        news: NewsService,
        onchain: OnChainService,
        universe_symbols: Callable[[], list[str]],
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._s = settings
        self._http = http
        self._health = health
        self._news = news
        self._onchain = onchain
        self._symbols = universe_symbols
        self._sessions = session_factory
        self._cache = AsyncTTLCache()

    def cached(self) -> SentimentDigest | None:
        entry = self._cache.peek("sentiment")
        return entry[0] if entry else None

    def for_asset(self, symbol: str) -> AssetSentiment | None:
        digest = self.cached()
        return digest.assets.get(symbol.upper()) if digest else None

    async def digest(self, *, force: bool = False) -> SentimentDigest:
        entry = self._cache.peek("sentiment")
        if entry is not None and "news" not in entry[0].components and self._news.cached() is not None:
            force = True  # headlines were loaded since: include their tone
        if force and entry is not None and entry[1] < self._s.min_refresh_seconds:
            force = False
        return await self._cache.get_or_load("sentiment", lambda: self._load(force), self._s.sentiment_cache_seconds, force=force)

    async def _load(self, force: bool) -> SentimentDigest:
        symbols = [s for s in self._symbols() if s]
        futures = {f"{s}USDT": s for s in symbols}
        fng_job = fetch_json(self._http, self._health, "alternative_me", "sentiment", f"{self._s.alternative_me_base_url.rstrip('/')}/fng/", {"limit": 30})
        funding_job = fetch_json(self._http, self._health, "binance_futures", "sentiment", f"{self._s.binance_futures_url.rstrip('/')}/fapi/v1/premiumIndex")
        fng_raw, funding_raw, onchain = await asyncio.gather(
            fng_job, funding_job, self._onchain.digest(force=force), return_exceptions=True
        )
        news: NewsDigest | BaseException | None = self._news.cached()  # news loads only on request
        errors = [str(x) if isinstance(x, SourceError) else f"{type(x).__name__}: {x}"
                  for x in (fng_raw, funding_raw, onchain) if isinstance(x, BaseException)]
        news_d: NewsDigest | None = None if isinstance(news, BaseException) else news
        chain: OnChainDigest | None = None if isinstance(onchain, BaseException) else onchain
        now = utcnow()

        components: dict[str, Any] = {}
        reasons: list[str] = []
        fg = None if isinstance(fng_raw, BaseException) else fear_greed_component(fng_raw)
        if fg:
            components["fear_greed"] = fg
            reasons.append(f"Fear & Greed {fg['value']} ({fg['classification']}), {fg['change_7d']:+d} over 7 days")
        rates = {} if isinstance(funding_raw, BaseException) else funding_rates(funding_raw, futures)
        if rates:
            avg = fmean(rates.values())
            hot = sorted(s for s, r in rates.items() if r >= FUNDING_HOT_PCT)
            cold = sorted(s for s, r in rates.items() if r <= FUNDING_COLD_PCT)
            components["funding"] = {
                "source": "binance futures", "avg_pct_8h": round(avg, 4), "coins": len(rates),
                "crowded_longs": hot, "crowded_shorts": cold,
                "score": _clip((avg - FUNDING_BASELINE_PCT) / 0.04),
            }
            reasons.append(f"average perpetual funding {avg:.4f}% per 8h across {len(rates)} coins")
        grouped, recent = news_by_asset(news_d, now)
        market_tone, pos, neg = tone(recent)
        if market_tone is not None:
            components["news"] = {"source": "headlines (keyword tone)", "headlines_48h": len(recent),
                                  "positive": pos, "negative": neg, "score": market_tone}
            reasons.append(f"{len(recent)} headlines in 48h: {pos} positive, {neg} negative")
        elif news_d is None:
            reasons.append("news not loaded: press Load in the news card to include headline tone")
        if chain and chain.stablecoins.get("change_7d_pct") is not None:
            change = chain.stablecoins["change_7d_pct"]
            components["stablecoins"] = {"source": "defillama", "change_7d_pct": change,
                                         "total_usd": chain.stablecoins.get("total_usd")}
            reasons.append(f"stablecoin supply {change:+.2f}% over 7 days ({'liquidity inflow' if change > 0 else 'liquidity outflow'})")

        weighted = [(WEIGHTS[k], components[k]["score"]) for k in WEIGHTS if k in components]
        score = round(sum(w * v for w, v in weighted) / sum(w for w, _ in weighted), 3) if weighted else None
        state = mood_state(score) if score is not None else "UNKNOWN"
        trend = "FLAT"
        if fg:
            trend = "RISING" if fg["change_7d"] >= 5 else "FALLING" if fg["change_7d"] <= -5 else "FLAT"

        assets: dict[str, AssetSentiment] = {}
        flows = chain.flows if chain else {}
        for symbol in symbols:
            items = grouped.get(symbol, [])
            score_a, p, n = tone(items)
            rate = rates.get(symbol)
            flow = flows.get(symbol, {})
            notes: list[str] = []
            if rate is not None and rate >= FUNDING_HOT_PCT:
                notes.append(f"perpetual funding {rate:.3f}% per 8h: crowded longs, squeeze risk")
            elif rate is not None and rate <= FUNDING_COLD_PCT:
                notes.append(f"negative funding {rate:.3f}% per 8h: shorts crowded (volatile both ways)")
            if n >= 2 and score_a is not None and score_a <= -0.3:
                notes.append(f"news tone negative: {n} of {len(items)} recent headlines")
            inflow, outflow = flow.get("exchange_inflow"), flow.get("exchange_outflow")
            if inflow and inflow >= 10_000_000 and inflow > (outflow or 0) * 2:
                notes.append(f"large labelled exchange inflows (${inflow / 1e6:,.0f}M in the last scan): possible selling")
            if not items:
                label = "QUIET"
            elif score_a is not None and score_a >= 0.25:
                label = "POSITIVE"
            elif score_a is not None and score_a <= -0.25:
                label = "NEGATIVE"
            else:
                label = "MIXED"
            assets[symbol] = AssetSentiment(
                symbol=symbol, state=label, news_score=score_a, headlines=len(items), positive=p, negative=n,
                funding_rate_pct=round(rate, 4) if rate is not None else None,
                exchange_inflow_usd=inflow, exchange_outflow_usd=outflow, notes=notes,
                recent_titles=[i.title for i in items[:5]],
            )
        digest = SentimentDigest(now, state, score, trend, components, reasons, assets, errors)
        await self._persist(digest)
        return digest

    async def _persist(self, d: SentimentDigest) -> None:
        if self._sessions is None:
            return
        rows = [SentimentReading(scope="market", symbol=None, computed_at=d.computed_at, state=d.state[:16],
                                 score=d.score, sample_size=len(d.components), trend=d.trend,
                                 details={"components": d.components, "reasons": d.reasons})]
        rows += [
            SentimentReading(scope="asset", symbol=a.symbol, computed_at=d.computed_at, state=a.state, score=a.news_score,
                             sample_size=a.headlines, trend=None,
                             details={"funding_rate_pct": a.funding_rate_pct, "notes": a.notes,
                                      "exchange_inflow_usd": a.exchange_inflow_usd, "exchange_outflow_usd": a.exchange_outflow_usd})
            for a in d.assets.values() if a.headlines or a.notes
        ]
        try:
            async with self._sessions() as session:
                session.add_all(rows)
                await session.commit()
        except Exception:
            log.exception("sentiment persistence failed")

