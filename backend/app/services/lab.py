"""Strategy lab service (Phases 8 and 9): runs the walk-forward variant test and trains the
statistical filter on real history, on request, in the background.

The heavy computation runs in a worker thread so the dashboard stays responsive. A variant is
applied to the scalp engine only when it passed its out-of-sample test, and a model is used only
when validated; both can be reset from the dashboard. Results survive restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.analysis import lab as lab_rules
from app.analysis import scalp as sc
from app.core.timeutil import utcnow
from app.services.scalp import ScalpService
from app.services.settings_store import SettingsStore
from app.services.universe import UniverseAsset, UniverseService

log = logging.getLogger(__name__)


@dataclass
class LabState:
    horizon: str
    running: bool = False
    outcome: str = "never_run"  # never_run | running | completed | stopped | failed
    phase: str = ""
    total: int = 0
    done: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class LabService:
    def __init__(
        self,
        universe: UniverseService,
        scalp: ScalpService,
        store: SettingsStore,
        *,
        selection_filter: Callable[[list[UniverseAsset]], list[UniverseAsset]] | None = None,
        blocked: Callable[[], bool] | None = None,
        concurrency: int = 4,
    ) -> None:
        self._universe = universe
        self._scalp = scalp
        self._store = store
        self._filter = selection_filter
        self._blocked = blocked or (lambda: False)
        self._concurrency = concurrency
        self.states: dict[str, LabState] = {h: LabState(h) for h in sc.PROFILES}
        self.results: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stopping = False

    async def load(self) -> None:
        for h in sc.PROFILES:
            data = await self._store.get(f"lab_{h}")
            if isinstance(data, dict):
                self.results[h] = data

    def status(self) -> dict[str, Any]:
        return {h: asdict(s) for h, s in self.states.items()}

    def running(self) -> bool:
        return any(not t.done() for t in self._tasks.values())

    def start(self, horizon: str) -> bool:
        prof = ScalpService.profile(horizon)
        task = self._tasks.get(prof.key)
        if (task is not None and not task.done()) or self._blocked():
            return False
        self._stopping = False
        self.states[prof.key] = LabState(prof.key, running=True, outcome="running", phase="loading history",
                                         started_at=utcnow())
        self._tasks[prof.key] = asyncio.create_task(self._run(prof), name=f"lab-{prof.key}")
        return True

    async def wait(self, horizon: str | None = None) -> None:
        for key, task in list(self._tasks.items()):
            if horizon is None or key == horizon:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def stop(self, grace_seconds: float = 20.0) -> None:
        running = [t for t in self._tasks.values() if not t.done()]
        if not running:
            return
        self._stopping = True
        _, pending = await asyncio.wait(running, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        await self.wait()

    async def _run(self, prof: sc.ScalpProfile) -> None:
        state = self.states[prof.key]
        try:
            universe = await self._universe.get()
            assets = [a for a in universe.assets if a.supported]
            if self._filter is not None:
                assets = self._filter(assets)
            state.total = len(assets)
            btc = await self._scalp.btc_trend(prof, universe.assets)
            semaphore = asyncio.Semaphore(max(1, self._concurrency))
            coins: list[lab_rules.CoinSeries] = []
            errors: list[str] = []

            async def load(asset: UniverseAsset) -> None:
                async with semaphore:
                    if self._stopping:
                        return
                    try:
                        series, _, problem = await self._scalp.load_series(asset, prof, btc)
                        if series is not None and problem is None:
                            coins.append(lab_rules.CoinSeries(asset.symbol, series, asset.symbol == "BTC"))
                        elif problem:
                            errors.append(f"{asset.symbol}: {problem}")
                    except Exception as exc:  # one coin never breaks the lab
                        errors.append(f"{asset.symbol}: {type(exc).__name__}: {exc}"[:200])
                    finally:
                        state.done += 1
                        state.phase = f"loading history {state.done}/{state.total}"

            await asyncio.gather(*(load(a) for a in assets))
            if self._stopping:
                state.outcome = "stopped"
                return
            if not coins:
                raise RuntimeError("no coin had enough clean history for the lab")
            coins.sort(key=lambda c: c.symbol)

            def progress(message: str) -> None:
                state.phase = message

            result = await asyncio.to_thread(
                lab_rules.run_lab, coins, self._scalp.base_params(), prof.key, utcnow(), progress,
                lambda: self._stopping,
            )
            result.errors = errors
            data = result.as_dict()
            data["applied"] = None
            if result.accepted and result.recommendation != lab_rules.BASELINE:
                data["applied"] = await self._scalp.apply_variant(prof.key, *result.recommendation, source="lab (validated)")
            elif not result.accepted:
                await self._scalp.reset_variant(prof.key)  # an unconfirmed variant must not stay in use
                data["applied"] = {"filter": "base", "exit": "x1", "label": "published rules", "source": "lab: nothing beat them"}
            await self._scalp.set_model(prof.key, result.model)
            self.results[prof.key] = data
            await self._store.set(f"lab_{prof.key}", data)
            state.outcome = "completed"
            state.phase = "done"
        except InterruptedError:
            state.outcome = "stopped"
        except asyncio.CancelledError:
            state.outcome = "stopped"
            raise
        except Exception as exc:
            log.exception("strategy lab failed")
            state.outcome = "failed"
            state.error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            state.running = False
            state.finished_at = utcnow()
