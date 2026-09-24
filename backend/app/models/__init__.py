"""Import every model so Base.metadata is complete (Alembic autogenerate, create_all)."""

from app.models.base import Base
from app.models.intel import NewsItem, SentimentReading, WhaleEvent
from app.models.market import (
    Asset,
    Candle,
    MarketRegime,
    MarketSnapshot,
    OrderbookSnapshot,
    TechnicalFeature,
    WatchlistItem,
)
from app.models.portfolio import Portfolio, PortfolioPosition, RiskSettings
from app.models.signals import BacktestResult, ModelPrediction, Signal, SignalOutcome, SignalTarget
from app.models.system import Alert, AppSetting, ProviderHealthRecord, SystemEvent

__all__ = [
    "Alert",
    "AppSetting",
    "Asset",
    "BacktestResult",
    "Base",
    "Candle",
    "MarketRegime",
    "MarketSnapshot",
    "ModelPrediction",
    "NewsItem",
    "OrderbookSnapshot",
    "Portfolio",
    "PortfolioPosition",
    "ProviderHealthRecord",
    "RiskSettings",
    "SentimentReading",
    "Signal",
    "SignalOutcome",
    "SignalTarget",
    "SystemEvent",
    "TechnicalFeature",
    "WatchlistItem",
    "WhaleEvent",
]
