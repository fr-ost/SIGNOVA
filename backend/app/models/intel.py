"""News, sentiment and whale/on-chain tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK, CreatedAtMixin, JSONType, TZDateTime


class NewsItem(CreatedAtMixin, Base):
    __tablename__ = "news"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    source_rank: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[datetime | None] = mapped_column(TZDateTime, index=True)
    collected_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    assets: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    category: Mapped[str] = mapped_column(String(32), default="other", nullable=False)
    sentiment: Mapped[str] = mapped_column(String(16), default="unavailable", nullable=False)
    importance: Mapped[int | None] = mapped_column(Integer)
    is_official: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class SentimentReading(CreatedAtMixin, Base):
    __tablename__ = "sentiment"
    __table_args__ = (Index("ix_sentiment_scope_symbol_time", "scope", "symbol", "computed_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    computed_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    sample_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    trend: Mapped[str | None] = mapped_column(String(16))
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class WhaleEvent(CreatedAtMixin, Base):
    __tablename__ = "whale_events"
    __table_args__ = (Index("ix_whale_events_symbol_time", "symbol", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    event_uid: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    chain: Mapped[str | None] = mapped_column(String(32))
    symbol: Mapped[str | None] = mapped_column(String(32))
    tx_hash: Mapped[str | None] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    classification: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    amount: Mapped[float | None] = mapped_column(Float)
    amount_usd: Mapped[float | None] = mapped_column(Float)
    from_label: Mapped[str | None] = mapped_column(String(128))
    to_label: Mapped[str | None] = mapped_column(String(128))
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
