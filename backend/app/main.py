"""FastAPI application factory and entrypoint (uvicorn app.main:app)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import Settings, get_settings
from app.data.http import ProviderError
from app.logging_config import configure_logging
from app.services.assets import AssetNotInUniverse, AssetUnsupported
from app.services.container import Container, build_container
from app.services.listing import ListingUnavailable
from app.services.spot_router import NoMarketData

log = logging.getLogger("app")


def create_app(
    settings: Settings | None = None,
    container_factory: Callable[[Settings], Container] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.json_logs, secrets=settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = (container_factory or build_container)(settings)
        app.state.container = container
        log.info(
            "application started",
            extra={
                "version": settings.app_version,
                "environment": settings.environment,
                "universe_size": settings.universe_size,
                "openai_key_present": settings.openai_configured,
            },
        )
        if not settings.openai_configured:
            log.warning("OPENAI_API_KEY is not set: AI review is unavailable, so no AI-confirmed signals")
        if settings.cmc_key:
            log.info("CoinMarketCap API key configured: verifying plan in the background")
        else:
            log.info("CMC_API_KEY not set: using the CoinMarketCap keyless public API")
        warmup = asyncio.create_task(container.warmup(), name="warmup")
        try:
            yield
        finally:
            if not warmup.done():
                warmup.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await warmup
            await container.aclose()
            log.info("application stopped")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.exception_handler(AssetNotInUniverse)
    async def _not_in_universe(_: Request, exc: AssetNotInUniverse) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc), "symbol": exc.symbol})

    @app.exception_handler(AssetUnsupported)
    async def _unsupported(_: Request, exc: AssetUnsupported) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"detail": str(exc), "symbol": exc.symbol, "data_state": "API_FAILURE"}
        )

    @app.exception_handler(NoMarketData)
    async def _no_market(_: Request, exc: NoMarketData) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"detail": str(exc), "data_state": "API_FAILURE", "errors": exc.errors}
        )

    @app.exception_handler(ListingUnavailable)
    async def _no_listing(_: Request, exc: ListingUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"detail": str(exc), "data_state": "API_FAILURE", "errors": exc.errors}
        )

    @app.exception_handler(ProviderError)
    async def _provider(_: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"detail": exc.message, "provider": exc.provider, "data_state": "API_FAILURE"}
        )

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, object]:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "health": "/health",
            "docs": "/api/docs",
            "endpoints": ["/api/market", "/api/assets", "/api/assets/{symbol}", "/api/provider-health"],
        }

    app.include_router(router)
    return app


app = create_app()
