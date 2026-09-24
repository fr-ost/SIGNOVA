"""Signals, targets, outcomes, model predictions and backtests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK, CreatedAtMixin, JSONType, TZDateTime


class Signal(CreatedAtMixin, Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_symbol_created", "symbol", "created_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    signal: Mapped[str] = mapped_column(String(16), nullable=False)
    signal_score: Mapped[int] = mapped_column(Integer, nullable=False)
    data_health_score: Mapped[int] = mapped_column(Integer, nullable=False)
    data_state: Mapped[str] = mapped_column(String(32), nullable=False)
    entry_low: Mapped[float | None] = mapped_column(Float)
    entry_high: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    risk_reward: Mapped[float | None] = mapped_column(Float)
    trend: Mapped[str | None] = mapped_column(String(16))
    market_regime: Mapped[str | None] = mapped_column(String(32))
    reasons: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    risks: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    invalidation: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", nullable=False)
    is_backtest: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ai_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    feature_version: Mapped[str | None] = mapped_column(String(32))
    engine_version: Mapped[str | None] = mapped_column(String(32))
    input_features: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    quant_output: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    ai_output: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class SignalTarget(Base):
    __tablename__ = "signal_targets"
    __table_args__ = (UniqueConstraint("signal_id", "kind", "level_index", name="uq_signal_targets_level"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    level_index: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    allocation_pct: Mapped[float | None] = mapped_column(Float)


class SignalOutcome(Base):
    __tablename__ = "signal_outcomes"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Float)
    return_pct: Mapped[float | None] = mapped_column(Float)
    r_multiple: Mapped[float | None] = mapped_column(Float)
    mfe_pct: Mapped[float | None] = mapped_column(Float)
    mae_pct: Mapped[float | None] = mapped_column(Float)
    time_to_outcome_seconds: Mapped[int | None] = mapped_column(Integer)
    hit_targets: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class ModelPrediction(CreatedAtMixin, Base):
    __tablename__ = "model_predictions"
    __table_args__ = (Index("ix_model_predictions_symbol_time", "symbol", "timeframe", "candle_open_time"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    candle_open_time: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    target: Mapped[str] = mapped_column(String(32), nullable=False)
    probability: Mapped[float | None] = mapped_column(Float)
    validated_out_of_sample: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)


class BacktestResult(CreatedAtMixin, Base):
    __tablename__ = "backtest_results"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    symbols: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    trades: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
