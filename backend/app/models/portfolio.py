"""Portfolio, positions and editable risk settings (spot only, no exchange credentials)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntPK, CreatedAtMixin, TZDateTime

Money = Numeric(24, 8)
Pct = Numeric(8, 4)


class Portfolio(CreatedAtMixin, Base):
    __tablename__ = "portfolio"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now(), nullable=False)


class PortfolioPosition(Base):
    __tablename__ = "portfolio_positions"
    __table_args__ = (UniqueConstraint("portfolio_id", "symbol", name="uq_portfolio_positions_symbol"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolio.id", ondelete="CASCADE"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False)
    average_entry: Mapped[Decimal] = mapped_column(Money, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now(), nullable=False)


class RiskSettings(Base):
    __tablename__ = "risk_settings"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolio.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    max_risk_per_signal_pct: Mapped[Decimal] = mapped_column(Pct, default=Decimal("1.0"), nullable=False)
    initial_allocation_pct: Mapped[Decimal] = mapped_column(Pct, default=Decimal("5.0"), nullable=False)
    max_allocation_per_opportunity_pct: Mapped[Decimal] = mapped_column(
        Pct, default=Decimal("10.0"), nullable=False
    )
    max_dca_allocation_pct: Mapped[Decimal] = mapped_column(Pct, default=Decimal("5.0"), nullable=False)
    max_total_open_allocation_pct: Mapped[Decimal] = mapped_column(Pct, default=Decimal("60.0"), nullable=False)
    fee_pct: Mapped[Decimal] = mapped_column(Pct, default=Decimal("0.1"), nullable=False)
    slippage_pct: Mapped[Decimal] = mapped_column(Pct, default=Decimal("0.05"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, server_default=func.now(), nullable=False)
