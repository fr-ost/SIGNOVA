"""The AI reads the headlines (Phase 10).

One JSON-mode request per set of headlines gives, for every coin they mention, the likely
effect on its spot price over the next days (-2..+2), whether anything makes buying now
reckless (hack, delisting, insolvency, halted withdrawals, charges against the project), and
one line why. The reading is cached by the set of headlines (AI_NEWS_CACHE_MINUTES), so scans
never pay twice for the same news. It needs an OpenAI key and can be switched off; without it
the evidence board falls back to the keyword tone.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from app.config import Settings
from app.core.timeutil import utcnow
from app.services.cache import AsyncTTLCache
from app.services.chat import ChatService, ChatUnavailable
from app.services.settings_store import SettingsStore

log = logging.getLogger(__name__)

MAX_HEADLINES = 60
PROMPT = """You read crypto news headlines for Signova, a crypto trading-signal dashboard (holding hours to days).
For each coin tagged in the headlines, judge ONLY from these headlines how the news is likely to move
its spot price over the next 1-3 days.

impact (integer): -2 clearly harmful (hack or exploit of the project, delisting from a major exchange,
charges against the project), -1 mildly negative, 0 no real effect or unclear, +1 mildly positive,
+2 clearly positive (listing on a major exchange, ETF approval, a major integration).
critical (boolean): true only when buying now would be reckless: the project was hacked or exploited,
delisted by a major exchange, is insolvent, halted withdrawals or its chain halted, or a regulator charged it.
Price-recap headlines ("X rises 5%"), predictions and general market commentary are impact 0.
Never invent facts. Include only coins tagged in the headlines.

Answer with JSON only:
{"coins": {"SYMBOL": {"impact": -2..2, "critical": true|false, "reason": "one short sentence"}}}"""


def parse_reading(text: str, allowed: set[str]) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[start : end + 1]) if 0 <= start < end else {}
        except ValueError:
            data = {}
    coins = data.get("coins") if isinstance(data, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for symbol, value in (coins.items() if isinstance(coins, dict) else []):
        sym = str(symbol).upper().strip()
        if sym not in allowed or not isinstance(value, dict):
            continue
        try:
            impact = max(-2, min(2, int(round(float(value.get("impact", 0))))))
        except (TypeError, ValueError):
            impact = 0
        out[sym] = {"impact": impact, "critical": value.get("critical") is True,
                    "reason": str(value.get("reason", "")).strip()[:240]}
    return out


class AINewsReader:
    def __init__(self, settings: Settings, chat: ChatService, store: SettingsStore,
                 blocked: Callable[[], bool] | None = None) -> None:
        self._s = settings
        self._chat = chat
        self._store = store
        self._blocked = blocked or (lambda: False)
        self._cache = AsyncTTLCache(prune_expired=True)
        self.enabled = True
        self.last: dict[str, Any] | None = None

    async def load(self) -> None:
        self.enabled = bool(await self._store.get("ai_news_enabled", True))

    async def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        await self._store.set("ai_news_enabled", enabled)

    @property
    def available(self) -> bool:
        return self.enabled and self._chat.configured and not self._blocked()

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "configured": self._chat.configured, "last": self.last}

    def cached(self) -> dict[str, dict[str, Any]]:
        return (self.last or {}).get("coins", {}) if self.last else {}

    async def read(self, items: Sequence[Any], symbols: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        """Per-coin reading of `items` (NewsEntry: title, source, published_at, assets). Without
        `symbols`, every coin tagged in the headlines (one reading serves every scan)."""
        if not self.available:
            return {}
        now = utcnow()
        wanted = {s.upper() for s in symbols} if symbols else {a.upper() for n in items for a in n.assets}
        recent = [n for n in items if (n.published_at is None or now - n.published_at <= timedelta(hours=48))
                  and wanted.intersection(a.upper() for a in n.assets)][:MAX_HEADLINES]
        if not recent:
            return {}
        lines = []
        for n in recent:
            age = f"{(now - n.published_at).total_seconds() / 3600:.0f}h ago" if n.published_at else "recent"
            tags = ",".join(sorted(wanted.intersection(a.upper() for a in n.assets)))
            lines.append(f"- [{tags}] {n.title} ({n.source}, {age})")
        key = hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()[:24]

        async def load() -> dict[str, dict[str, Any]]:
            return await self._ask(lines, wanted, len(recent))

        try:
            return await self._cache.get_or_load(key, load, self._s.ai_news_cache_minutes * 60)
        except ChatUnavailable as exc:
            log.warning("AI news reading failed", extra={"error": exc.message})
            self.last = {"computed_at": now, "error": exc.message, "coins": {}, "headlines": len(recent)}
            return {}

    async def _ask(self, lines: list[str], wanted: set[str], count: int) -> dict[str, dict[str, Any]]:
        messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": "HEADLINES:\n" + "\n".join(lines)}]
        errors: list[str] = []
        for candidate in self._chat.models_for(None):
            try:
                text, _ = await self._chat._complete(candidate, messages, None, json_mode=True)  # noqa: SLF001
            except ChatUnavailable as exc:
                if exc.status_code in (404, 204) or "model" in exc.message.lower():
                    errors.append(f"{candidate}: {exc.message}")
                    continue
                raise
            coins = parse_reading(text, wanted)
            self.last = {"computed_at": utcnow(), "model": candidate, "coins": coins, "headlines": count, "error": None}
            return coins
        raise ChatUnavailable("; ".join(errors) or "no model available", 502)
