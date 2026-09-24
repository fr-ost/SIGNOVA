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
| 2 | Historical data, indicators, structure, regime, quantitative signal and risk engine | **Done** |
| 3 | On-demand controls, dashboard UI, charts, live updates | Next |
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
failed risk checks, an emergency stop or a circuit breaker. Phase 1 built the first two layers;
Phase 2 adds the deterministic calculations, the risk engine, the quantitative signal engine and
the final deterministic validation.

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

## Signals (Phase 2)

Spot long only. The engine is deterministic: the same candles always give the same signal, and
every point of the score and every limit is explained in the API and on the dashboard. The score
ranks setups; it is **not a probability**, and there is no measured track record until signal
tracking and backtesting arrive in Phase 8.

**Strategy:** multi-timeframe trend following with pullback entries. 1D sets the primary trend,
4H is the setup timeframe, 1H confirms momentum, 15m times the entry.

**Pipeline** (the label can only be lowered after scoring, never raised):

```
integrity gate (6 data stages) -> ANALYSIS_CHECK -> score -> trade plan
-> RISK_CHECK (block / cap / downgrade) -> market regime cap -> FINAL_VALIDATION -> label
```

A failed data stage, a failed analysis check, a blocking risk check or a final-validation
violation always means `NO TRADE`.

**Score (0 to 100):**

| Factor | Points | Looks at |
|---|---|---|
| Trend alignment | 30 | EMA20/50/200 alignment and swing structure on 1D, 4H, 1H; 4H ADX and +DI/-DI |
| Momentum | 20 | RSI(14) zones and MACD histogram level and direction on 4H and 1H |
| Market structure | 15 | 4H higher highs/lows, latest break of structure, nearby support, room to resistance |
| Entry location | 15 | distance from the 4H EMA20 in ATRs, Bollinger %B, 15m RSI |
| Volume | 10 | 4H on-balance volume trend, buying versus selling volume on 4H and 1H |
| Market context | 10 | market regime, 20-day performance versus Bitcoin |

**Labels:** `STRONG BUY` from 80, `BUY` from 65, `WATCH` from 50, otherwise `NO TRADE`
(thresholds configurable), then limited by the risk checks and the market regime.
`WATCH` means "a setup exists but not now" and carries a non-actionable watch plan with the
reason (for example "wait for a breakout above X" or "extended: wait for a pullback").

**Trade plan** (quote currency, spot):

* Entry zone from the live price down to 0.5 ATR (4H) below it. Reward:risk is measured from the
  top of the zone, the least favourable fill.
* Stop 0.25 ATR below the most recent 4H swing low under the entry zone, kept between 1 and 3 ATR
  from the entry (2 ATR without a swing low).
* TP1 is the nearest overhead resistance (4H and 1D swing clusters) however close; TP2 and TP3 the
  next levels. Without resistance (price discovery) targets are labelled 1.5R / 3R / 4.5R
  projections. Suggested scale-out 40% / 35% / 25%.
* Reward:risk is net of round-trip fees and slippage (0.1% + 0.05% per side by default), at TP2.
* Size: the portfolio share that loses at most `RISK_MAX_PER_SIGNAL_PCT` (1%) if the stop is hit,
  capped at `RISK_MAX_ALLOCATION_PCT` (10%).

**Risk checks:**

| Effect | Checks |
|---|---|
| Block (`NO TRADE`) | order book missing or invalid, spread above 30 bps, less than $25,000 depth within 1% on either side, 24h volume below $5M |
| Cap (at most `WATCH`) | 1D or 4H trend down, net reward:risk below 1.5R, nearest resistance closer than 0.75R, stop wider than 15%, price more than 2.5 ATR above the 4H EMA20, RSI above 78 (4H) or 80 (1D), 24h move above +25%, bear or unknown market regime |
| Downgrade (at most `BUY`) | 1D and 4H not both up, net reward:risk below 2R, resistance closer than 1R, 15m RSI above 85, 4H volatility at the 95th percentile, neutral market regime |

**Market regime:** Bitcoin's daily trend (EMA50/EMA200 and slope) plus breadth, the share of the
universe trading above its daily EMA50. `BULL` allows every label, `NEUTRAL` at most `BUY`, `BEAR`
and `UNKNOWN` (Bitcoin data unavailable) at most `WATCH`. Fear & Greed extremes and high Bitcoin
volatility are reported as risks.

**Indicators** are pure Python, computed on validated closed candles only (Wilder RSI/ATR/ADX, EMA
seeded with its SMA, population-deviation Bollinger Bands, MACD, OBV, ROC). They are tested against
the published StockCharts RSI example and cross-checked with the `ta` library. Swing structure
uses confirmed pivots only, so nothing repaints. 4H and 1D use 1,000 candles of history so the
EMA200 is fully warmed up.

**History:** every analysis stores its 1H/4H/1D indicator snapshot once per closed candle
(`technical_features`), a signal row with its targets when the label changes or a new 4H setup
candle closes (`signals`, `signal_targets`, including the full inputs and outputs for later
backtesting), and the market regime at most hourly or on change (`market_regimes`).

**Provider usage:** a full scan analyses the whole universe with at most 4 assets in parallel and
is cached for 2 minutes, so any number of open dashboards triggers at most one scan per 2 minutes
(about 350 Binance request weight of the 6,000 per-minute limit). CoinMarketCap candle
cross-checks stay cached per asset (30 minutes for 1H, 6 hours for 1D) under the credit pacing
described above.

## API

| Endpoint | Description |
|---|---|
| `GET /` | Status dashboard (see below) |
| `GET /api` | JSON index of the API |
| `GET /health` | Liveness, database check, processing state. Never calls external providers. |
| `GET /api/system/state` | Processing state and aggregate data state with reasons |
| `GET /api/market` | Top 20 with live prices, per-asset state and score, market context |
| `GET /api/assets` | Universe with market mapping |
| `GET /api/assets/{symbol}` | Full collection on 5 timeframes, order book, cross-check, volatility, integrity gate |
| `GET /api/assets/{symbol}/candles?timeframe=1H&limit=300` | Validated closed candles plus the forming candle, separately |
| `GET /api/provider-health` | Status, latency, errors, rate limits and circuit state per provider |
| `GET /api/signals` | Phase 2: signal for every universe asset, best first, with the market regime (cached 2 minutes) |
| `GET /api/assets/{symbol}/analysis` | Phase 2: indicators, structure, regimes, score factors, trade plan, risk checks, full pipeline |
| `GET /api/market/regime` | Phase 2: market regime and the signal cap it applies |
| `GET /api/signals/history?symbol=BTC&limit=50` | Phase 2: stored signals with their targets, newest first |

Interactive docs: `/api/docs`. Expensive endpoints are cached with single-flight loading, so
many open tabs never multiply provider calls.

## Status dashboard

Opening the service URL shows a read-only page built on the API above: the market regime,
market context (total market cap, BTC dominance, Fear & Greed, Altcoin Season), the signals
table (label, score, trend, entry zone, stop, TP1/TP2, net reward:risk, suggested size and the
main reason), the Top 20 with live price, 24h change, price cross-check and data state, and
provider health including CoinMarketCap credit usage. Selecting an asset shows its signal,
trade plan, reasons and risks, score breakdown, the full pipeline, every risk check,
indicators and structure per timeframe, then the Phase 1 integrity details.

It is plain HTML, CSS and JavaScript in `backend/app/static` with no build step and no
third-party scripts (strict Content-Security-Policy). It refreshes every 30 seconds only while
the tab is visible, so a background tab spends no provider calls or credits. The React
dashboard in Phase 3 replaces it.

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
5. Under **Networking**, generate a public domain and open it: the status dashboard loads.
   `/health` must return 200.
6. Open `/api/provider-health`: the `coinmarketcap` entry should show `"mode": "pro"` and your
   plan's credit limit. `"pro_disabled"` means the key was rejected (the reason is shown).

Migrations run automatically on start (`scripts/start.sh`). The app runs as one worker by
design: system state, caches and the live stream are in-process. Phase 2 needs no new
migration: it fills tables created by the initial schema.

## Configuration (Phase 2)

Every setting has a safe default; override any of them as a Railway variable.

| Variable | Default | Meaning |
|---|---|---|
| `SIGNAL_MIN_SCORE_STRONG_BUY` / `_BUY` / `_WATCH` | 80 / 65 / 50 | score thresholds |
| `RISK_MAX_PER_SIGNAL_PCT` | 1.0 | portfolio % lost if a stop is hit (drives the suggested size) |
| `RISK_MAX_ALLOCATION_PCT` | 10.0 | largest suggested position, % of portfolio |
| `RISK_FEE_PCT` / `RISK_SLIPPAGE_PCT` | 0.1 / 0.05 | per side, used for net reward:risk and sizing |
| `RISK_MIN_REWARD_RISK` / `RISK_STRONG_MIN_REWARD_RISK` | 1.5 / 2.0 | net reward:risk at TP2 for BUY / STRONG BUY |
| `RISK_MIN_ROOM_R` / `RISK_STRONG_MIN_ROOM_R` | 0.75 / 1.0 | room to the nearest resistance, in R |
| `RISK_MAX_STOP_PCT` | 15 | widest stop allowed |
| `RISK_MAX_SPREAD_BPS` / `RISK_MIN_DEPTH_USD` / `RISK_MIN_VOLUME_24H_USD` | 30 / 25000 / 5000000 | liquidity limits |
| `RISK_MAX_EXTENSION_ATR` / `RISK_MAX_CHANGE_24H_PCT` | 2.5 / 25 | chasing limits |
| `SIGNAL_SCAN_CACHE_SECONDS` / `SIGNAL_SCAN_CONCURRENCY` | 120 / 4 | scan cadence and parallelism |
| `REGIME_CACHE_SECONDS` | 600 | market regime refresh |
| `CANDLE_FETCH_LIMIT` / `CANDLE_FETCH_LIMIT_LONG` | 500 / 1000 | candles per request (5m-1H / 4H-1D) |
| `SIGNAL_PERSIST_ENABLED` / `FEATURE_PERSIST_TIMEFRAMES` | true / 1h,4h,1d | history storage |

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

Phase 2: indicator math against published and library reference values, swing pivots, structure,
change of character, level clustering, timeframe and market regimes, and the full engine on
deterministic synthetic markets sampled into consistent 5m-1D candles (strong uptrend, downtrend,
parabolic move, failed integrity gate, wide spread, bear and neutral regimes, missing history,
unsupported asset, STRONG BUY downgrades), trade-plan math net of costs, sizing, final validation,
and the Phase 2 endpoints with persistence and de-duplication (SQLite and PostgreSQL).
Fake adapters and synthetic markets exist only in `backend/tests`; production code never
generates data.

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
    analysis/            indicators, features, structure, regime, scoring, engine, final validation
    risk/                risk parameters, trade plan, risk checks
    services/            universe, listing, spot router, market, assets, context, persistence,
                         market regime, analysis (signals, scan, history)
    static/              status dashboard (HTML, CSS, JS; no build step)
    models/              20 SQLAlchemy tables
    schemas/             API response models
  migrations/            Alembic
  tests/
scripts/start.sh
Dockerfile, railway.json, .env.example
```

Later phases add `ml/`, `ai/`, `workers/` and `frontend/`.
