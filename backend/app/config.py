"""Application configuration.

Only OPENAI_API_KEY is a user-supplied credential. Everything else has safe defaults
and can be overridden with environment variables (Railway service variables).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.enums import ALL_TIMEFRAMES, Timeframe

CsvList = Annotated[list[str], NoDecode]


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            return json.loads(text)
        return [part.strip() for part in text.split(",") if part.strip()]
    return value


def normalize_database_url(url: str) -> tuple[str, dict[str, Any]]:
    """Convert Railway/Heroku style Postgres URLs into SQLAlchemy asyncpg URLs.

    Returns the URL plus asyncpg connect_args. asyncpg rejects libpq-only query
    parameters such as ``sslmode``, so they are translated into connect_args.
    """
    url = url.strip()
    connect_args: dict[str, Any] = {}
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://") or url.startswith("postgresql+psycopg"):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
    if url.startswith("postgresql+asyncpg://"):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        sslmode = query.pop("sslmode", None)
        query.pop("channel_binding", None)
        if sslmode and sslmode != "disable":
            connect_args["ssl"] = sslmode
        url = urlunsplit(parts._replace(query=urlencode(query)))
    return url, connect_args


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- application ---------------------------------------------------------
    app_name: str = "Crypto Market Analysis & Spot Signal Dashboard"
    app_version: str = "0.1.1-phase1"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    json_logs: bool = True
    port: int = 8000
    cors_origins: CsvList = Field(default_factory=list)

    # --- database ------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/cryptosig"
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # --- OpenAI (the only required user credential; used from the AI phase on) ---
    openai_api_key: SecretStr | None = None
    openai_signal_model: str = ""
    openai_analysis_model: str = ""
    openai_fallback_model: str = ""

    # --- CoinMarketCap (optional paid key; keyless public API is always the fallback) ---
    cmc_api_key: SecretStr | None = None
    cmc_keyless_fallback: bool = True
    cmc_credit_safety_factor: float = 0.9
    cmc_credit_reserve_pct: float = 5.0
    cmc_plan_refresh_seconds: float = 300.0

    # --- provider endpoints ------------------------------------------------------
    binance_rest_base_urls: CsvList = Field(
        default_factory=lambda: [
            "https://api.binance.com",
            "https://api-gcp.binance.com",
            "https://data-api.binance.vision",
        ]
    )
    binance_ws_base_urls: CsvList = Field(
        default_factory=lambda: [
            "wss://stream.binance.com:9443",
            "wss://data-stream.binance.vision",
        ]
    )
    binance_quote_asset: str = "USDT"
    binance_weight_limit_1m: int = 6000
    kraken_base_url: str = "https://api.kraken.com"
    kraken_quote_asset: str = "USD"
    cmc_public_base_url: str = "https://pro-api.coinmarketcap.com/public-api"
    cmc_pro_base_url: str = "https://pro-api.coinmarketcap.com"
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    coinpaprika_base_url: str = "https://api.coinpaprika.com/v1"
    alternative_me_base_url: str = "https://api.alternative.me"

    # --- HTTP resilience ---------------------------------------------------------
    http_timeout_seconds: float = 10.0
    http_max_retries: int = 3
    http_backoff_base_seconds: float = 0.5
    http_backoff_max_seconds: float = 8.0
    circuit_failure_threshold: int = 5
    circuit_recovery_seconds: float = 60.0

    # --- universe --------------------------------------------------------------------
    universe_size: int = 20
    universe_refresh_seconds: int = 600
    listing_refresh_seconds: int = 60
    listing_fetch_limit: int = 100
    exclude_wrapped_assets: bool = True
    symbol_overrides: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)

    # --- caching -----------------------------------------------------------------------
    market_cache_seconds: int = 20
    context_cache_seconds: int = 300
    asset_detail_cache_seconds: int = 15
    market_snapshot_persist_seconds: int = 300
    exchange_pairs_ttl_seconds: int = 21600

    # --- fail-safe thresholds ---------------------------------------------------------
    ticker_max_age_seconds: float = 120.0
    listing_max_age_seconds: float = 900.0
    reference_max_age_seconds: float = 300.0
    cross_source_warn_deviation_pct: float = 0.5
    cross_source_max_deviation_pct: float = 1.5
    require_price_cross_validation: bool = True
    candle_fetch_limit: int = 500
    candle_min_history: int = 210
    candle_stale_tolerance_intervals: float = 1.0
    candle_stale_grace_seconds: float = 90.0
    candle_recent_window: int = 50
    candle_max_recent_missing_ratio: float = 0.02
    candle_outlier_zscore: float = 12.0
    extreme_volatility_ratio: float = 4.0
    extreme_volatility_min_move_pct: float = 3.0
    extreme_volatility_absolute_move_pct: float = 8.0
    orderbook_depth_levels: int = 100
    candle_reference_enabled: bool = True
    candle_reference_daily_count: int = 60
    candle_reference_hourly_count: int = 72
    candle_reference_daily_cache_seconds: float = 21600.0
    candle_reference_hourly_cache_seconds: float = 1800.0
    candle_reference_warn_pct: float = 1.0
    candle_reference_max_pct: float = 2.5
    candle_reference_min_overlap: int = 10
    orderbook_band_pct: float = 1.0
    integrity_required_timeframes: CsvList = Field(
        default_factory=lambda: [tf.value for tf in ALL_TIMEFRAMES]
    )

    @field_validator(
        "cors_origins",
        "binance_rest_base_urls",
        "binance_ws_base_urls",
        "integrity_required_timeframes",
        mode="before",
    )
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @field_validator("symbol_overrides", mode="before")
    @classmethod
    def _parse_overrides(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            if text.startswith("{"):
                return json.loads(text)
            pairs = [item.split(":", 1) for item in text.split(",") if ":" in item]
            return {k.strip().upper(): v.strip().upper() for k, v in pairs}
        return value

    @field_validator("integrity_required_timeframes")
    @classmethod
    def _validate_timeframes(cls, value: list[str]) -> list[str]:
        return [Timeframe.parse(item).value for item in value]

    @property
    def required_timeframes(self) -> tuple[Timeframe, ...]:
        return tuple(Timeframe(value) for value in self.integrity_required_timeframes)

    @property
    def async_database_url(self) -> str:
        return normalize_database_url(self.database_url)[0]

    @property
    def database_connect_args(self) -> dict[str, Any]:
        return normalize_database_url(self.database_url)[1]

    @property
    def cmc_key(self) -> str | None:
        value = self.cmc_api_key.get_secret_value().strip() if self.cmc_api_key else ""
        return value or None

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.get_secret_value().strip())

    def secret_values(self) -> list[str]:
        """Secrets that must be redacted from logs."""
        values = []
        for secret in (self.openai_api_key, self.cmc_api_key):
            if secret is not None and secret.get_secret_value():
                values.append(secret.get_secret_value())
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()
