"""Evidence board service (Phase 10): gathers every input of app.analysis.evidence for a coin.

Before a scan (`prepare`): headlines are reloaded when older than EVIDENCE_NEWS_MAX_AGE_MINUTES
(free sources) and read by the AI when that is switched on, the futures market listing is
refreshed, and news mention counts for the last 7 days are counted from the database. Per coin
(`for_coin`): the futures snapshot (cached), candles the analysis already has, and cached
context (market regime, on-chain flows, stablecoins, unlocks, altcoin season). Nothing here runs
in the background, and every source failure is listed on the board instead of being guessed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis import evidence as ev
from app.analysis.regime import MarketRegimeResult
from app.config import Settings
from app.core.enums import Timeframe
from app.core.timeutil import utcnow
from app.data.normalization.schemas import Candle
from app.models import NewsItem
from app.services.ai_news import AINewsReader
from app.services.derivatives import DerivativesService
from app.services.settings_store import SettingsStore

log = logging.getLogger(__name__)

MODES = ("filter", "advisory", "off")


class EvidenceService:
    def __init__(
        self,
        settings: Settings,
        derivatives: DerivativesService,
        store: SettingsStore,
        *,
        news: Any = None,
        ai_news: AINewsReader | None = None,
        onchain: Any = None,
        events: Any = None,
        context: Any = None,
        assets: Any = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        blocked: Callable[[], bool] | None = None,
        universe_symbols: Callable[[], list[str]] | None = None,
    ) -> None:
        self._s = settings
        self.derivatives = derivatives
        self._store = store
        self._news = news
        self.ai_news = ai_news
        self._onchain = onchain
        self._events = events
        self._context = context
        self._assets = assets
        self._sessions = session_factory
        self._blocked = blocked or (lambda: False)
        self.universe_symbols = universe_symbols or (lambda: [])
        self.mode = "filter"
        self.refresh_news = settings.evidence_refresh_news_default
        self.last: dict[tuple[str, str], ev.Evidence] = {}
        self._mentions: tuple[float, dict[str, tuple[int, float]]] | None = None
        self._btc_h1: tuple[float, list[Candle]] | None = None
        self._ai: dict[str, dict[str, Any]] = {}
        self.learner: Any = None  # app.services.learning.LearningService (set by the container)

    # ------------------------------------------------------------------ settings

    async def load(self) -> None:
        data = await self._store.get("evidence_settings", None)
        if isinstance(data, dict):
            self.mode = data.get("mode") if data.get("mode") in MODES else "filter"
            self.refresh_news = bool(data.get("refresh_news", self._s.evidence_refresh_news_default))
        if self.ai_news is not None:
            await self.ai_news.load()

    def settings(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "refresh_news": self.refresh_news, "derivatives": self.derivatives.enabled,
            "ai_news": self.ai_news.status() if self.ai_news is not None else {"enabled": False, "configured": False},
            "weights": ev.PRIOR_WEIGHTS, "labels": ev.LABELS, "groups": ev.GROUPS,
            "thresholds": {"strong_against": ev.STRONG_AGAINST, "against": ev.AGAINST, "for": ev.SUPPORTIVE,
                           "strong_for": ev.STRONG_FOR, "min_factors": ev.MIN_FACTORS},
        }

    async def update(self, *, mode: str | None = None, refresh_news: bool | None = None,
                     ai_news: bool | None = None) -> dict[str, Any]:
        if mode is not None:
            if mode not in MODES:
                raise ValueError("mode must be filter, advisory or off")
            self.mode = mode
        if refresh_news is not None:
            self.refresh_news = refresh_news
        if ai_news is not None and self.ai_news is not None:
            await self.ai_news.set_enabled(ai_news)
        await self._store.set("evidence_settings", {"mode": self.mode, "refresh_news": self.refresh_news})
        return self.settings()

    # ------------------------------------------------------------------ before a scan

    async def prepare(self, symbols: Sequence[str]) -> None:
        """Refresh the shared inputs once per scan (never fails the scan)."""
        if self.mode == "off" or self._blocked():
            return
        jobs: list[Any] = [self._refresh_news(symbols), self._count_mentions()]
        if self.learner is not None:
            jobs.append(self.learner.refresh())
        if self.derivatives.enabled:
            jobs.append(self.derivatives.market())
        for result in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(result, Exception):
                log.warning("evidence preparation step failed", extra={"error": f"{type(result).__name__}: {result}"})

    async def _refresh_news(self, symbols: Sequence[str]) -> None:
        if self._news is None:
            return
        digest = self._news.cached()
        stale = digest is None or (utcnow() - digest.fetched_at) > timedelta(minutes=self._s.evidence_news_max_age_minutes)
        if stale and self.refresh_news:
            digest = await self._news.digest(force=digest is not None)
        if digest is not None and self.ai_news is not None and self.ai_news.available:
            self._ai = await self.ai_news.read(digest.items)  # every tagged coin: one reading serves all scans

    async def _count_mentions(self) -> None:
        if self._mentions is not None and time.monotonic() - self._mentions[0] < 900:
            return
        counts24: Counter[str] = Counter()
        counts7: Counter[str] = Counter()
        now = utcnow()
        if self._sessions is not None:
            try:
                async with self._sessions() as session:
                    rows = (await session.execute(
                        select(NewsItem.published_at, NewsItem.assets).where(NewsItem.published_at >= now - timedelta(days=7))
                    )).all()
            except Exception:
                log.exception("news mention count failed")
                rows = []
            for published, assets in rows:
                for a in assets or []:
                    counts7[str(a).upper()] += 1
                    if published is not None and now - _aware(published) <= timedelta(hours=24):
                        counts24[str(a).upper()] += 1
        elif self._news is not None and self._news.cached() is not None:
            for n in self._news.cached().items:
                if n.published_at is not None and now - n.published_at <= timedelta(hours=24):
                    for a in n.assets:
                        counts24[a.upper()] += 1
        self._mentions = (time.monotonic(), {s: (counts24.get(s, 0), counts7.get(s, 0) / 7.0) for s in set(counts7) | set(counts24)})

    # ------------------------------------------------------------------ per coin

    async def _btc_hourly(self) -> list[Candle]:
        if self._btc_h1 is not None and time.monotonic() - self._btc_h1[0] < 300:
            return self._btc_h1[1]
        if self._assets is None:
            return []
        try:
            collection = await self._assets.collect("BTC")
        except Exception:
            return []
        candles = list(collection.closed.get(Timeframe.H1, []))
        self._btc_h1 = (time.monotonic(), candles)
        return candles

    def _headlines(self, symbol: str) -> list[tuple[datetime | None, str, str]]:
        digest = self._news.cached() if self._news is not None else None
        if digest is None:
            return []
        return [(n.published_at, n.title, n.sentiment) for n in digest.items if symbol in (a.upper() for a in n.assets)]

    def _trending(self, symbol: str) -> int | None:
        digest = self._news.cached() if self._news is not None else None
        for rank, coin in enumerate(digest.trending if digest else [], start=1):
            if coin.symbol.upper() == symbol:
                return rank
        return None

    def _unlock(self, symbol: str) -> dict[str, Any] | None:
        digest = self._events.cached_unlocks() if self._events is not None else None
        coin = next((c for c in digest.coins if c.symbol == symbol), None) if digest else None
        if coin is None or not coin.upcoming:
            return None
        soon = [e for e in coin.upcoming if e.days_until <= 14]
        if not soon:
            return None
        return {"days": soon[0].days_until, "pct": sum(e.pct_circulating or 0 for e in soon),
                "usd": sum(e.value_usd or 0 for e in soon) or None}

    def _chain(self, symbol: str) -> tuple[dict[str, Any] | None, float | None]:
        digest = self._onchain.cached() if self._onchain is not None else None
        if digest is None:
            return None, None
        flow = digest.flows.get(symbol) or {}
        flows = {"inflow": flow.get("exchange_inflow"), "outflow": flow.get("exchange_outflow")} if flow else None
        return flows, digest.stablecoins.get("change_7d_pct")

    def _altseason(self) -> int | None:
        cached = self._context.cached() if self._context is not None else None
        season = getattr(cached, "altcoin_season", None)
        return season.value if season is not None else None

    async def for_coin(
        self,
        symbol: str,
        *,
        horizon: str,
        price: float | None,
        market: MarketRegimeResult | None,
        h1: Sequence[Candle] = (),
        setup: Sequence[Candle] = (),
        d1: Sequence[Candle] = (),
        entry: float | None = None,
        stop: float | None = None,
        tp1: float | None = None,
        tp2: float | None = None,
        volume_24h_quote: float | None = None,
        change_24h_pct: float | None = None,
        rsi: float | None = None,
        atr_pct: float | None = None,
        book_imbalance: float | None = None,
    ) -> ev.Evidence | None:
        if self.mode == "off":
            return None
        sym = symbol.upper()
        deriv = None
        if self.derivatives.enabled and not self._blocked():
            try:
                deriv = await self.derivatives.snapshot(sym)
            except Exception as exc:  # the board shows what is missing; a coin never fails because of it
                log.warning("futures data failed", extra={"symbol": sym, "error": f"{type(exc).__name__}: {exc}"})
        market_deriv = None
        cached_market = self.derivatives.cached_market()
        if cached_market is not None:
            market_deriv = cached_market.summary(self.universe_symbols() or None)
        mentions = self._mentions[1].get(sym) if self._mentions else None
        flows, stable = self._chain(sym)
        inputs = ev.EvidenceInputs(
            symbol=sym, now=utcnow(), price=price, horizon=horizon, entry=entry, stop=stop, tp1=tp1, tp2=tp2,
            h1=h1, setup=setup, d1=d1, btc_h1=[] if sym == "BTC" else await self._btc_hourly(),
            volume_24h_quote=volume_24h_quote, change_24h_pct=change_24h_pct, rsi=rsi, atr_pct=atr_pct,
            book_imbalance=book_imbalance, deriv=deriv, market_deriv=market_deriv, headlines=self._headlines(sym),
            ai_news=self._ai.get(sym) if self.ai_news is not None and self.ai_news.enabled else None,
            mentions_24h=mentions[0] if mentions else (0 if self._mentions is not None else None),
            mentions_per_day=mentions[1] if mentions and self._sessions is not None else None,
            trending_rank=self._trending(sym), market=market, stablecoin_change_7d=stable, altseason=self._altseason(),
            unlock=self._unlock(sym), exchange_flow=flows,
        )
        result = await asyncio.to_thread(ev.build_evidence, inputs)
        self.last[(sym, horizon)] = result
        return result

    def apply(self, label: Any, evidence: ev.Evidence | None, horizon: str) -> tuple[Any, list[str], str | None]:
        """(label, reasons, filtered_by): the board, then the learned model when it is validated."""
        new, reasons = ev.apply_to_label(label, evidence, self.mode)
        filtered_by = "evidence" if new != label else None
        if self.learner is not None and evidence is not None and self.mode == "filter":
            capped, why = self.learner.gate(new, evidence, horizon)
            if capped != new:
                new, reasons, filtered_by = capped, reasons + why, filtered_by or "learned"
        return new, reasons, filtered_by


def _aware(value: datetime) -> datetime:
    from datetime import UTC

    return value if value.tzinfo else value.replace(tzinfo=UTC)
