"""AI review of signals (Phase 7): a second opinion from an OpenAI model that can only lower.

The deterministic engine decides. On request (or, if switched on, automatically for a few new
buy signals after a scan) the model reads one signal with all its data and answers in strict
JSON: agree, caution or reject, with the concrete risks it sees. In "advisory" mode (the
default) the verdict is shown next to the signal; in "filter" mode a reject also caps the
signal at WATCH. The model can never create or upgrade a signal, and its numbers are not
trusted: only the verdict, a short summary and the listed risks are kept.

Verdicts are stored with the signal (signals.ai_output), so the track record can show whether
the reviewer's "agree" signals actually did better than its "reject" signals.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.engine import STRATEGY as SWING_STRATEGY
from app.core.enums import SignalLabel
from app.core.timeutil import utcnow
from app.models import Signal
from app.services.chat import ChatService, ChatUnavailable
from app.services.settings_store import SettingsStore

log = logging.getLogger(__name__)

PROMPT_VERSION = "review-1"
VERDICTS = ("agree", "caution", "reject")
MAX_CONTEXT_CHARS = 14000

REVIEW_PROMPT = """You are the risk reviewer of a spot-only crypto trading dashboard.
You get ONE trade signal produced by a deterministic engine, with its data (SIGNAL, JSON).
Look for concrete reasons the trade could fail that the engine may have underweighted:
weak or small-sample backtest, price extended above its averages, resistance close above,
thin liquidity or a wide spread, timeframes disagreeing, crowded funding, token unlocks,
negative news tone, Bitcoin weakness, a stop that is too tight for costs. When an evidence board is
included (futures funding, open interest, long/short ratios, top traders, liquidations and the
estimated liquidation map, order flow, news and hype, market), weigh its factors too.

Rules:
- Use only the data in SIGNAL. Never invent prices, news, or statistics.
- You cannot create or upgrade a trade. Your verdict is one of: "agree", "caution", "reject".
- "reject" only for a concrete problem visible in the data; "caution" for real but minor concerns.
- Be brief and specific.

Answer with a JSON object only, exactly these keys:
{"verdict": "agree" | "caution" | "reject",
 "confidence": number between 0 and 1,
 "summary": "one or two sentences, max 240 characters",
 "risks": ["up to 5 short, specific risks"],
 "checks_before_entry": ["up to 4 things to confirm before buying"]}
"""


def _trim(value: Any, depth: int = 0) -> Any:
    """Keep the signal readable for the model: short lists, no huge nested blocks."""
    if isinstance(value, dict):
        drop = {"indicators", "structure", "equity", "recent", "records", "pipeline", "candles", "by_hour_utc", "by_weekday"}
        return {k: _trim(v, depth + 1) for k, v in value.items() if k not in drop and v is not None}
    if isinstance(value, list):
        return [_trim(v, depth + 1) for v in value[:24 if value and isinstance(value[0], str) else 8]]
    if isinstance(value, float):
        return float(f"{value:.6g}")
    return value


def compact_board(board: Any) -> Any:
    """The evidence board as short lines (the full board with its liquidation map is too long)."""
    if not isinstance(board, dict) or not isinstance(board.get("factors"), list):
        return board
    sign = {1: "+", -1: "-", 0: "0"}
    return {
        "score": board.get("score"), "grade": board.get("grade"), "vetoes": board.get("vetoes"),
        "notes": (board.get("notes") or [])[:4],
        "factors": [f"{sign.get(f.get('direction'), '0')} {f.get('label')}: {f.get('value')} ({f.get('detail')})"
                    for f in board["factors"] if isinstance(f, dict)][:24],
        "missing": board.get("missing"),
    }


def parse_review(text: str) -> dict[str, Any]:
    """The model's JSON, validated: unknown verdicts become "caution", lists are capped."""
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1]) if 0 <= start < end else {}
    if not isinstance(data, dict):
        data = {}
    verdict = str(data.get("verdict", "")).strip().lower()
    try:
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5

    def strings(key: str, limit: int) -> list[str]:
        items = data.get(key) if isinstance(data.get(key), list) else []
        return [str(x)[:200] for x in items if str(x).strip()][:limit]

    return {
        "verdict": verdict if verdict in VERDICTS else "caution",
        "confidence": confidence,
        "summary": str(data.get("summary", "")).strip()[:300] or "no summary",
        "risks": strings("risks", 5),
        "checks_before_entry": strings("checks_before_entry", 4),
        "valid_json": verdict in VERDICTS,
    }


class AIReviewService:
    def __init__(
        self,
        chat: ChatService,
        store: SettingsStore,
        session_factory: async_sessionmaker[AsyncSession] | None,
        *,
        blocked: Callable[[], bool] | None = None,
    ) -> None:
        self._chat = chat
        self._store = store
        self._sessions = session_factory
        self._blocked = blocked or (lambda: False)
        self.mode = "advisory"  # advisory | filter
        self.auto = False
        self.auto_max = 3
        self.reviews: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._auto_task: asyncio.Task[None] | None = None
        self._stopping = False
        self.on_review: Callable[[dict[str, Any]], None] | None = None  # applies a verdict to the shown signal

    def schedule_auto(self, items: list[tuple[str, str, str, dict[str, Any]]]) -> bool:
        """Review a few new buy signals in the background (kind, symbol, horizon, signal)."""
        if not self.auto or not self._chat.configured or self._blocked() or not items:
            return False
        if self._auto_task is not None and not self._auto_task.done():
            return False
        self._stopping = False
        self._auto_task = asyncio.create_task(self._auto(items[: self.auto_max]), name="ai-auto-review")
        return True

    async def _auto(self, items: list[tuple[str, str, str, dict[str, Any]]]) -> None:
        for kind, symbol, horizon, signal in items:
            if self._stopping or self._blocked():
                return
            try:
                await self.review(kind, symbol, signal, horizon=horizon)
            except ChatUnavailable as exc:
                log.warning("automatic AI review failed", extra={"symbol": symbol, "error": exc.message})
            except Exception:
                log.exception("automatic AI review failed", extra={"symbol": symbol})

    async def stop(self) -> None:
        self._stopping = True
        if self._auto_task is not None and not self._auto_task.done():
            done, _ = await asyncio.wait({self._auto_task}, timeout=20)
            if not done:
                self._auto_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._auto_task

    async def load(self) -> None:
        data = await self._store.get("ai_review_settings")
        if isinstance(data, dict):
            self.mode = "filter" if data.get("mode") == "filter" else "advisory"
            self.auto = bool(data.get("auto", False))
            self.auto_max = max(1, min(10, int(data.get("auto_max", 3))))

    def settings(self) -> dict[str, Any]:
        return {"mode": self.mode, "auto": self.auto, "auto_max": self.auto_max, "available": self._chat.configured,
                "prompt_version": PROMPT_VERSION}

    async def update(self, *, mode: str | None = None, auto: bool | None = None, auto_max: int | None = None) -> dict[str, Any]:
        if mode is not None:
            if mode not in ("advisory", "filter"):
                raise ValueError("mode must be advisory or filter")
            self.mode = mode
        if auto is not None:
            self.auto = auto
        if auto_max is not None:
            self.auto_max = max(1, min(10, auto_max))
        await self._store.set("ai_review_settings", {"mode": self.mode, "auto": self.auto, "auto_max": self.auto_max})
        return self.settings()

    def get(self, kind: str, symbol: str, horizon: str = "") -> dict[str, Any] | None:
        return self.reviews.get((kind, symbol.upper(), horizon))

    def caps(self, review: dict[str, Any] | None) -> bool:
        """In filter mode a reject caps the signal at WATCH."""
        return bool(review) and self.mode == "filter" and review.get("verdict") == "reject"  # type: ignore[union-attr]

    async def review(self, kind: str, symbol: str, signal: dict[str, Any], *, horizon: str = "",
                     model: str | None = None) -> dict[str, Any]:
        if self._blocked():
            raise ChatUnavailable("emergency stop is engaged", 503)
        if not self._chat.configured:
            raise ChatUnavailable("OPENAI_API_KEY is not set on the server")
        signal = {k: (compact_board(v) if k in ("evidence", "board") else v) for k, v in signal.items()}
        context = json.dumps(_trim(signal), default=str, separators=(",", ":"))[:MAX_CONTEXT_CHARS]
        messages = [
            {"role": "system", "content": REVIEW_PROMPT},
            {"role": "user", "content": f"SIGNAL ({kind}{' ' + horizon if horizon else ''}):\n{context}"},
        ]
        errors: list[str] = []
        for candidate in self._chat.models_for(model):
            try:
                text, usage = await self._chat._complete(candidate, messages, None, json_mode=True)  # noqa: SLF001
            except ChatUnavailable as exc:
                if exc.status_code in (404, 204) or "model" in exc.message.lower():
                    errors.append(f"{candidate}: {exc.message}")
                    continue
                raise
            result = parse_review(text) | {
                "model": candidate, "kind": kind, "symbol": symbol.upper(), "horizon": horizon or None,
                "signal": signal.get("signal"), "reviewed_at": utcnow().isoformat(), "usage": usage,
                "mode": self.mode, "prompt_version": PROMPT_VERSION,
            }
            result["effect"] = (
                "capped at WATCH (filter mode)" if self.caps(result) and signal.get("signal") in ("BUY", "STRONG BUY")
                else "advisory only"
            )
            self.reviews[(kind, symbol.upper(), horizon)] = result
            await self._store_with_signal(kind, symbol.upper(), horizon, result)
            if self.on_review is not None:
                self.on_review(result)
            return result
        raise ChatUnavailable("; ".join(errors) or "no model available", 502)

    async def _store_with_signal(self, kind: str, symbol: str, horizon: str, review: dict[str, Any]) -> None:
        """Attach the verdict to the stored signal it reviewed (the latest one within a day)."""
        if self._sessions is None:
            return
        strategy = SWING_STRATEGY if kind == "swing" else f"scalp_{horizon}"
        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        select(Signal).where(Signal.symbol == symbol, Signal.strategy == strategy,
                                             Signal.created_at >= utcnow() - timedelta(days=1))
                        .order_by(Signal.created_at.desc()).limit(1)
                    )
                ).scalar_one_or_none()
                if row is None:
                    return
                row.ai_output = {k: v for k, v in review.items() if k != "usage"}
                row.ai_confirmed = review["verdict"] == "agree"
                row.model_name = str(review.get("model"))[:64]
                row.prompt_version = PROMPT_VERSION
                await session.commit()
        except Exception:
            log.exception("AI review could not be stored", extra={"symbol": symbol})


def capped_label(label: str, review: dict[str, Any] | None, service: AIReviewService) -> str:
    """The label to show after the review (filter mode only lowers BUY / STRONG BUY)."""
    if service.caps(review) and label in (SignalLabel.BUY.value, SignalLabel.STRONG_BUY.value):
        return SignalLabel.WATCH.value
    return label
