# Crypto Market Analysis & Spot Signal Dashboard

Railway-hosted market analysis and **spot-only** trading-signal dashboard for the
CoinMarketCap Top 20 (stablecoins excluded). FastAPI, PostgreSQL, React (from Phase 3),
OpenAI (from Phase 7).

No futures, no leverage, no short selling, no trade execution, no exchange credentials.
Nothing in this project promises profitability or accuracy.

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Railway, PostgreSQL, provider adapters, Binance, CMC Top 20, health and fail-safe | **Done** |
| 2 | Historical data, indicators, structure, regime, quantitative signal and risk engine | Next |
| 3 | On-demand controls, dashboard UI, charts, live updates | |
| 4 | Portfolio, risk, allocation, DCA, P/L scenarios | |
| 5 | News, sentiment, whale / on-chain | |
| 6 | Alerts | |
| 7 | OpenAI reasoning layer | |
| 8 | Signal tracking, backtesting, statistics | |
| 9 | ML / statistical prediction | |

## Decision hierarchy

```
data integrity -> fail-safe validation -> deterministic calculations -> risk engine
-> quantitative signal engine -> statistical/ML evidence -> OpenAI reasoning
-> final deterministic validation -> dashboard
```

The LLM can never override stale data, source conflicts, missing candles, abnormal prices,
failed risk checks, an emergency stop or a circuit breaker. Phase 1 builds the first two layers.

## Data sources (all keyless)

| Purpose | Primary | Fallbacks |
|---|---|---|
| Top 20 ranking, reference prices | CoinMarketCap Pro API (with `CMC_API_KEY`) | CMC keyless API, CoinGecko, CoinPaprika |
| Spot price, OHLCV, order book | Binance public Spot REST | Kraken public REST |
| Live stream (on demand) | Binance WebSocket | alternate Binance stream host |
| Total market cap, BTC dominance | CoinMarketCap | CoinGecko |
| Fear & Greed | CoinMarketCap index | alternative.me index (labelled, different index) |
| Altcoin Season Index | CoinMarketCap | none (shown as unavailable) |
| Candle-history cross-check | CoinMarketCap OHLCV (Startup plan or higher) | none (shown as not verified) |

`OPENAI_API_KEY` is the only required credential. `CMC_API_KEY` is optional; see below.

## CoinMarketCap API key

Set `CMC_API_KEY` as a Railway variable (never in Git). With a key the app:

1. **Detects your plan** from `/v1/key/info` (costs no credits): monthly credits, reset date,
   per-minute limit and live usage. The Pro request rate is tuned to 80% of your per-minute limit.
2. **Paces credits across the month.** A token bucket spreads the remaining credits evenly until
   the monthly reset, keeps a 5% reserve and respects CoinMarketCap's daily limit. Fail-safe data
   (Top 20 listing, reference prices) always has priority over extra verification.
3. **Never goes dark.** When Pro cannot serve a request (credit pacing, a limit, an outage, a
   rejected key), the official keyless public API serves it instead. `/api/market` shows
   `listing_access: pro | keyless`, and `/api/provider-health` shows the plan and credit usage.
4. **Handles every CoinMarketCap error code precisely.** An endpoint outside your plan (1006) is
   remembered for a day instead of being retried. A rejected or unpaid key (1001 to 1007) switches
   to keyless and is reported. Minute, daily, monthly and IP limits (1008 to 1011) each get their
   correct back-off.
5. **Cross-checks candle history.** On Startup plans and higher, closed 1H and 1D exchange candles
   are compared with CoinMarketCap's aggregated candles for the same UTC periods (USDT converted to
   USD). Conflicting history blocks signals (`DATA_CONFLICT`). On lower plans the check is shown as
   not verified and does not block.

| Feature | Basic / Builder | Startup and higher |
|---|---|---|
| Pro Top 20 listing, reference prices, global metrics, Fear & Greed, Altcoin Season | yes | yes |
| Plan detection, credit pacing, keyless fallback | yes | yes |
| 1H and 1D candle-history cross-check | not included in plan | yes |

Credits are only spent on demand. With the dashboard open all day the core feeds use at most about
2,300 credits per day (listing once a minute, context every 5 minutes); pacing keeps usage inside
your plan in all cases.

Every value in the API carries its source. When a source fails, a supported fallback is used
and labelled, or the value is shown as unavailable. Nothing is silently substituted: if no
exchange price is available, `price` is `null` with the reason, never the reference price.

## Universe rules

1. Rank the listing by market cap.
2. Exclude stablecoins always (provider tags, a curated list and a $1-peg check for USD-named
   tokens, so one missing tag cannot let a stablecoin through).
3. Exclude wrapped, bridged, liquid-staking and commodity-pegged tokens by default
   (`EXCLUDE_WRAPPED_ASSETS=true`): they only mirror another asset.
4. Take the first 20. Map each to a Binance USDT pair, else a Kraken USD pair.
5. An asset with no supported spot market stays in the list, marked unsupported, and can
   never produce a signal.

## Fail-safe layer

The integrity gate runs before any analysis and **fails closed**. Every stage is evaluated so
all reasons are shown, but a single failure means `NO TRADE`.

| Stage | Checks |
|---|---|
| `DATA_HEALTH_CHECK` | signals paused, market source available, provider not DOWN/RESTRICTED |
| `MARKET_DATA_CHECK` | live ticker present, valid price, bid below ask, 24h high above low |
| `TIMESTAMP_CHECK` | ticker age within limit, no future timestamps (clock skew) |
| `CANDLE_COMPLETENESS_CHECK` | per timeframe: closed candles only, alignment, OHLC sanity, duplicates, gaps in the recent window, staleness, enough history |
| `SOURCE_CONSISTENCY_CHECK` | exchange price vs independent reference (USDT converted to USD), 1H/1D candle history vs CoinMarketCap OHLCV (Startup plan or higher), abnormal candles must be corroborated |
| `VOLATILITY_CHECK` | realized 5m volatility vs its normal level, plus an absolute move limit |

States: `HEALTHY`, `DEGRADED`, `STALE_DATA`, `DATA_CONFLICT`, `API_FAILURE`,
`EXTREME_VOLATILITY`, `SIGNAL_PAUSED`.

Other protections: retries with exponential backoff and jitter, `Retry-After` handling for
429/418, per-provider circuit breakers, Binance request-weight tracking, HTTP 451/403
detection with automatic endpoint failover, WebSocket heartbeat, stale-stream watchdog,
reconnect with resubscribe, and proactive renewal before Binance's 24-hour limit.
A reference price older than `REFERENCE_MAX_AGE_SECONDS` is never used as current: the check
becomes `UNVERIFIED`, which blocks signals while `REQUIRE_PRICE_CROSS_VALIDATION=true`.

The data health score (0 to 100) weights freshness 30%, completeness 30%, cross-source
consistency 25% and provider health 15%. It is informational; the gate decision is stricter.

## API (Phase 1)

| Endpoint | Description |
|---|---|
| `GET /health` | Liveness, database check, processing state. Never calls external providers. |
| `GET /api/system/state` | Processing state and aggregate data state with reasons |
| `GET /api/market` | Top 20 with live prices, per-asset state and score, market context |
| `GET /api/assets` | Universe with market mapping |
| `GET /api/assets/{symbol}` | Full collection on 5 timeframes, order book, cross-check, volatility, integrity gate |
| `GET /api/assets/{symbol}/candles?timeframe=1H&limit=300` | Validated closed candles plus the forming candle, separately |
| `GET /api/provider-health` | Status, latency, errors, rate limits and circuit state per provider |

Interactive docs: `/api/docs`. Expensive endpoints are cached with single-flight loading, so
many open tabs never multiply provider calls.

## Deploy on Railway

1. Push this repository to GitHub and create a Railway project from it.
   Railway builds the `Dockerfile` (see `railway.json`).
2. Add a **PostgreSQL** service to the project.
3. In the app service **Variables**, set:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (reference variable)
   - `OPENAI_API_KEY` = your key (used from Phase 7; safe to add now)
   - `CMC_API_KEY` = your CoinMarketCap Pro key
   - `ENVIRONMENT` = `production`
4. **Region:** Binance answers HTTP 451 from restricted locations, including the United States.
   Choose a non-US region in the service settings. If Binance is still restricted, the app
   automatically uses Kraken and labels the source; check `/api/provider-health` after deploy.
5. Under **Networking**, generate a public domain and open it. `/health` must return 200.
6. Open `/api/provider-health`: the `coinmarketcap` entry should show `"mode": "pro"` and your
   plan's credit limit. `"pro_disabled"` means the key was rejected (the reason is shown).

Migrations run automatically on start (`scripts/start.sh`). The app runs as one worker by
design: system state, caches and the live stream are in-process.

## Local development

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql://app:app@localhost:5432/cryptosig
alembic upgrade head
uvicorn app.main:app --reload
```

## Tests

```bash
cd backend
pytest -q
# include the real PostgreSQL end-to-end test:
TEST_DATABASE_URL=postgresql://app:app@localhost:5432/cryptosig pytest -q
```

Covered: CoinMarketCap Pro integration (every error code, plan detection, credit pacing,
keyless fallback, key never sent to the keyless API or written to logs, v3 response shapes,
OHLCV parsing, candle-history cross-checks and their effect on the gate), configuration and
Railway URL handling, secret redaction, HTTP retry/backoff/
Retry-After/circuit breaker/451/403, Binance endpoint failover and weight limits, Kraken and
listing parsers, listing fallback, stablecoin and derivative exclusion, candle validation
(gaps, duplicates, invalid OHLC, misalignment, staleness, history, anomalies), cross-source
checks, order book sanity, volatility breaker, every integrity-gate stage, universe building,
market snapshot states and failover, API endpoints and error mapping, persistence and upserts
(SQLite and PostgreSQL), and the WebSocket manager (resubscribe, stale watchdog, renewal).
Fake adapters exist only in `backend/tests`; production code never generates data.

## Project structure

```
backend/
  app/
    main.py, config.py, database.py, logging_config.py
    api/                 routes and dependencies
    core/                enums, time helpers
    data/
      providers/         raw clients: Binance REST + WebSocket, Kraken, CoinMarketCap Pro +
                         keyless (with credit budget), CoinGecko, CoinPaprika, alternative.me
      adapters/          provider payloads -> normalized schemas
      normalization/     schemas, stablecoin / derivative classification
      validation/        candles, candle-history reference, prices, order book, volatility,
                         listings, health score, gate
      health.py, http.py provider health registry, resilient HTTP client
    services/            universe, listing, spot router, market, assets, context, persistence
    models/              20 SQLAlchemy tables
    schemas/             API response models
  migrations/            Alembic
  tests/
scripts/start.sh
Dockerfile, railway.json, .env.example
```

Later phases add `analysis/`, `risk/`, `ml/`, `ai/`, `workers/` and `frontend/`.
