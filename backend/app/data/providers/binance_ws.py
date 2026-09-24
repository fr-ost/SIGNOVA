"""Binance public market-data WebSocket manager.

* Combined-stream endpoint with SUBSCRIBE/UNSUBSCRIBE control messages
* Heartbeat: the server pings every ~20s and the client library answers with pong
  automatically; the client also sends its own pings to detect dead connections
* Watchdog: reconnects if no message arrives within `stale_after_seconds`
* Automatic reconnect with exponential backoff + jitter, rotating base URLs
* Resubscribes every stream after each reconnect
* Proactive renewal before Binance's 24-hour connection limit
* Control messages are rate limited below Binance's 5 messages/second limit

Stream names are passed through unchanged: symbols must be lowercase while the event
type keeps Binance's casing, e.g. ``btcusdt@miniTicker`` or ``ethusdt@kline_5m``.

The manager is started on demand (analysis / live monitor) and is idle otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.core.timeutil import utcnow

log = logging.getLogger(__name__)

MessageHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]

_MAX_PARAMS_PER_MESSAGE = 200
_CONTROL_MESSAGE_SPACING = 0.25  # 4 msg/s, below Binance's 5 msg/s limit
_RENEW_BEFORE_SECONDS = 24 * 3600 - 10 * 60


@dataclass
class StreamStatus:
    running: bool = False
    connected: bool = False
    base_url: str | None = None
    streams: list[str] = field(default_factory=list)
    connected_since: str | None = None
    last_message_at: str | None = None
    messages_received: int = 0
    reconnects: int = 0
    renewals: int = 0
    last_error: str | None = None


class BinanceStreamManager:
    def __init__(
        self,
        base_urls: list[str],
        on_message: MessageHandler,
        *,
        stale_after_seconds: float = 30.0,
        max_connection_seconds: float = _RENEW_BEFORE_SECONDS,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
        stable_after_seconds: float = 60.0,
        connect_kwargs: dict[str, Any] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if not base_urls:
            raise ValueError("at least one WebSocket base URL is required")
        self._bases = [url.rstrip("/") for url in base_urls]
        self._base_index = 0
        self._on_message = on_message
        self._stale_after = stale_after_seconds
        self._max_connection = max_connection_seconds
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._stable_after = stable_after_seconds
        self._connect_kwargs = {"ping_interval": 20, "ping_timeout": 20, "close_timeout": 5, **(connect_kwargs or {})}
        self._rng = rng or random.Random()
        self._streams: set[str] = set()
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._request_id = 0
        self._status = StreamStatus()

    # -- public API -----------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> StreamStatus:
        self._status.streams = sorted(self._streams)
        self._status.running = self.running
        return StreamStatus(**vars(self._status))

    async def start(self, streams: Iterable[str]) -> None:
        self._streams = set(streams)
        if self.running:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="binance-ws")

    async def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=10)
        self._task = None
        self._status.connected = False

    async def update_streams(self, streams: Iterable[str]) -> None:
        wanted = set(streams)
        added, removed = sorted(wanted - self._streams), sorted(self._streams - wanted)
        self._streams = wanted
        ws = self._ws
        if ws is None:
            return
        with contextlib.suppress(ConnectionClosed):
            if removed:
                await self._send_control(ws, "UNSUBSCRIBE", removed)
            if added:
                await self._send_control(ws, "SUBSCRIBE", added)

    # -- internals -----------------------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        cap = min(self._backoff_max, self._backoff_base * (2**attempt))
        return cap / 2 + self._rng.random() * cap / 2

    async def _send_control(self, ws: Any, method: str, params: list[str]) -> None:
        for start in range(0, len(params), _MAX_PARAMS_PER_MESSAGE):
            self._request_id += 1
            chunk = params[start : start + _MAX_PARAMS_PER_MESSAGE]
            await ws.send(json.dumps({"method": method, "params": chunk, "id": self._request_id}))
            await asyncio.sleep(_CONTROL_MESSAGE_SPACING)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        stream = message.get("stream")
        data = message.get("data")
        if isinstance(stream, str) and isinstance(data, dict):
            result = self._on_message(stream, data)
            if asyncio.iscoroutine(result):
                await result
        elif "error" in message:
            log.warning("binance ws control error", extra={"error": str(message.get("error"))[:300]})

    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            base = self._bases[self._base_index]
            url = f"{base}/stream"
            connected_at: float | None = None
            reason = "connection closed"
            try:
                async with ws_connect(url, **self._connect_kwargs) as ws:
                    self._ws = ws
                    connected_at = time.monotonic()
                    self._status.connected = True
                    self._status.base_url = base
                    self._status.connected_since = utcnow().isoformat()
                    if self._streams:
                        await self._send_control(ws, "SUBSCRIBE", sorted(self._streams))
                    log.info("binance ws connected", extra={"base_url": base, "streams": len(self._streams)})
                    reason = await self._receive_loop(ws, connected_at)
            except InvalidStatus as exc:
                status = exc.response.status_code
                reason = f"handshake rejected with HTTP {status}"
                if status == 451:
                    reason = "HTTP 451 restricted location"
                self._rotate_base()
            except (ConnectionClosed, OSError, TimeoutError) as exc:
                reason = f"{type(exc).__name__}: {exc}"[:300]
            finally:
                self._ws = None
                self._status.connected = False

            if self._stop.is_set():
                break
            if reason == "renewal":
                self._status.renewals += 1
                attempt = 0
                continue
            self._status.last_error = reason
            self._status.reconnects += 1
            if connected_at is not None and time.monotonic() - connected_at >= self._stable_after:
                attempt = 0
            elif attempt >= 2:
                self._rotate_base()
            delay = self._backoff(attempt)
            attempt += 1
            log.warning("binance ws reconnecting", extra={"reason": reason, "delay_s": round(delay, 2)})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

    def _rotate_base(self) -> None:
        self._base_index = (self._base_index + 1) % len(self._bases)

    async def _receive_loop(self, ws: Any, connected_at: float) -> str:
        renew_at = connected_at + self._max_connection
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= renew_at:
                return "renewal"
            timeout = min(self._stale_after, renew_at - now)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except TimeoutError:
                if time.monotonic() >= renew_at:
                    return "renewal"
                return f"no data for {self._stale_after:.0f}s (stale stream)"
            self._status.messages_received += 1
            self._status.last_message_at = utcnow().isoformat()
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(message, dict):
                try:
                    await self._dispatch(message)
                except Exception:  # handler bugs must not kill the connection
                    log.exception("binance ws handler failed")
        return "stopped"
