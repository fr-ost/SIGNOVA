"""Manual control of the analysis (Phase 3).

Nothing analyses in the background by default: a scan runs when the user presses
"Analyze now", or on an optional schedule the user switches on. "Stop" cancels a
running scan, switches the schedule off and stops live monitoring. The latest
completed scan stays available for reading without spending any provider calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.core.enums import ProcessingState
from app.core.timeutil import utcnow
from app.data.providers.binance_ws import BinanceStreamManager
from app.schemas.api import SignalScanOut
from app.services.analysis import AnalysisService, ScanStopped
from app.services.system_state import SystemStateStore
from app.services.universe import UniverseService

log = logging.getLogger(__name__)

MIN_AUTO_MINUTES = 5
STOP_GRACE_SECONDS = 20.0  # a stopped scan may finish its coins in progress for this long


@dataclass
class ScanProgress:
    running: bool = False
    total: int = 0
    done: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: str = "never_run"  # never_run | running | completed | stopped | failed
    error: str | None = None
    trigger: str | None = None  # manual | auto


class AnalysisController:
    def __init__(
        self,
        analysis: AnalysisService,
        universe: UniverseService,
        state: SystemStateStore,
        stream: BinanceStreamManager | None,
        live_prices: Any,
        *,
        auto_minutes: int = 0,
        selection: Any = None,
    ) -> None:
        self._selection = selection
        self._analysis = analysis
        self._universe = universe
        self._state = state
        self._stream = stream
        self._live = live_prices
        self.progress = ScanProgress()
        self.last_scan: SignalScanOut | None = None
        self.auto_minutes = 0
        self._task: asyncio.Task[None] | None = None
        self._auto_task: asyncio.Task[None] | None = None
        self._next_auto_at: datetime | None = None
        self._live_symbols: dict[str, str] = {}  # exchange symbol -> base asset
        self._stopping = False
        self.after_scan: Any = None  # called with the finished scan (automatic AI review)
        self._initial_auto = auto_minutes

    # ------------------------------------------------------------------ scans

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start_scan(self, trigger: str = "manual") -> bool:
        """Start a scan in the background. Returns False if one is already running or the
        emergency stop is engaged."""
        if self.running or self._state.emergency_stop:
            return False
        self.progress = ScanProgress(
            running=True, started_at=utcnow(), outcome="running", trigger=trigger
        )
        self._state.processing_state = ProcessingState.ANALYZING
        self._stopping = False
        self._task = asyncio.create_task(self._scan(), name="analysis-scan")
        return True

    async def _scan(self) -> None:
        progress = self.progress

        def on_start(total: int) -> None:
            progress.total = total

        def on_progress(symbol: str) -> None:
            progress.done += 1

        try:
            self.last_scan = await self._analysis.run_scan(
                on_start=on_start, on_progress=on_progress, should_stop=lambda: self._stopping
            )
            progress.outcome = "completed"
            if self.after_scan is not None:
                try:
                    self.after_scan(self.last_scan)
                except Exception:
                    log.exception("after-scan hook failed")
        except ScanStopped:
            progress.outcome = "stopped"
        except asyncio.CancelledError:
            progress.outcome = "stopped"
            raise
        except Exception as exc:
            log.exception("analysis scan failed")
            progress.outcome = "failed"
            progress.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            progress.running = False
            progress.finished_at = utcnow()
            self._state.processing_state = (
                ProcessingState.LIVE_MONITORING if self.live_running else ProcessingState.IDLE
            )

    async def wait(self) -> None:
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def stop(self) -> None:
        """Stop everything: the running scan, the auto schedule and live monitoring.

        The scan stops gracefully (no new coins; the ones in progress finish, so no database
        write is cut in half); it is cancelled only if it has not ended after STOP_GRACE_SECONDS."""
        self.set_auto(0)
        if self.running and self._task is not None:
            self._stopping = True
            done, _ = await asyncio.wait({self._task}, timeout=STOP_GRACE_SECONDS)
            if not done:
                self._task.cancel()
            await self.wait()
        await self.stop_live()
        self._state.processing_state = (
            ProcessingState.EMERGENCY_STOP if self._state.emergency_stop else ProcessingState.IDLE
        )

    # ------------------------------------------------------------------ schedule

    def set_auto(self, minutes: int) -> int:
        minutes = 0 if minutes <= 0 else max(MIN_AUTO_MINUTES, int(minutes))
        self.auto_minutes = minutes
        if self._auto_task is not None:
            self._auto_task.cancel()
            self._auto_task = None
        self._next_auto_at = None
        if minutes:
            self._auto_task = asyncio.create_task(self._auto_loop(minutes), name="analysis-auto")
        return minutes

    async def _auto_loop(self, minutes: int) -> None:
        while True:
            self._next_auto_at = utcnow() + timedelta(minutes=minutes)
            await asyncio.sleep(minutes * 60)
            self.start_scan(trigger="auto")

    def start_background(self) -> None:
        if self._initial_auto and not self._state.emergency_stop:
            self.set_auto(self._initial_auto)

    # ------------------------------------------------------------------ live monitoring

    @property
    def live_running(self) -> bool:
        return self._stream is not None and self._stream.running

    async def start_live(self) -> list[str]:
        """Stream Binance mini-tickers for the universe (no REST calls, no credits)."""
        if self._state.emergency_stop:
            raise RuntimeError("emergency stop is engaged")
        if self._stream is None:
            raise RuntimeError("live stream unavailable")
        universe = await self._universe.get()
        symbols: dict[str, str] = {}
        assets = self._selection.filter(universe.assets) if self._selection is not None else universe.assets
        for asset in assets:
            ref = next((m for m in asset.markets if m.adapter == "binance"), None)
            if ref is not None:
                symbols[ref.symbol] = asset.symbol
        if not symbols:
            raise RuntimeError("no Binance markets in the universe to stream")
        self._live_symbols = symbols
        await self._stream.start(f"{s.lower()}@miniTicker" for s in symbols)
        self._state.processing_state = ProcessingState.LIVE_MONITORING
        return sorted(symbols.values())

    async def stop_live(self) -> None:
        if self._stream is not None and self._stream.running:
            await self._stream.stop()
        if not self.running:
            self._state.processing_state = ProcessingState.IDLE

    def live_prices(self) -> dict[str, dict[str, Any]]:
        now = utcnow()
        out: dict[str, dict[str, Any]] = {}
        for exchange_symbol, base in self._live_symbols.items():
            quote = self._live.prices.get(exchange_symbol)
            if not quote:
                continue
            try:
                price = float(quote["close"])
            except (TypeError, ValueError):
                continue
            out[base] = {
                "price": price,
                "market_symbol": exchange_symbol,
                "age_seconds": round((now - quote["received_at"]).total_seconds(), 1),
            }
        return out

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        p = self.progress
        return {
            "processing_state": self._state.processing_state.value,
            "scan": {
                "running": p.running,
                "outcome": p.outcome,
                "trigger": p.trigger,
                "total": p.total,
                "done": p.done,
                "started_at": p.started_at,
                "finished_at": p.finished_at,
                "error": p.error,
            },
            "last_scan_at": self.last_scan.generated_at if self.last_scan else None,
            "auto_minutes": self.auto_minutes,
            "next_auto_at": self._next_auto_at,
            "selection": self._selection.status() if self._selection is not None else {"mode": "all", "symbols": []},
            "live": {
                "running": self.live_running,
                "symbols": len(self._live_symbols) if self.live_running else 0,
            },
        }

    async def aclose(self) -> None:
        if self._auto_task is not None:
            self._auto_task.cancel()
        if self.running and self._task is not None:
            self._task.cancel()
            await self.wait()
