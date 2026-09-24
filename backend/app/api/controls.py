"""Phase 3/4 API: manual analysis control, live monitor, watchlist, news, chat, portfolio.

Anything that spends provider calls, OpenAI tokens or changes data requires the admin
token (header `X-Admin-Token`) when ADMIN_TOKEN is set. Reads of public market data
stay open; the portfolio is private and needs the token as well.
"""

from __future__ import annotations

import hmac
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field

from app.api.deps import get_container
from app.core.timeutil import utcnow
from app.schemas.api import SignalScanOut
from app.services.chat import ChatUnavailable
from app.services.container import Container
from app.services.portfolio import PortfolioError
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


@router.get("/api/control/status", tags=["control"])
async def control_status(c: ContainerDep) -> dict[str, Any]:
    return {
        **c.controller.status(),
        "auth_required": c.settings.admin_token_value is not None,
        "chat_available": c.chat.configured,
        "watchlist": c.watchlist.symbols(),
    }


@router.post("/api/control/analyze", tags=["control"])
async def analyze_now(c: ContainerDep, _: Admin) -> dict[str, Any]:
    """Scan the Top 20 plus the watchlist once, in the background."""
    started = c.controller.start_scan("manual")
    return {"started": started, **c.controller.status()}


class AutoIn(BaseModel):
    minutes: int = Field(ge=0, le=1440, description="0 switches the schedule off; minimum 5")


@router.post("/api/control/auto", tags=["control"])
async def set_auto(body: AutoIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    c.controller.set_auto(body.minutes)
    return c.controller.status()


@router.post("/api/control/stop", tags=["control"])
async def stop_all(c: ContainerDep, _: Admin) -> dict[str, Any]:
    """Stop the running scan, the auto schedule and live monitoring."""
    await c.controller.stop()
    return c.controller.status()


@router.post("/api/control/live/start", tags=["control"])
async def live_start(c: ContainerDep, _: Admin) -> dict[str, Any]:
    try:
        symbols = await c.controller.start_live()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"symbols": symbols, **c.controller.status()}


@router.post("/api/control/live/stop", tags=["control"])
async def live_stop(c: ContainerDep, _: Admin) -> dict[str, Any]:
    await c.controller.stop_live()
    return c.controller.status()


@router.get("/api/live", tags=["control"])
async def live_prices(c: ContainerDep) -> dict[str, Any]:
    """Latest streamed prices (no provider calls)."""
    stream = asdict(c.stream.status()) if c.stream is not None else None
    return {"generated_at": utcnow(), "running": c.controller.live_running, "prices": c.controller.live_prices(), "stream": stream}


@router.get("/api/signals", response_model=SignalScanOut | None, tags=["signals"])
async def latest_signals(c: ContainerDep) -> SignalScanOut | None:
    """The latest completed scan (null until "Analyze now" has run). Never starts a scan."""
    return c.controller.last_scan


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
    return {"added": symbol, "items": [{"symbol": s, "note": n} for s, n in c.watchlist.items()]}


@router.delete("/api/watchlist/{symbol}", tags=["watchlist"])
async def remove_watch(symbol: SymbolPath, c: ContainerDep, _: Admin) -> dict[str, Any]:
    removed = await c.watchlist.remove(symbol)
    return {"removed": removed, "items": [{"symbol": s, "note": n} for s, n in c.watchlist.items()]}


# ----------------------------------------------------------------------------- news


@router.get("/api/news", tags=["news"])
async def news(c: ContainerDep, refresh: bool = False) -> dict[str, Any]:
    """Latest crypto headlines and trending coins from free public sources (cached)."""
    digest = await c.news.digest(force=refresh)
    return asdict(digest)


# ----------------------------------------------------------------------------- chat


class ChatMessage(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str = Field(min_length=1, max_length=8000)


class ChatIn(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    symbol: str | None = Field(default=None, max_length=15, pattern=r"^[A-Za-z0-9]+$")


@router.post("/api/chat", tags=["chat"])
async def chat(body: ChatIn, c: ContainerDep, _: Admin) -> dict[str, Any]:
    try:
        return await c.chat.reply([m.model_dump() for m in body.messages], body.symbol.upper() if body.symbol else None)
    except ChatUnavailable as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


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
