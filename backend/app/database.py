"""Async SQLAlchemy engine and session management."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    url = settings.async_database_url
    kwargs: dict[str, Any] = {"pool_pre_ping": True, "connect_args": settings.database_connect_args}
    if url.startswith("postgresql"):
        kwargs.update(pool_size=settings.db_pool_size, max_overflow=settings.db_max_overflow, pool_recycle=1800)
    return create_async_engine(url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def check_database(engine: AsyncEngine) -> dict[str, Any]:
    started = time.monotonic()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # report, never raise from a health check
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
