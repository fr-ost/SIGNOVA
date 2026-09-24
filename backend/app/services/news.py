"""Latest crypto news and trends from free public sources (no API keys).

Sources: RSS feeds of major crypto news sites, the CryptoCompare news API and CoinGecko
trending coins. Fetched only on request and cached (NEWS_CACHE_SECONDS). Every item keeps
its source and link. Headline sentiment is a transparent keyword count, labelled as such;
it is context for the user and the chat assistant, never an input to the signal engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.timeutil import parse_iso, utcnow
from app.models import NewsItem
from app.services.cache import AsyncTTLCache

log = logging.getLogger(__name__)

MAX_BYTES = 3_000_000
MAX_ITEMS = 60
MIN_REFRESH_SECONDS = 60
COINGECKO_TRENDING = "https://api.coingecko.com/api/v3/search/trending"

POSITIVE = {
    "surge", "surges", "soar", "soars", "rally", "rallies", "gain", "gains", "jump", "jumps", "bullish", "record",
    "high", "highs", "approve", "approval", "approved", "adopt", "adoption", "inflow", "inflows", "breakout",
    "rebound", "rebounds", "climb", "climbs", "upgrade", "partnership", "launch", "launches", "etf",
}
NEGATIVE = {
    "crash", "crashes", "plunge", "plunges", "drop", "drops", "fall", "falls", "bearish", "hack", "hacked",
    "exploit", "exploited", "lawsuit", "sue", "sues", "ban", "bans", "outflow", "outflows", "liquidation",
    "liquidations", "fraud", "scam", "selloff", "sell-off", "slump", "slumps", "tumble", "tumbles", "fine",
    "fined", "delist", "delisted", "investigation", "warning", "fear", "losses",
}
_WORD = re.compile(r"[a-z][a-z\-]+")
_TAG = re.compile(r"<[^>]+>")


@dataclass
class NewsEntry:
    title: str
    url: str
    source: str
    published_at: datetime | None
    summary: str
    assets: list[str] = field(default_factory=list)
    sentiment: str = "neutral"


@dataclass
class TrendingCoin:
    symbol: str
    name: str
    market_cap_rank: int | None
    price_change_24h_pct: float | None


@dataclass
class NewsDigest:
    fetched_at: datetime
    items: list[NewsEntry]
    trending: list[TrendingCoin]
    sentiment: dict[str, int]
    by_asset: dict[str, dict[str, int]]
    sources_ok: list[str]
    errors: list[str]


def headline_sentiment(text: str) -> str:
    words = _WORD.findall(text.lower())
    pos = sum(w in POSITIVE for w in words)
    neg = sum(w in NEGATIVE for w in words)
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _clean(text: str | None, limit: int = 400) -> str:
    plain = html.unescape(_TAG.sub(" ", text or ""))
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:limit]


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return parse_iso(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(content: bytes, source: str) -> list[NewsEntry]:
    """RSS 2.0 and Atom."""
    root = ET.fromstring(content)
    out: list[NewsEntry] = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        link = ""
        for child in node:
            name = _local(child.tag)
            if name == "link":
                link = child.get("href") or (child.text or "").strip() or link
            elif name in ("title", "description", "summary", "pubDate", "published", "updated") and child.text:
                fields.setdefault(name, child.text)
        title = _clean(fields.get("title"), 300)
        if not title or not link.startswith("http"):
            continue
        out.append(
            NewsEntry(
                title=title,
                url=link,
                source=source,
                published_at=_date(fields.get("pubDate") or fields.get("published") or fields.get("updated")),
                summary=_clean(fields.get("description") or fields.get("summary")),
            )
        )
    return out


def parse_cryptocompare(payload: Any) -> list[NewsEntry]:
    data = payload.get("Data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if not isinstance(item, dict) or not item.get("title") or not str(item.get("url", "")).startswith("http"):
            continue
        info = item.get("source_info") if isinstance(item.get("source_info"), dict) else {}
        published = item.get("published_on")
        out.append(
            NewsEntry(
                title=_clean(item.get("title"), 300),
                url=str(item["url"]),
                source=str(info.get("name") or item.get("source") or "CryptoCompare"),
                published_at=datetime.fromtimestamp(int(published), tz=UTC) if isinstance(published, int | float) else None,
                summary=_clean(item.get("body")),
            )
        )
    return out


def parse_trending(payload: Any) -> list[TrendingCoin]:
    coins = payload.get("coins") if isinstance(payload, dict) else None
    out = []
    for wrapper in coins or []:
        item = wrapper.get("item") if isinstance(wrapper, dict) else None
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        change = None
        data = item.get("data")
        if isinstance(data, dict) and isinstance(data.get("price_change_percentage_24h"), dict):
            raw = data["price_change_percentage_24h"].get("usd")
            change = float(raw) if isinstance(raw, int | float) else None
        rank = item.get("market_cap_rank")
        out.append(
            TrendingCoin(
                symbol=str(item["symbol"]).upper(),
                name=str(item.get("name", "")),
                market_cap_rank=int(rank) if isinstance(rank, int) else None,
                price_change_24h_pct=change,
            )
        )
    return out


def tag_assets(entries: Sequence[NewsEntry], names: dict[str, str]) -> None:
    """Attach universe symbols mentioned by symbol (3+ letters, upper case) or by name."""
    patterns = []
    for symbol, name in names.items():
        alternatives = [re.escape(name.lower())] if len(name) >= 4 else []
        patterns.append((symbol, re.compile(r"\b(" + "|".join(alternatives) + r")\b") if alternatives else None,
                         re.compile(r"\b" + re.escape(symbol) + r"\b") if len(symbol) >= 3 else None))
    for entry in entries:
        text = f"{entry.title} {entry.summary}"
        lower = text.lower()
        for symbol, by_name, by_symbol in patterns:
            if (by_name and by_name.search(lower)) or (by_symbol and by_symbol.search(text)):
                entry.assets.append(symbol)


class NewsService:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        feeds: Sequence[str],
        cryptocompare_url: str | None,
        cache_seconds: float,
        asset_names: Callable[[], dict[str, str]] | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        trending_url: str | None = COINGECKO_TRENDING,
    ) -> None:
        self._http = http
        self._feeds = list(feeds)
        self._cc = cryptocompare_url
        self._trending = trending_url
        self._ttl = cache_seconds
        self._names = asset_names
        self._sessions = session_factory
        self._cache = AsyncTTLCache()

    def cached(self) -> NewsDigest | None:
        entry = self._cache.peek("news")
        return entry[0] if entry else None

    async def digest(self, *, force: bool = False) -> NewsDigest:
        entry = self._cache.peek("news")
        if force and entry is not None and entry[1] < MIN_REFRESH_SECONDS:
            force = False  # protect the free sources from rapid refreshes
        return await self._cache.get_or_load("news", self._load, self._ttl, force=force)

    async def _get(self, url: str) -> httpx.Response:
        response = await self._http.get(
            url, timeout=12.0, headers={"Accept": "application/rss+xml, application/xml, application/json, */*"},
            follow_redirects=True,
        )
        response.raise_for_status()
        if len(response.content) > MAX_BYTES:
            raise ValueError("response too large")
        return response

    async def _feed(self, url: str) -> list[NewsEntry]:
        source = urlsplit(url).hostname or url
        source = source.removeprefix("www.")
        return parse_feed((await self._get(url)).content, source)

    async def _load(self) -> NewsDigest:
        jobs: dict[str, Any] = {url: self._feed(url) for url in self._feeds}
        if self._cc:
            jobs["cryptocompare"] = self._get(self._cc)
        if self._trending:
            jobs["coingecko trending"] = self._get(self._trending)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        items: list[NewsEntry] = []
        trending: list[TrendingCoin] = []
        ok: list[str] = []
        errors: list[str] = []
        for name, result in zip(jobs, results, strict=True):
            label = urlsplit(name).hostname or name
            if isinstance(result, BaseException):
                errors.append(f"{label}: {type(result).__name__}: {str(result)[:120]}")
                continue
            try:
                if name == "cryptocompare":
                    items.extend(parse_cryptocompare(result.json()))
                elif name == "coingecko trending":
                    trending = parse_trending(result.json())
                else:
                    items.extend(result)
                ok.append(label)
            except (ValueError, ET.ParseError) as exc:
                errors.append(f"{label}: unreadable response ({exc})"[:160])

        seen: set[str] = set()
        unique: list[NewsEntry] = []
        for entry in sorted(items, key=lambda e: e.published_at or datetime.min.replace(tzinfo=UTC), reverse=True):
            key = re.sub(r"\W+", "", entry.title.lower())[:80]
            if key in seen or entry.url in seen:
                continue
            seen.update({key, entry.url})
            unique.append(entry)
        unique = unique[:MAX_ITEMS]
        for entry in unique:
            entry.sentiment = headline_sentiment(f"{entry.title} {entry.summary}")
        if self._names is not None:
            tag_assets(unique, self._names())
        by_asset: dict[str, dict[str, int]] = {}
        for entry in unique:
            for symbol in entry.assets:
                counts = by_asset.setdefault(symbol, {"positive": 0, "negative": 0, "neutral": 0})
                counts[entry.sentiment] += 1
        digest = NewsDigest(
            fetched_at=utcnow(),
            items=unique,
            trending=trending,
            sentiment=dict(Counter(e.sentiment for e in unique)),
            by_asset=by_asset,
            sources_ok=ok,
            errors=errors,
        )
        await self._persist(unique)
        return digest

    async def _persist(self, entries: Sequence[NewsEntry]) -> None:
        if self._sessions is None or not entries:
            return
        from app.services.persistence import _insert  # dialect-aware insert

        rows = [
            {
                "url": e.url[:2000],
                "content_hash": hashlib.sha256(e.url.encode()).hexdigest(),
                "title": e.title,
                "source": e.source[:128],
                "published_at": e.published_at,
                "collected_at": utcnow(),
                "assets": e.assets,
                "category": "news",
                "sentiment": e.sentiment,
                "is_official": False,
                "is_verified": False,
                "details": {"summary": e.summary, "sentiment_method": "keyword"},
            }
            for e in entries
        ]
        try:
            async with self._sessions() as session:
                stmt = _insert(session, NewsItem).values(rows)
                await session.execute(stmt.on_conflict_do_nothing())
                await session.commit()
        except Exception:
            log.exception("news persistence failed")
