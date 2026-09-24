"""Operational tables: provider health history, system events, alerts."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK, CreatedAtMixin, JSONType, TZDateTime


class ProviderHealthRecord(Base):
    __tablename__ = "provider_health"
    __table_args__ = (Index("ix_provider_health_provider_time", "provider", "recorded_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    recorded_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    circuit_state: Mapped[str] = mapped_column(String(16), nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    total_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    total_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    rate_limit_hits: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_latency_ms: Mapped[float | None] = mapped_column(Float)
    last_error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class SystemEvent(CreatedAtMixin, Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class Alert(CreatedAtMixin, Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_ack_created", "acknowledged", "created_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class AppSetting(Base):
    """Small user preferences that must survive restarts (e.g. which coins a scan analyses)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
