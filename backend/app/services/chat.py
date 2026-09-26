"""AI chat assistant (OpenAI) grounded in the dashboard's own data.

Every request carries a compact context: the latest scan, the market regime, the news
digest, the user's portfolio and, when a coin is selected, its full deterministic
analysis. The assistant explains and discusses; it cannot change a signal. The system
prompt states that the engine's labels, risk checks and data-integrity results are
authoritative (the decision hierarchy: the LLM never overrides them).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

MAX_MESSAGES = 16
MAX_MESSAGE_CHARS = 4000
MAX_CONTEXT_CHARS = 24000
EFFORTS = ("minimal", "low", "medium", "high")
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

SYSTEM_PROMPT = """You are the analysis assistant of Signova, a crypto trading-signal dashboard (spot and futures).
You help the user think through their next trade using the data in CONTEXT.

Rules:
- Use only the numbers in CONTEXT for current prices, levels and signals. If something is
  not in CONTEXT, say you do not have it; never invent prices, news or statistics.
- The dashboard's deterministic engine is authoritative. Its labels (STRONG BUY, BUY,
  WATCH, NO TRADE), risk checks and data-integrity results cannot be overridden by you.
  Never present a NO TRADE or WATCH coin as a buy; you may explain what would need to change.
- You cannot place trades. Spot signals are buys only; futures signals can be long or short with the
  leverage plan shown in CONTEXT. Never suggest more leverage than the plan.
- Be concrete: entry zone, stop, targets, reward:risk and position size come from the plan.
  Mention the main risks and the invalidation level.
- Signals have no measured track record yet; nothing is guaranteed. Keep answers concise,
  use short lists, and end trade discussions with a one-line risk reminder.
"""


class ChatUnavailable(Exception):
    def __init__(self, message: str, status_code: int = 503, upstream_status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.upstream_status = upstream_status  # OpenAI's own HTTP status, when it answered


class ChatService:
    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        context: Callable[[str | None], Awaitable[dict[str, Any]]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._s = settings
        self._http = http
        self._context = context
        self._clock = clock
        self._recent: deque[float] = deque()
        self._models_cache: tuple[float, list[str] | None] | None = None

    @property
    def configured(self) -> bool:
        return self._s.openai_configured

    def _rate_limit(self) -> None:
        now = self._clock()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= max(1, self._s.chat_rate_limit_per_minute):
            raise ChatUnavailable("too many chat messages; wait a minute", 429)
        self._recent.append(now)

    @staticmethod
    def _clean(messages: list[dict[str, str]]) -> list[dict[str, str]]:
        cleaned = []
        for m in messages[-MAX_MESSAGES:]:
            role = m.get("role")
            content = str(m.get("content", "")).strip()
            if role in ("user", "assistant") and content:
                cleaned.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
        if not cleaned or cleaned[-1]["role"] != "user":
            raise ChatUnavailable("the last message must come from the user", 422)
        return cleaned

    @staticmethod
    def is_reasoning(model: str) -> bool:
        """GPT-5 and o-series models think before answering; the thinking uses output tokens."""
        m = model.lower()
        return m.startswith("gpt-5") or bool(re.match(r"^o\d", m))

    def models_for(self, preferred: str | None) -> list[str]:
        return list(dict.fromkeys([m for m in [preferred, *self._s.chat_models] if m]))

    async def account_models(self) -> list[str] | None:
        """Every model id this OpenAI key can use (None when the list is unavailable); cached for an hour."""
        await self.available_models()
        return self._models_cache[1] if self._models_cache else None

    async def available_models(self) -> dict[str, Any]:
        """The configured options, marked with what this OpenAI key can use (cached for an hour)."""
        options = list(dict.fromkeys([*self._s.chat_models, *self._s.chat_model_options]))
        now = self._clock()
        if self._models_cache is None or now - self._models_cache[0] > 3600:
            account: list[str] | None = None
            if self.configured and self._s.openai_api_key is not None:
                try:
                    response = await self._http.get(
                        f"{self._s.openai_base_url.rstrip('/')}/models",
                        headers={"Authorization": f"Bearer {self._s.openai_api_key.get_secret_value()}"},
                        timeout=httpx.Timeout(15.0, connect=10.0),
                    )
                    if response.status_code == 200:
                        account = sorted(str(m.get("id")) for m in response.json().get("data", []) if isinstance(m, dict))
                except (httpx.HTTPError, ValueError, AttributeError):
                    account = None
            self._models_cache = (now, account)
        account = self._models_cache[1]
        return {
            "default": self._s.chat_models[0],
            "fallbacks": self._s.chat_models[1:],
            "reasoning_effort": self._s.chat_reasoning_effort,
            "efforts": list(EFFORTS),
            "models": [
                {"id": m, "reasoning": self.is_reasoning(m), "available": None if account is None else m in account}
                for m in options
            ],
            "account_checked": account is not None,
        }

    async def reply(
        self,
        messages: list[dict[str, str]],
        symbol: str | None = None,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        if not self.configured or self._s.openai_api_key is None:
            raise ChatUnavailable("OPENAI_API_KEY is not set on the server")
        if model is not None and not MODEL_PATTERN.match(model):
            raise ChatUnavailable("invalid model name", 422)
        if reasoning_effort is not None and reasoning_effort not in EFFORTS:
            raise ChatUnavailable("reasoning effort must be one of " + ", ".join(EFFORTS), 422)
        conversation = self._clean(messages)
        self._rate_limit()
        context = await self._context(symbol)
        context_text = json.dumps(context, default=str, separators=(",", ":"))[:MAX_CONTEXT_CHARS]
        payload_messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "system", "content": f"CONTEXT (JSON, generated by the dashboard):\n{context_text}"},
            *conversation,
        ]
        errors: list[str] = []
        for candidate in self.models_for(model):
            try:
                text, usage = await self._complete(candidate, payload_messages, reasoning_effort)
            except ChatUnavailable as exc:
                if exc.status_code in (404, 204) or "model" in exc.message.lower():
                    errors.append(f"{candidate}: {exc.message}")
                    continue  # try the next model
                raise
            return {
                "reply": text, "model": candidate, "usage": usage, "fallback_from": errors or None,
                "context_symbols": context.get("symbols_in_context", []),
            }
        raise ChatUnavailable("; ".join(errors) or "no chat model available", 502)

    def _payload(
        self, model: str, messages: list[dict[str, str]], effort: str | None, json_mode: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.is_reasoning(model):
            chosen = effort or self._s.chat_reasoning_effort
            if chosen == "minimal" and not model.lower().startswith("gpt-5"):
                chosen = "low"  # o-series models have no "minimal" setting
            payload["reasoning_effort"] = chosen
            # the budget covers hidden reasoning plus the visible answer
            payload["max_completion_tokens"] = self._s.chat_max_output_tokens + self._s.chat_reasoning_budget_tokens
        else:
            payload["max_completion_tokens"] = self._s.chat_max_output_tokens
        return payload

    async def _complete(
        self, model: str, messages: list[dict[str, str]], effort: str | None = None, json_mode: bool = False
    ) -> tuple[str, dict[str, Any]]:
        return await self._post(self._payload(model, messages, effort, json_mode), 120.0)

    async def structured(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        name: str,
        schema: dict[str, Any],
        effort: str | None,
        max_tokens: int,
        timeout: float,
    ) -> tuple[str, dict[str, Any]]:
        """A JSON answer that follows `schema` (Structured Outputs). Models without schema support get
        plain JSON mode; a reasoning effort a model rejects is retried at its default."""
        payload: dict[str, Any] = {
            "model": model, "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
        }
        if self.is_reasoning(model):
            if effort:
                payload["reasoning_effort"] = effort
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_completion_tokens"] = min(max_tokens, 8000)
        for _ in range(3):
            try:
                return await self._post(payload, timeout)
            except ChatUnavailable as exc:
                if exc.upstream_status != 400:
                    raise
                text = exc.message.lower()
                if "reasoning" in text and "reasoning_effort" in payload:
                    payload.pop("reasoning_effort")
                elif ("response_format" in text or "json_schema" in text or "schema" in text) \
                        and payload["response_format"].get("type") == "json_schema":
                    payload["response_format"] = {"type": "json_object"}
                else:
                    raise
        return await self._post(payload, timeout)

    async def _post(self, payload: dict[str, Any], timeout: float) -> tuple[str, dict[str, Any]]:
        assert self._s.openai_api_key is not None
        try:
            response = await self._http.post(
                f"{self._s.openai_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self._s.openai_api_key.get_secret_value()}"},
                timeout=httpx.Timeout(timeout, connect=10.0),
            )
        except httpx.HTTPError as exc:
            raise ChatUnavailable(f"OpenAI unreachable: {type(exc).__name__}", 502) from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            error = body.get("error") if isinstance(body, dict) else None
            message = error.get("message") if isinstance(error, dict) else f"HTTP {response.status_code}"
            code = error.get("code") if isinstance(error, dict) else None
            status = 404 if code == "model_not_found" or response.status_code == 404 else 502
            if response.status_code == 401:
                message = "OpenAI rejected the API key"
            elif response.status_code == 429:
                message = f"OpenAI rate limit or quota: {message}"
            raise ChatUnavailable(str(message)[:300], status, response.status_code)
        try:
            choice = body["choices"][0]
            message_obj = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise ChatUnavailable("unexpected OpenAI response", 502) from None
        text = message_obj.get("content") or message_obj.get("refusal") or ""
        if isinstance(text, list):  # content parts
            text = "".join(str(p.get("text", "")) for p in text if isinstance(p, dict))
        if not str(text).strip():
            reason = choice.get("finish_reason")
            detail = (
                "the model used its whole token budget on reasoning (finish_reason=length)"
                if reason == "length" else f"the model returned no text (finish_reason={reason})"
            )
            raise ChatUnavailable(detail, 204)
        return str(text).strip(), body.get("usage") or {}
