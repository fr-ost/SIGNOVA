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
    elif url.startswith("sqlite"):
        connect_args["timeout"] = 30  # wait for the single SQLite writer instead of failing at once
    return url, connect_args


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- application ---------------------------------------------------------
    app_name: str = "Crypto Market Analysis & Spot Signal Dashboard"
    app_version: str = "0.6.0-phase6"
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
    coingecko_pro_base_url: str = "https://pro-api.coingecko.com/api/v3"
    coingecko_api_key: SecretStr | None = None  # optional; free Demo key (or a paid Pro key)
    coingecko_plan: Literal["demo", "pro"] = "demo"
    coinpaprika_base_url: str = "https://api.coinpaprika.com/v1"  # free plan, no key
    coinpaprika_timeout_seconds: float = 30.0  # /tickers is a large download
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
    listing_fetch_limit: int = 200  # still one CoinMarketCap credit; covers watchlist coins
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
    # 4H and 1D need a long warm-up for EMA200 (Binance serves up to 1000 per request, Kraken 720).
    candle_fetch_limit_long: int = 1000
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

    # --- Phase 2: analysis and signals -------------------------------------------------
    analysis_cache_seconds: int = 30
    signal_scan_concurrency: int = 4
    regime_cache_seconds: int = 600
    regime_min_breadth_sample: int = 8
    signal_min_score_strong_buy: float = 80.0
    signal_min_score_buy: float = 65.0
    signal_min_score_watch: float = 50.0
    signal_persist_enabled: bool = True
    feature_persist_timeframes: CsvList = Field(default_factory=lambda: ["1h", "4h", "1d"])
    regime_persist_min_interval_seconds: int = 3600

    # --- Phase 3/4: controls, watchlist, news, chat, portfolio ---------------------------
    admin_token: SecretStr | None = None  # protects controls, chat and portfolio when set
    auto_analyze_minutes: int = 0  # 0 = analysis runs only when "Analyze now" is pressed
    news_cache_seconds: int = 900
    news_feeds: CsvList = Field(
        default_factory=lambda: [
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "https://cointelegraph.com/rss",
            "https://decrypt.co/feed",
            "https://bitcoinmagazine.com/.rss/full/",
        ]
    )
    cryptocompare_news_url: str = "https://min-api.cryptocompare.com/data/v2/news/?lang=EN"
    openai_base_url: str = "https://api.openai.com/v1"
    chat_max_output_tokens: int = 1500  # visible answer; reasoning models get extra room
    chat_reasoning_budget_tokens: int = 6000
    chat_reasoning_effort: Literal["minimal", "low", "medium", "high"] = "low"
    chat_model_options: CsvList = Field(
        default_factory=lambda: ["gpt-5-mini", "gpt-5", "gpt-5-nano", "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o4-mini"]
    )
    chat_rate_limit_per_minute: int = 10

    # --- Phase 5: sentiment and on-chain (free public sources) -------------------------
    sentiment_cache_seconds: int = 600
    onchain_cache_seconds: int = 600
    min_refresh_seconds: int = 60  # a forced refresh never hits the sources more often
    mempool_base_url: str = "https://mempool.space/api"
    blockchain_info_url: str = "https://blockchain.info"
    blockscout_eth_url: str = "https://eth.blockscout.com/api/v2"
    defillama_stablecoins_url: str = "https://stablecoins.llama.fi"
    binance_futures_url: str = "https://fapi.binance.com"
    whale_min_btc: float = 100.0
    whale_min_eth: float = 1000.0
    whale_alert_api_key: SecretStr | None = None  # optional: labelled exchange flows, all chains
    whale_alert_min_usd: int = 1_000_000

    # --- scalp signals (15m / 1h / 4h) ----------------------------------------------------
    scalp_slippage_pct: float = 0.02  # per side; fees come from the portfolio risk settings
    scalp_min_risk_cost_multiple: float = 2.5  # the stop must be at least this many round-trip costs away
    scalp_min_trades: int = 15  # backtest trades a coin needs before its own record counts
    track_record_days: int = 90

    # --- token unlocks and airdrops (optional keys) -------------------------------------
    events_cache_seconds: int = 21600
    mobula_base_url: str = "https://api.mobula.io/api/1"
    mobula_api_key: SecretStr | None = None  # free key; token unlock schedules
    alphadrops_base_url: str = "https://alphadrops.net/api/v1"
    alphadrops_api_key: SecretStr | None = None  # AlphaDrops Developer API subscription
    unlock_window_days: int = 30
    unlock_note_min_pct: float = 1.0  # % of circulating supply that turns an unlock into a risk note
    unlock_note_days: int = 14

    # --- Phase 2: risk engine (defaults match the risk_settings table) --------------------
    risk_max_per_signal_pct: float = 1.0
    risk_max_allocation_pct: float = 10.0
    risk_fee_pct: float = 0.1
    risk_slippage_pct: float = 0.05
    risk_min_reward_risk: float = 1.5
    risk_strong_min_reward_risk: float = 2.0
    risk_min_room_r: float = 0.75
    risk_strong_min_room_r: float = 1.0
    risk_max_stop_pct: float = 15.0
    risk_max_spread_bps: float = 30.0
    risk_min_depth_usd: float = 25_000.0
    risk_min_volume_24h_usd: float = 5_000_000.0
    risk_max_extension_atr: float = 2.5
    risk_max_change_24h_pct: float = 25.0

    @field_validator(
        "cors_origins",
        "binance_rest_base_urls",
        "binance_ws_base_urls",
        "integrity_required_timeframes",
        "feature_persist_timeframes",
        "news_feeds",
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

    @field_validator("integrity_required_timeframes", "feature_persist_timeframes")
    @classmethod
    def _validate_timeframes(cls, value: list[str]) -> list[str]:
        return [Timeframe.parse(item).value for item in value]

    def fetch_limit(self, timeframe: Timeframe) -> int:
        """Candles fetched per request for a timeframe."""
        if timeframe in (Timeframe.H4, Timeframe.D1):
            return max(self.candle_fetch_limit, self.candle_fetch_limit_long)
        return self.candle_fetch_limit

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
    def admin_token_value(self) -> str | None:
        value = self.admin_token.get_secret_value().strip() if self.admin_token else ""
        return value or None

    @staticmethod
    def _secret(value: SecretStr | None) -> str | None:
        text = value.get_secret_value().strip() if value else ""
        return text or None

    @property
    def coingecko_key(self) -> str | None:
        return self._secret(self.coingecko_api_key)

    @property
    def coingecko_url(self) -> str:
        """Pro keys use the pro host; Demo keys and keyless calls the public host."""
        return self.coingecko_pro_base_url if self.coingecko_key and self.coingecko_plan == "pro" else self.coingecko_base_url

    @property
    def coingecko_headers(self) -> dict[str, str]:
        key = self.coingecko_key
        if not key:
            return {}
        return {"x-cg-pro-api-key" if self.coingecko_plan == "pro" else "x-cg-demo-api-key": key}

    @property
    def mobula_key(self) -> str | None:
        return self._secret(self.mobula_api_key)

    @property
    def alphadrops_key(self) -> str | None:
        return self._secret(self.alphadrops_api_key)

    @property
    def whale_alert_key(self) -> str | None:
        value = self.whale_alert_api_key.get_secret_value().strip() if self.whale_alert_api_key else ""
        return value or None

    @property
    def chat_models(self) -> list[str]:
        models = [self.openai_analysis_model or "gpt-5-mini", self.openai_fallback_model or "gpt-4o-mini"]
        return list(dict.fromkeys(m for m in models if m))

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.get_secret_value().strip())

    def secret_values(self) -> list[str]:
        """Secrets that must be redacted from logs."""
        values = []
        for secret in (self.openai_api_key, self.cmc_api_key, self.admin_token, self.whale_alert_api_key,
                       self.coingecko_api_key, self.mobula_api_key, self.alphadrops_api_key):
            if secret is not None and secret.get_secret_value():
                values.append(secret.get_secret_value())
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()
