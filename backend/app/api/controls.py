"""Phase 3/4 API: manual analysis control, live monitor, watchlist, news, chat, portfolio.

Anything that spends provider calls, OpenAI tokens or changes data requires the admin
token (header `X-Admin-Token`) when ADMIN_TOKEN is set. Reads of public market data
stay open; the portfolio is private and needs the token as well.
"""

from __future__ import annotations

import hmac
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import get_container
from app.core.timeutil import utcnow
from app.schemas.api import SignalScanOut
from app.services.chat import ChatUnavailable
from app.services.container import Container
from app.services.portfolio import PortfolioError
from app.services.selection import SelectionError
from app.services.watchlist import WatchlistError

router = APIRouter()
ContainerDep = Annotated[Container, Depends(get_container)]
SymbolPath = Annotated[str, Path(min_length=1, max_length=15, pattern=r"^[A-Za-z0-9]+$")]


def require_admin(request: Request, c: ContainerDep) -> None:
    token = c.settings.admin_token_value
    if token is None:
        return
    provided = request.headers.get("X-Admin-Token", "")
    if not hmac.compare_digest(provided.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="admin token required")


Admin = Annotated[None, Depends(require_admin)]


# ----------------------------------------------------------------------------- analysis control


def full_status(c: Container) -> dict[str, Any]:
    """The same status shape for every control endpoint, so the dashboard never loses fields."""
    return {
        **c.controller.status(),
        "scalp": c.scalp.status(),
        "lab": c.lab.status(),
        "auth_required": c.settings.admin_token_value is not None,
        "chat_available": c.chat.configured,
        "watchlist": c.watchlist.symbols(),
        "emergency_stop": c.kill.status(),
    }


class KillIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


@router.post("/api/control/kill", tags=["control"])
async def emergency_stop(c: ContainerDep, _: Admin, body: KillIn | None = None) -> dict[str, Any]:
    """Emergency stop: halt scans, schedule, lab and live prices, and refuse every provider, news
    and AI call until Resume. Survives restarts."""
    await c.emergency_stop(body.reason if body else None)
    return full_status(c)


@router.post("/api/control/resume", tags=["control"])
async def emergency_resume(c: ContainerDep, _: Admin) -> dict[str, Any]:
    """Release the emergency stop. Nothing restarts on its own: press Analyze now when ready."""
    await c.resume()
    return full_status(c)


@router.get("/api/control/status", tags=["control"])
async def control_status(c: ContainerDep) -> dict[str, Any]:
    return full_status(c)


@router.post("/api/control/analyze", tags=["control"])
async def analyze_now(c: ContainerDep, _: Admin) -> dict[str, Any]:
    """Scan the Top 20 plus the watchlist once, in the background."""
    started = c.controller.start_scan("manual")
    return {"started": started, **full_status(c)}


class AutoIn(BaseModel):
    minutes: int = Field(ge=0, le=1440, description="0 switches the schedule off; minimum 5")


@router.post("/api/control/auto", tags=["control"])
async def set_auto(body: AutoIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    c.controller.set_auto(body.minutes)
    return full_status(c)


@router.post("/api/control/stop", tags=["control"])
async def stop_all(c: ContainerDep, _: Admin) -> dict[str, Any]:
    """Stop the running scans (swing and scalp), the auto schedule and live monitoring."""
    await c.scalp.stop()
    await c.controller.stop()
    return full_status(c)


class SelectionIn(BaseModel):
    mode: str = Field(pattern=r"^(all|selected)$")
    symbols: list[str] = Field(default_factory=list, max_length=60)


@router.get("/api/control/selection", tags=["control"])
async def get_selection(c: ContainerDep) -> dict[str, Any]:
    """Which coins Analyze now scans: every coin, or the user's selection."""
    last = c.universe.last
    universe = [{"symbol": a.symbol, "name": a.name, "rank": a.universe_rank, "watchlist": a.watchlist,
                 "supported": a.supported, "selected": c.selection.includes(a.symbol)} for a in last.assets] if last else []
    return {**c.selection.status(), "universe": universe}


@router.put("/api/control/selection", tags=["control"])
async def set_selection(body: SelectionIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    try:
        await c.selection.set(body.mode, body.symbols)
    except (SelectionError, WatchlistError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return await get_selection(c)


@router.post("/api/control/live/start", tags=["control"])
async def live_start(c: ContainerDep, _: Admin) -> dict[str, Any]:
    try:
        symbols = await c.controller.start_live()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"symbols": symbols, **full_status(c)}


@router.post("/api/control/live/stop", tags=["control"])
async def live_stop(c: ContainerDep, _: Admin) -> dict[str, Any]:
    await c.controller.stop_live()
    return full_status(c)


@router.get("/api/live", tags=["control"])
async def live_prices(c: ContainerDep) -> dict[str, Any]:
    """Latest streamed prices (no provider calls)."""
    stream = asdict(c.stream.status()) if c.stream is not None else None
    return {"generated_at": utcnow(), "running": c.controller.live_running, "prices": c.controller.live_prices(), "stream": stream}


@router.get("/api/signals", response_model=SignalScanOut | None, tags=["signals"])
async def latest_signals(c: ContainerDep) -> SignalScanOut | None:
    """The latest completed scan (null until "Analyze now" has run). Never starts a scan."""
    return c.controller.last_scan


# ----------------------------------------------------------------------------- scalp signals, track record

HorizonQuery = Annotated[str, Query(pattern=r"^(15m|1h|4h|1d)$")]


@router.post("/api/scalp/scan", tags=["scalp"])
async def scalp_scan(c: ContainerDep, _: Admin, horizon: HorizonQuery = "1h") -> dict[str, Any]:
    """Find scalp setups on the selected coins now (in the background), with a backtest per coin."""
    if c.kill.active:
        raise HTTPException(status_code=503, detail="emergency stop is engaged")
    started = c.scalp.start_scan(horizon)
    return {"started": started, "status": c.scalp.status()}


@router.get("/api/scalp", tags=["scalp"])
async def scalp_results(c: ContainerDep, horizon: HorizonQuery = "1h") -> dict[str, Any]:
    """The latest scalp scan for a horizon (null until one ran) and the scan status."""
    return {"status": c.scalp.status(), "result": c.scalp.results.get(horizon)}


@router.get("/api/scalp/{symbol}", tags=["scalp"])
async def scalp_symbol(symbol: SymbolPath, c: ContainerDep, _: Admin, horizon: HorizonQuery = "1h") -> dict[str, Any]:
    """Scalp analysis of one coin now, with its backtest."""
    try:
        return await c.scalp.analyze(symbol, horizon)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


# ----------------------------------------------------------------------------- strategy lab, statistical filter


class VariantIn(BaseModel):
    filter: str = Field(max_length=16)
    exit: str = Field(max_length=16)


class ToggleIn(BaseModel):
    enabled: bool


def _lab_view(c: Container, horizon: str) -> dict[str, Any]:
    from app.analysis import lab as lab_rules

    return {
        "horizon": horizon,
        "status": c.lab.status()[horizon],
        "result": c.lab.results.get(horizon),
        "applied": c.scalp.variant_info.get(horizon, {"filter": "base", "exit": "x1", "label": "published rules",
                                                      "source": "default"}),
        "model": c.scalp.models[horizon].as_dict() if horizon in c.scalp.models else None,
        "ml_enabled": c.scalp.ml_enabled.get(horizon, True),
        "filters": [{"key": v.key, "label": v.label} for v in lab_rules.FILTERS],
        "exits": [{"key": v.key, "label": v.label} for v in lab_rules.EXITS],
    }


@router.get("/api/lab", tags=["lab"])
async def lab_view(c: ContainerDep, horizon: HorizonQuery = "1h") -> dict[str, Any]:
    """Strategy lab: the latest walk-forward result, the variant in use and the statistical model."""
    return _lab_view(c, horizon)


@router.post("/api/lab/run", tags=["lab"])
async def lab_run(c: ContainerDep, _: Admin, horizon: HorizonQuery = "1h") -> dict[str, Any]:
    """Test the rule variants on the selected coins' history (older 70% chooses, newer 30% checks)
    and train the statistical filter. Runs in the background."""
    if c.kill.active:
        raise HTTPException(status_code=503, detail="emergency stop is engaged")
    return {"started": c.lab.start(horizon), **_lab_view(c, horizon)}


@router.post("/api/lab/apply", tags=["lab"])
async def lab_apply(body: VariantIn, c: ContainerDep, _: Admin, horizon: HorizonQuery = "1h") -> dict[str, Any]:
    """Use a variant by hand (even one the lab did not confirm: your decision)."""
    try:
        await c.scalp.apply_variant(horizon, body.filter, body.exit, source="manual")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _lab_view(c, horizon)


@router.post("/api/lab/reset", tags=["lab"])
async def lab_reset(c: ContainerDep, _: Admin, horizon: HorizonQuery = "1h") -> dict[str, Any]:
    """Back to the published rules."""
    await c.scalp.reset_variant(horizon)
    return _lab_view(c, horizon)


@router.get("/api/ml", tags=["lab"])
async def ml_view(c: ContainerDep, horizon: HorizonQuery = "1h") -> dict[str, Any]:
    model = c.scalp.models.get(horizon)
    return {"horizon": horizon, "enabled": c.scalp.ml_enabled.get(horizon, True),
            "model": model.as_dict() if model else None}


@router.put("/api/ml", tags=["lab"])
async def ml_toggle(body: ToggleIn, c: ContainerDep, _: Admin, horizon: HorizonQuery = "1h") -> dict[str, Any]:
    """Switch the statistical filter on or off for a horizon (it only ever acts when validated)."""
    await c.scalp.set_ml_enabled(horizon, body.enabled)
    return await ml_view(c, horizon)


# ----------------------------------------------------------------------------- evidence board (phase 10)


class EvidenceSettingsIn(BaseModel):
    mode: str | None = Field(default=None, pattern=r"^(filter|advisory|off)$")
    refresh_news: bool | None = None
    ai_news: bool | None = None


@router.get("/api/evidence/settings", tags=["evidence"])
async def evidence_settings(c: ContainerDep) -> dict[str, Any]:
    return c.evidence.settings()


@router.put("/api/evidence/settings", tags=["evidence"])
async def evidence_settings_update(body: EvidenceSettingsIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    """filter: the board can hold buys back; advisory: shown only; off: not computed."""
    return await c.evidence.update(mode=body.mode, refresh_news=body.refresh_news, ai_news=body.ai_news)


@router.get("/api/evidence/learning", tags=["evidence"])
async def learning_view(c: ContainerDep) -> dict[str, Any]:
    """What the closed setups say about each factor, and the learned model's validation."""
    return c.learning.status()


@router.post("/api/evidence/learning/refresh", tags=["evidence"])
async def learning_refresh(c: ContainerDep, _: Admin) -> dict[str, Any]:
    return await c.learning.refresh(force=True)


@router.put("/api/evidence/learning", tags=["evidence"])
async def learning_toggle(body: ToggleIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    """Switch the learned model off or on (it only ever acts when validated)."""
    await c.learning.set_enabled(body.enabled)
    return c.learning.status()


@router.get("/api/evidence/{symbol}", tags=["evidence"])
async def evidence_view(symbol: SymbolPath, c: ContainerDep,
                        horizon: Annotated[str, Query(pattern=r"^(swing|15m|1h|4h|1d)$")] = "swing") -> dict[str, Any]:
    """The latest board computed for a coin (by a scan or by opening the coin); never fetches."""
    board = c.evidence.last.get((symbol.upper(), horizon))
    if board is None:
        raise HTTPException(status_code=404, detail="no evidence board yet: run a scan or open the coin")
    return board.as_dict()


@router.get("/api/derivatives/market", tags=["evidence"])
async def derivatives_market(c: ContainerDep) -> dict[str, Any]:
    """Futures positioning across the market (funding, crowded coins) from the reachable exchanges."""
    if not c.derivatives.enabled:
        raise HTTPException(status_code=503, detail="futures data is switched off (DERIVATIVES_ENABLED=false)")
    market = await c.derivatives.market()
    return {"fetched_at": market.fetched_at, **market.summary(c.evidence.universe_symbols() or None)}


@router.get("/api/derivatives/{symbol}", tags=["evidence"])
async def derivatives_coin(symbol: SymbolPath, c: ContainerDep) -> dict[str, Any]:
    """Funding, open interest, long/short ratios, taker flow and liquidations for one coin (cached)."""
    if not c.derivatives.enabled:
        raise HTTPException(status_code=503, detail="futures data is switched off (DERIVATIVES_ENABLED=false)")
    return (await c.derivatives.snapshot(symbol)).as_dict()


# ----------------------------------------------------------------------------- AI review (phase 7)


class ReviewIn(BaseModel):
    kind: str = Field(pattern=r"^(swing|scalp)$")
    symbol: str = Field(min_length=1, max_length=15, pattern=r"^[A-Za-z0-9]+$")
    horizon: str | None = Field(default=None, pattern=r"^(15m|1h|4h|1d)$")
    model: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class ReviewSettingsIn(BaseModel):
    mode: str | None = Field(default=None, pattern=r"^(advisory|filter)$")
    auto: bool | None = None
    auto_max: int | None = Field(default=None, ge=1, le=10)


@router.post("/api/ai/review", tags=["ai"])
async def ai_review(body: ReviewIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    """A second opinion from the OpenAI model on one signal: agree, caution or reject (it can only lower)."""
    symbol = body.symbol.upper()
    if body.kind == "scalp":
        horizon = body.horizon or "1h"
        result = c.scalp.results.get(horizon)
        signal = next((s for s in (result or {}).get("signals", []) if s["symbol"] == symbol), None)
        if signal is None:
            raise HTTPException(status_code=404, detail=f"no {horizon} scalp result for {symbol}: run Find scalps first")
    else:
        horizon = ""
        cached = c.analysis.cached(symbol) or await c.analysis.analyze(symbol)
        signal = cached.model_dump(mode="json")
    try:
        return await c.ai_review.review(body.kind, symbol, signal, horizon=horizon, model=body.model)
    except ChatUnavailable as exc:
        raise HTTPException(status_code=exc.status_code if exc.status_code != 204 else 502, detail=exc.message) from None


@router.get("/api/ai/settings", tags=["ai"])
async def ai_settings(c: ContainerDep) -> dict[str, Any]:
    return c.ai_review.settings()


@router.put("/api/ai/settings", tags=["ai"])
async def ai_settings_update(body: ReviewSettingsIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    """advisory: verdicts are shown only; filter: a reject caps a buy at WATCH. auto: review new buys after scans."""
    try:
        return await c.ai_review.update(mode=body.mode, auto=body.auto, auto_max=body.auto_max)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/api/performance", tags=["signals"])
async def performance(c: ContainerDep, days: Annotated[int, Query(ge=1, le=365)] = 90) -> dict[str, Any]:
    """Track record: what happened after each buy signal (swing and scalp), per strategy."""
    return await c.tracker.performance(days)


# ----------------------------------------------------------------------------- watchlist


class WatchIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)
    note: str | None = Field(default=None, max_length=255)


@router.get("/api/watchlist", tags=["watchlist"])
async def get_watchlist(c: ContainerDep) -> dict[str, Any]:
    return {"items": [{"symbol": s, "note": n} for s, n in c.watchlist.items()]}


@router.post("/api/watchlist", tags=["watchlist"])
async def add_watch(body: WatchIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    try:
        symbol = await c.watchlist.add(body.symbol, body.note)
    except WatchlistError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    await c.selection.include(symbol)
    return {"added": symbol, "items": [{"symbol": s, "note": n} for s, n in c.watchlist.items()]}


@router.delete("/api/watchlist/{symbol}", tags=["watchlist"])
async def remove_watch(symbol: SymbolPath, c: ContainerDep, _: Admin) -> dict[str, Any]:
    removed = await c.watchlist.remove(symbol)
    if removed:
        await c.selection.discard(symbol)
    return {"removed": removed, "items": [{"symbol": s, "note": n} for s, n in c.watchlist.items()]}


# ----------------------------------------------------------------------------- news


@router.get("/api/news", tags=["news"])
async def news(c: ContainerDep, refresh: bool = False) -> dict[str, Any]:
    """Latest crypto headlines and trending coins from free public sources (cached)."""
    digest = await c.news.digest(force=refresh)
    return asdict(digest)


# ----------------------------------------------------------------------------- sentiment, on-chain (Phase 5)


@router.get("/api/sentiment", tags=["sentiment"])
async def sentiment(c: ContainerDep, refresh: bool = False) -> dict[str, Any]:
    """Market mood (Fear & Greed trend, funding, news tone) and per-coin sentiment (cached)."""
    return (await c.sentiment.digest(force=refresh)).as_dict()


@router.get("/api/onchain", tags=["sentiment"])
async def onchain(c: ContainerDep, refresh: bool = False) -> dict[str, Any]:
    """Bitcoin and Ethereum network state, stablecoin supply and large transfers (cached)."""
    return (await c.onchain.digest(force=refresh)).as_dict()


# ----------------------------------------------------------------------------- token unlocks, airdrops


@router.get("/api/events/unlocks", tags=["events"])
async def token_unlocks(c: ContainerDep, refresh: bool = False) -> dict[str, Any]:
    """Upcoming token unlocks for the analysed coins (Mobula; needs MOBULA_API_KEY)."""
    return (await c.events.unlocks(force=refresh)).as_dict()


@router.get("/api/events/airdrops", tags=["events"])
async def airdrops(c: ContainerDep, refresh: bool = False) -> dict[str, Any]:
    """Active, claimable and upcoming airdrops (AlphaDrops; needs ALPHADROPS_API_KEY)."""
    return (await c.events.airdrops(force=refresh)).as_dict()


# ----------------------------------------------------------------------------- chat


class ChatMessage(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str = Field(min_length=1, max_length=8000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    symbol: str | None = Field(default=None, max_length=15, pattern=r"^[A-Za-z0-9]+$")
    model: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    reasoning_effort: str | None = Field(default=None, pattern=r"^(minimal|low|medium|high)$")


@router.get("/api/chat/models", tags=["chat"])
async def chat_models(c: ContainerDep, _: Admin) -> dict[str, Any]:
    """Model choices for the assistant, marked with what the server's OpenAI key can use."""
    return await c.chat.available_models()


@router.post("/api/chat", tags=["chat"])
async def chat(body: ChatIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    try:
        return await c.chat.reply(
            [m.model_dump() for m in body.messages], body.symbol.upper() if body.symbol else None,
            model=body.model, reasoning_effort=body.reasoning_effort,
        )
    except ChatUnavailable as exc:
        status = 502 if exc.status_code == 204 else exc.status_code
        raise HTTPException(status_code=status, detail=exc.message) from None


# ----------------------------------------------------------------------------- portfolio


class CashIn(BaseModel):
    cash: float = Field(ge=0)


class PositionIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)
    quantity: float = Field(gt=0)
    average_entry: float = Field(gt=0)
    notes: str | None = Field(default=None, max_length=500)


class RiskIn(BaseModel):
    max_risk_per_signal_pct: float | None = None
    initial_allocation_pct: float | None = None
    max_allocation_per_opportunity_pct: float | None = None
    max_dca_allocation_pct: float | None = None
    max_total_open_allocation_pct: float | None = None
    fee_pct: float | None = None
    slippage_pct: float | None = None


class PlanIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=15)
    budget: float | None = Field(default=None, gt=0)


def _portfolio_error(exc: PortfolioError | WatchlistError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/api/portfolio", tags=["portfolio"])
async def portfolio(c: ContainerDep, _: Admin) -> dict[str, Any]:
    return await c.portfolio.overview()


@router.put("/api/portfolio/cash", tags=["portfolio"])
async def portfolio_cash(body: CashIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    try:
        await c.portfolio.set_cash(body.cash)
    except PortfolioError as exc:
        raise _portfolio_error(exc) from None
    return await c.portfolio.overview()


@router.post("/api/portfolio/positions", tags=["portfolio"])
async def portfolio_position(body: PositionIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    try:
        await c.portfolio.upsert_position(body.symbol, body.quantity, body.average_entry, body.notes)
    except (PortfolioError, WatchlistError) as exc:
        raise _portfolio_error(exc) from None
    return await c.portfolio.overview()


@router.delete("/api/portfolio/positions/{symbol}", tags=["portfolio"])
async def portfolio_delete(symbol: SymbolPath, c: ContainerDep, _: Admin) -> dict[str, Any]:
    await c.portfolio.delete_position(symbol)
    return await c.portfolio.overview()


@router.put("/api/portfolio/risk", tags=["portfolio"])
async def portfolio_risk(body: RiskIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    try:
        await c.portfolio.update_risk(body.model_dump(exclude_none=True))
    except PortfolioError as exc:
        raise _portfolio_error(exc) from None
    return await c.portfolio.overview()


@router.post("/api/portfolio/plan", tags=["portfolio"])
async def portfolio_plan(body: PlanIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    """Position size, DCA ladder and P/L scenarios from the coin's current trade plan."""
    try:
        return await c.portfolio.trade_plan(body.symbol, body.budget)
    except (PortfolioError, WatchlistError) as exc:
        raise _portfolio_error(exc) from None
