"""Phase 1 API: health, system state, market overview, assets, candles, provider health."""

from __future__ import annotations

import dataclasses
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.api.deps import get_container
from app.core.enums import Timeframe
from app.core.timeutil import utcnow
from app.database import check_database
from app.schemas.api import (
    AssetDetailOut,
    AssetListItemOut,
    AssetListOut,
    CandlesOut,
    ComponentHealth,
    HealthOut,
    MarketSnapshotOut,
    OpenAIConfigStatus,
    ProviderHealthListOut,
    ProviderHealthOut,
    SystemStateOut,
)
from app.services.container import Container

router = APIRouter()

SymbolPath = Annotated[str, Path(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9]+$")]
ContainerDep = Annotated[Container, Depends(get_container)]


@router.get("/health", response_model=HealthOut, tags=["system"])
async def health(c: ContainerDep) -> HealthOut:
    """Liveness + database connectivity. Cheap: never calls external providers."""
    db = await check_database(c.engine) if c.engine is not None else {"ok": False, "error": "database disabled"}
    s = c.settings
    snapshot = c.state.snapshot()
    return HealthOut(
        status="ok" if db["ok"] else "degraded",
        version=s.app_version,
        environment=s.environment,
        time=utcnow(),
        database=ComponentHealth(**db),
        processing_state=snapshot.processing_state,
        data_state=snapshot.data_state,
        openai=OpenAIConfigStatus(
            api_key_present=s.openai_configured,
            signal_model=s.openai_signal_model or None,
            analysis_model=s.openai_analysis_model or None,
            fallback_model=s.openai_fallback_model or None,
            availability_check="not_run",
        ),
    )


@router.get("/api/system/state", response_model=SystemStateOut, tags=["system"])
async def system_state(c: ContainerDep) -> SystemStateOut:
    return SystemStateOut.model_validate(c.state.snapshot())


@router.get("/api/market", response_model=MarketSnapshotOut, tags=["market"])
async def market(c: ContainerDep) -> MarketSnapshotOut:
    """Top-20 universe with live prices, per-asset fail-safe state and market context."""
    return await c.market.snapshot()


@router.get("/api/assets", response_model=AssetListOut, tags=["market"])
async def assets(c: ContainerDep) -> AssetListOut:
    universe = await c.universe.get()
    return AssetListOut(
        generated_at=utcnow(),
        listing_source=universe.listing_source,
        stale=universe.stale,
        assets=[
            AssetListItemOut(
                universe_rank=a.universe_rank,
                symbol=a.symbol,
                name=a.name,
                slug=a.slug,
                supported=a.supported,
                unsupported_reason=a.unsupported_reason,
                markets=[{"source": m.adapter, "symbol": m.symbol, "quote_asset": m.quote_asset} for m in a.markets],
                market_cap_usd=a.listing.market_cap_usd,
                listing_source=a.listing.source,
            )
            for a in universe.assets
        ],
    )


@router.get("/api/assets/{symbol}", response_model=AssetDetailOut, tags=["market"])
async def asset_detail(symbol: SymbolPath, c: ContainerDep) -> AssetDetailOut:
    """Full data collection + integrity gate for one asset (cached briefly)."""
    return await c.assets.detail(symbol)


@router.get("/api/assets/{symbol}/candles", response_model=CandlesOut, tags=["market"])
async def asset_candles(
    symbol: SymbolPath,
    c: ContainerDep,
    timeframe: Annotated[str, Query(description="5m, 15m, 1H, 4H or 1D")] = "1h",
    limit: Annotated[int, Query(ge=20, le=1000)] = 300,
) -> CandlesOut:
    try:
        tf = Timeframe.parse(timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return await c.assets.candles(symbol, tf, limit)


@router.get("/api/provider-health", response_model=ProviderHealthListOut, tags=["system"])
async def provider_health(c: ContainerDep) -> ProviderHealthListOut:
    stream = dataclasses.asdict(c.stream.status()) if c.stream is not None else None
    return ProviderHealthListOut(
        generated_at=utcnow(),
        providers=[ProviderHealthOut.model_validate(p) for p in c.health.snapshot()],
        live_stream=stream,
    )
