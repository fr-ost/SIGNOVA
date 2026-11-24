<div align="center">

<img width="100%" alt="Signova — AI crypto signals for spot and futures" src="https://capsule-render.vercel.app/api?type=waving&amp;color=0:6C2BD9,50:FF2E93,100:FFB703&amp;height=210&amp;section=header&amp;text=Signova&amp;fontSize=58&amp;fontColor=ffffff&amp;fontAlignY=30&amp;animation=fadeIn&amp;desc=AI%20crypto%20signals%20%C2%B7%20spot%20%26%20futures&amp;descAlignY=62&amp;descAlign=50&amp;descSize=18"/>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://readme-typing-svg.demolab.com?font=Fira+Code&amp;weight=600&amp;size=22&amp;duration=3600&amp;pause=700&amp;color=FFB703&amp;center=true&amp;vCenter=true&amp;width=820&amp;height=48&amp;lines=AI+crypto+signals+for+spot+and+futures;Analysis+only+%C2%B7+you+place+every+order;FastAPI+%C2%B7+PostgreSQL+%C2%B7+OpenAI;Top+20+by+market+cap%2C+stablecoins+out;Fail-closed+data+gate+before+any+signal" />
  <source media="(prefers-color-scheme: light)" srcset="https://readme-typing-svg.demolab.com?font=Fira+Code&amp;weight=600&amp;size=22&amp;duration=3600&amp;pause=700&amp;color=6C2BD9&amp;center=true&amp;vCenter=true&amp;width=820&amp;height=48&amp;lines=AI+crypto+signals+for+spot+and+futures;Analysis+only+%C2%B7+you+place+every+order;FastAPI+%C2%B7+PostgreSQL+%C2%B7+OpenAI;Top+20+by+market+cap%2C+stablecoins+out;Fail-closed+data+gate+before+any+signal" />
  <img width="820" alt="AI crypto signals for spot and futures. Analysis only: you place every order. FastAPI, PostgreSQL, and OpenAI. Top 20 by market cap, stablecoins excluded. A fail-closed data gate runs before any signal." src="https://readme-typing-svg.demolab.com?font=Fira+Code&amp;weight=600&amp;size=22&amp;duration=3600&amp;pause=700&amp;color=6C2BD9&amp;center=true&amp;vCenter=true&amp;width=820&amp;height=48&amp;lines=AI+crypto+signals+for+spot+and+futures;Analysis+only+%C2%B7+you+place+every+order;FastAPI+%C2%B7+PostgreSQL+%C2%B7+OpenAI;Top+20+by+market+cap%2C+stablecoins+out;Fail-closed+data+gate+before+any+signal" />
</picture>

*A Railway dashboard that reads the market, scores setups, and shows plans you place yourself.*

<br/>

<img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&amp;logo=python&amp;logoColor=white"/>
<img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&amp;logo=fastapi&amp;logoColor=white"/>
<img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&amp;logo=postgresql&amp;logoColor=white"/>
<img alt="SQLAlchemy" src="https://img.shields.io/badge/SQLAlchemy-D71F00?style=for-the-badge&amp;logo=sqlalchemy&amp;logoColor=white"/>
<img alt="OpenAI" src="https://img.shields.io/badge/OpenAI-412991?style=for-the-badge&amp;logo=openai&amp;logoColor=white"/>
<br/>
<img alt="Docker" src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&amp;logo=docker&amp;logoColor=white"/>
<img alt="Railway" src="https://img.shields.io/badge/Railway-6C2BD9?style=for-the-badge&amp;logo=railway&amp;logoColor=white"/>
<img alt="Alembic migrations" src="https://img.shields.io/badge/Alembic-FF2E93?style=for-the-badge"/>
<img alt="Analysis only. No trade execution." src="https://img.shields.io/badge/Analysis_only-FFB703?style=for-the-badge&amp;labelColor=1A1024"/>

<br/>

<a href="#getting-started">Getting started</a>
&nbsp;·&nbsp;
<a href="#configuration">Configuration</a>
&nbsp;·&nbsp;
<a href="#api">API</a>
&nbsp;·&nbsp;
<a href="#credits">Credits</a>

</div>

> [!CAUTION]
> **Analysis only.** Signova does not place trades and does not take exchange credentials. Futures signals are plans you place yourself, with leverage kept inside your cap and the estimated liquidation price beyond the stop. Nothing in this project promises profitability or accuracy.

> [!IMPORTANT]
> The language model can never override stale data, source conflicts, missing candles, abnormal prices, a failed risk check, an emergency stop, or a circuit breaker. Evidence, the statistical filter, and the AI review can only **lower** a signal.

## ✨ Overview

Signova is a [Railway](https://railway.com)-hosted market-analysis and trading-signal dashboard for the CoinMarketCap Top 20, with stablecoins excluded. An OpenAI analyst reads one coin's full dossier and proposes a plan (Phase 13). Next to it, measured rule engines produce spot signals and long and short signals for USDT perpetual futures. The stack is FastAPI, PostgreSQL, and OpenAI.

The same candles always produce the same rule-engine signal. Scores rank setups. They are **not probabilities**. How those signals actually performed is measured afterwards in the track record.

`OPENAI_API_KEY` is the only required credential. `CMC_API_KEY` is optional. With a key, CoinMarketCap Pro is used and paced. Without one, the keyless public API is used and labelled.

## ⚡ Key features

| | What it does |
| --- | --- |
| 🛡️ **Fail-closed gate** | Six data checks run before any analysis. One failure means `NO TRADE`. |
| 📈 **Spot signals** | Multi-timeframe trend following with a pullback entry, a sized plan, and a reward:risk net of fees. |
| ⏱️ **Scalp signals** | 15m, 1h, 4h, and 1d spot longs. A label is earned only if the same rules made money on that coin's history. |
| 📚 **Strategy library** | Eight published strategies, each with its own exit, walk-forward tested on every scan. |
| 📉 **Futures** | Long and short plans on USDT perpetuals, including a leverage plan whose liquidation sits beyond the stop. |
| 🔎 **Evidence board** | Funding, liquidations, flow, news, and market context. It can only hold a buy back. |
| 🤖 **Signova AI** | One token at a time. Spot and futures from a single call, then an independent risk-manager pass. |
| 📒 **Track record** | Live signals are followed on later candles. Held-back setups are tracked in their own lane. |
| 🎛️ **On demand** | Scans, news, and OpenAI run when you ask. An idle dashboard does not spend provider credits. |
| 🛑 **Emergency stop** | Halts scans, live prices, the lab, and AI calls, and survives a restart. |

## 🧰 Tech stack

<div align="center">

<img alt="Python 3.12" src="https://img.shields.io/badge/Python-3.12-3776AB?style=for-the-badge&amp;logo=python&amp;logoColor=white"/>
<img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&amp;logo=fastapi&amp;logoColor=white"/>
<img alt="Uvicorn" src="https://img.shields.io/badge/Uvicorn-499848?style=for-the-badge&amp;logo=uvicorn&amp;logoColor=white"/>
<img alt="Pydantic" src="https://img.shields.io/badge/Pydantic-E92063?style=for-the-badge&amp;logo=pydantic&amp;logoColor=white"/>
<img alt="SQLAlchemy 2" src="https://img.shields.io/badge/SQLAlchemy-2.0-D71F00?style=for-the-badge&amp;logo=sqlalchemy&amp;logoColor=white"/>
<br/>
<img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&amp;logo=postgresql&amp;logoColor=white"/>
<img alt="asyncpg" src="https://img.shields.io/badge/asyncpg-4169E1?style=for-the-badge"/>
<img alt="Alembic" src="https://img.shields.io/badge/Alembic-FF2E93?style=for-the-badge"/>
<img alt="OpenAI" src="https://img.shields.io/badge/OpenAI-412991?style=for-the-badge&amp;logo=openai&amp;logoColor=white"/>
<img alt="httpx" src="https://img.shields.io/badge/httpx-6C2BD9?style=for-the-badge"/>
<img alt="WebSockets" src="https://img.shields.io/badge/WebSockets-FFB703?style=for-the-badge&amp;labelColor=1A1024"/>
<br/>
<img alt="Docker" src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&amp;logo=docker&amp;logoColor=white"/>
<img alt="Railway" src="https://img.shields.io/badge/Deployed_on_Railway-6C2BD9?style=for-the-badge&amp;logo=railway&amp;logoColor=white"/>
<img alt="pytest" src="https://img.shields.io/badge/pytest-0A9EDC?style=for-the-badge&amp;logo=pytest&amp;logoColor=white"/>

</div>

The status page is plain HTML, CSS, and JavaScript in `backend/app/static`. There is no frontend build step and no third-party scripts. A strict Content-Security-Policy is set. The service runs as **one worker**: system state, caches, and the live stream live in-process.

Public market data comes from CoinMarketCap, Binance, Kraken, CoinGecko, CoinPaprika, alternative.me, and the futures venues named under <a href="#evidence">Evidence</a>. Optional keys add CoinMarketCap Pro, CoinGecko Demo or Pro, Mobula unlocks, AlphaDrops, and Whale Alert.

## 🗺️ Status

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Railway, PostgreSQL, provider adapters, Binance, CMC Top 20, health and fail-safe | **Done** |
| 2 | Historical data, indicators, structure, regime, quantitative signal and risk engine | **Done** |
| 3 | On-demand controls, dashboard UI, charts, live updates | **Done** |
| 4 | Portfolio, risk, allocation, DCA, P/L scenarios | **Done** |
| 5 | News, sentiment, whale / on-chain | **Done** |
| 6 | Scalp signals, track record, emergency stop | **Done** (price alerts: not yet) |
| 7 | OpenAI reasoning layer | **Done**. AI chat, and an AI review that can only lower a signal |
| 8 | Signal tracking, backtesting, statistics | **Done**. Track record, per-coin backtests, Strategy lab |
| 9 | Statistical prediction | **Done**. A trade filter used only after it validates on newer data |
| 10 | Evidence engine | **Done**. Futures positioning, liquidations, an estimated liquidation map, order flow, news, hype, market trend. Held-back setups are tracked and learned from |
| 11 | Strategy library | **Done**. Eight published strategies with their own exits, walk-forward research on every scan, a 1-day horizon, and "why no buy" |
| 12 | Futures signals | **Done**. Long and short on USDT perpetuals, both sides researched with futures costs, a side-aware evidence board, and a leverage plan |
| 13 | Signova AI analyst | **Done**. One coin at a time. The model reads the full dossier and decides long, short, or no trade. An independent risk-manager pass can only lower it. Plans are checked, sized, stored, and tracked |

## 🪜 Decision hierarchy

```text
data integrity → fail-safe validation → deterministic calculations → risk engine
→ quantitative signal engine → evidence board (can only lower)
→ statistical / ML evidence → OpenAI reasoning → final deterministic validation → dashboard
```

Phase 1 built the first two layers. Phase 2 adds the deterministic calculations, the risk engine, the quantitative signal engine, and the final validation. Later phases insert evidence, statistics, and OpenAI **above** those checks, never through them.

## 📡 Data sources

All of these work without a key. A key, where one exists, raises limits or unlocks a paid endpoint. It is never required for the public fallback.

| Purpose | Primary | Fallbacks |
| --- | --- | --- |
| Top 20 ranking, reference prices | CoinMarketCap Pro (`CMC_API_KEY`) | CMC keyless API, CoinGecko, CoinPaprika |
| Spot price, OHLCV, order book | Binance public Spot REST | Kraken public REST |
| Live stream (on demand) | Binance WebSocket | Alternate Binance stream host |
| Total market cap, BTC dominance | CoinMarketCap | CoinGecko |
| Fear & Greed | CoinMarketCap index | alternative.me (labelled, a different index) |
| Altcoin Season Index | CoinMarketCap | None. Shown as unavailable |
| Candle-history cross-check | CoinMarketCap OHLCV (Startup plan or higher) | None. Shown as not verified |

Every value in the API carries its source. When a source fails, a supported fallback is used and labelled, or the value is shown as unavailable. Nothing is silently substituted. If no exchange price is available, `price` is `null` with the reason, never the reference price.

**CoinGecko (optional).** Set `COINGECKO_API_KEY` to a free Demo key from the [CoinGecko developer dashboard](https://www.coingecko.com/en/developers/dashboard). It is sent as `x-cg-demo-api-key` to `https://api.coingecko.com/api/v3`, which raises the rate limit to about 30 calls a minute for the listing fallback, market-cap fallback, and trending coins. For a paid key also set `COINGECKO_PLAN=pro` (`pro-api.coingecko.com` and `x-cg-pro-api-key`).

**CoinPaprika** uses the free plan at `https://api.coinpaprika.com/v1`, with no key. `/tickers` lists every coin (several MB), so it gets `COINPAPRIKA_TIMEOUT_SECONDS` (30) instead of the default 10 seconds. The shorter timeout is what made that call fail before. It is called only when CoinMarketCap and CoinGecko both fail.

<details>
<summary><b>CoinMarketCap key, credits, and error codes</b></summary>

<br/>

Set `CMC_API_KEY` as a Railway variable. Never commit it. If an endpoint is outside your plan (error 1006, for example OHLCV history on lower plans), the app skips it for a day and shows a note on the provider card, not a provider error.

With a key the app:

1. **Detects the plan** from `/v1/key/info` (no credits): monthly credits, reset date, per-minute limit, and live usage. The Pro request rate is tuned to 80% of the per-minute limit.
2. **Paces credits across the month.** A token bucket spreads the remaining credits evenly until the monthly reset, keeps a 5% reserve, and respects CoinMarketCap's daily limit. Fail-safe data (Top 20 listing, reference prices) always has priority over extra verification.
3. **Never goes dark.** When Pro cannot serve a request (credit pacing, a limit, an outage, a rejected key), the official keyless public API serves it instead. `/api/market` shows `listing_access: pro | keyless`. `/api/provider-health` shows the plan and credit usage.
4. **Handles CoinMarketCap error codes.** An endpoint outside the plan (1006) is remembered for a day instead of being retried. A rejected or unpaid key (1001 to 1007) switches to keyless and is reported. Minute, daily, monthly, and IP limits (1008 to 1011) each get their own back-off.
5. **Cross-checks candle history.** On Startup plans and higher, closed 1H and 1D exchange candles are compared with CoinMarketCap's aggregated candles for the same UTC periods (USDT converted to USD). Conflicting history blocks signals (`DATA_CONFLICT`). On lower plans the check is shown as not verified and does not block.

| Feature | Basic / Builder | Startup and higher |
| --- | --- | --- |
| Pro Top 20 listing, reference prices, global metrics, Fear & Greed, Altcoin Season | yes | yes |
| Plan detection, credit pacing, keyless fallback | yes | yes |
| 1H and 1D candle-history cross-check | not in the plan | yes |

Credits are spent on demand. With the dashboard open all day, the core feeds use at most about 2,300 credits per day (listing once a minute, context every 5 minutes). Pacing keeps usage inside the plan in all cases.

</details>

## 🌌 Universe

1. Rank the listing by market cap.
2. Always exclude stablecoins: provider tags, a curated list, and a $1-peg check for USD-named tokens, so one missing tag cannot let a stablecoin through.
3. Exclude wrapped, bridged, liquid-staking, and commodity-pegged tokens by default (`EXCLUDE_WRAPPED_ASSETS=true`). They only mirror another asset.
4. Take the first 20. Map each to a Binance USDT pair, otherwise a Kraken USD pair.
5. An asset with no supported spot market stays in the list, marked unsupported, and can never produce a signal.

## 🛡️ Fail-safe layer

The integrity gate runs before any analysis and **fails closed**. Every stage is evaluated so all reasons are shown, but a single failure means `NO TRADE`.

| Stage | Checks |
| --- | --- |
| `DATA_HEALTH_CHECK` | Signals paused, market source available, provider not DOWN or RESTRICTED |
| `MARKET_DATA_CHECK` | Live ticker present, valid price, bid below ask, 24h high above low |
| `TIMESTAMP_CHECK` | Ticker age within limit, no future timestamps (clock skew) |
| `CANDLE_COMPLETENESS_CHECK` | Per timeframe: closed candles only, alignment, OHLC sanity, duplicates, gaps in the recent window, staleness, enough history |
| `SOURCE_CONSISTENCY_CHECK` | Exchange price vs an independent reference (USDT converted to USD), 1H/1D history vs CoinMarketCap OHLCV on Startup plans and higher, abnormal candles must be corroborated |
| `VOLATILITY_CHECK` | Realized 5m volatility vs its normal level, plus an absolute move limit |

States: `HEALTHY`, `DEGRADED`, `STALE_DATA`, `DATA_CONFLICT`, `API_FAILURE`, `EXTREME_VOLATILITY`, `SIGNAL_PAUSED`.

Other protections: retries with exponential backoff and jitter, `Retry-After` handling for 429 and 418, per-provider circuit breakers, Binance request-weight tracking, HTTP 451 and 403 detection with automatic endpoint failover, a WebSocket heartbeat, a stale-stream watchdog, reconnect with resubscribe, and proactive renewal before Binance's 24-hour limit.

A reference price older than `REFERENCE_MAX_AGE_SECONDS` is never used as current. The check becomes `UNVERIFIED`, which blocks signals while `REQUIRE_PRICE_CROSS_VALIDATION=true`.

The data health score (0 to 100) weights freshness 30%, completeness 30%, cross-source consistency 25%, and provider health 15%. It is informational. The gate decision is stricter.

## 📈 Spot signals

Spot long only. The engine is deterministic, and every point of the score and every limit is explained in the API and on the dashboard.

**Engine audit (quant-2.1.0).** Indicator maths was re-verified: Wilder RSI, ATR, and ADX, plus EMA, MACD, Bollinger, and OBV against reference values. Swing points are recorded only once confirmed, so they do not repaint. Three rule checks were added on top:

- **Entry trigger** (cap at `WATCH`): at least 2 of 3 on 1H (close above EMA20, MACD histogram rising, RSI rising). A pullback is not bought while 1H momentum is still falling.
- **Bitcoin 4H trend** (cap at `WATCH`, altcoins only): when Bitcoin's 4H trend is down, altcoin buys wait.
- **Order book pressure** (no `STRONG BUY`): sellers stacked in the book (imbalance −0.25 or lower within 1%) block a `STRONG BUY`.

**Strategy.** Multi-timeframe trend following with pullback entries. 1D sets the primary trend, 4H is the setup, 1H confirms momentum, and 15m times the entry.

```text
integrity gate (6 stages) → ANALYSIS_CHECK → score → trade plan
→ RISK_CHECK (block / cap / downgrade) → market regime cap → FINAL_VALIDATION → label
```

The label can only be lowered after scoring, never raised. A failed data stage, a failed analysis check, a blocking risk check, or a final-validation violation is always `NO TRADE`.

**Score (0 to 100)**

| Factor | Points | Looks at |
| --- | --- | --- |
| Trend alignment | 30 | EMA20/50/200 alignment and swing structure on 1D, 4H, and 1H; 4H ADX and +DI/−DI |
| Momentum | 20 | RSI(14) zones, and MACD histogram level and direction, on 4H and 1H |
| Market structure | 15 | 4H higher highs and lows, the latest break of structure, nearby support, room to resistance |
| Entry location | 15 | Distance from the 4H EMA20 in ATRs, Bollinger %B, 15m RSI |
| Volume | 10 | 4H on-balance volume trend, buying versus selling volume on 4H and 1H |
| Market context | 10 | Market regime, 20-day performance versus Bitcoin |

**Labels.** `STRONG BUY` from 80, `BUY` from 65, `WATCH` from 50, otherwise `NO TRADE` (thresholds are configurable), then limited by the risk checks and the market regime. `WATCH` means a setup exists but not now. It carries a non-actionable watch plan and the reason, for example "wait for a breakout above X" or "extended: wait for a pullback".

**Trade plan** (quote currency, spot):

- Entry zone from the live price down to 0.5 ATR (4H) below it. Reward:risk is measured from the top of the zone, the least favourable fill.
- Stop 0.25 ATR below the most recent 4H swing low under the entry zone, kept between 1 and 3 ATR from the entry (2 ATR without a swing low).
- TP1 is the nearest overhead resistance (4H and 1D swing clusters), however close. TP2 and TP3 are the next levels. Without resistance (price discovery), targets are labelled 1.5R / 3R / 4.5R projections. Suggested scale-out is 40% / 35% / 25%.
- Reward:risk is net of round-trip fees and slippage (0.1% + 0.05% per side by default), measured at TP2.
- Size is the portfolio share that loses at most `RISK_MAX_PER_SIGNAL_PCT` (1%) if the stop is hit, capped at `RISK_MAX_ALLOCATION_PCT` (10%).

**Risk checks**

| Effect | Checks |
| --- | --- |
| Block (`NO TRADE`) | Order book missing or invalid, spread above 30 bps, less than $25,000 depth within 1% on either side, 24h volume below $5M |
| Cap (at most `WATCH`) | 1D or 4H trend down, net reward:risk below 1.5R, nearest resistance closer than 0.75R, stop wider than 15%, price more than 2.5 ATR above the 4H EMA20, RSI above 78 (4H) or 80 (1D), 24h move above +25%, bear or unknown market regime, no 1H entry trigger, Bitcoin 4H trend down (altcoins) |
| Downgrade (at most `BUY`) | 1D and 4H not both up, net reward:risk below 2R, resistance closer than 1R, 15m RSI above 85, 4H volatility at the 95th percentile, neutral market regime, sellers dominating the order book |

**Market regime.** Bitcoin's daily trend (EMA50/EMA200 and slope) plus breadth, the share of the universe trading above its daily EMA50. `BULL` allows every label. `NEUTRAL` allows at most `BUY`. `BEAR` and `UNKNOWN` (Bitcoin data unavailable) allow at most `WATCH`. Fear & Greed extremes and high Bitcoin volatility are reported as risks.

**Indicators** are pure Python, computed on validated closed candles only: Wilder RSI, ATR, and ADX, EMA seeded with its SMA, population-deviation Bollinger Bands, MACD, OBV, and ROC. They are tested against the published StockCharts RSI example and were cross-checked with the `ta` library during development. `ta` is not a runtime dependency. 4H and 1D use 1,000 candles so the EMA200 is fully warmed up.

**History.** Every analysis stores its 1H/4H/1D indicator snapshot once per closed candle (`technical_features`). A signal row and its targets are stored when the label changes or a new 4H setup candle closes (`signals`, `signal_targets`, including the full inputs and outputs for later backtesting). The market regime is stored at most hourly, or on change (`market_regimes`).

**Provider usage.** Scans run only on request. A scan analyses the universe with at most 4 coins in parallel (about 350 Binance request weight of the 6,000 per-minute limit). CoinMarketCap candle cross-checks stay cached per asset (30 minutes for 1H, 6 hours for 1D) under the credit pacing above.

## ⏱️ Scalp signals

The Scalp signals card finds short-term spot longs for a horizon you pick, on the coins in "Coins to analyse". Press **Find scalps**. Nothing runs in the background.

| Horizon | Setup candles | Trend must be up | Must not be down | Time limit |
| --- | --- | --- | --- | --- |
| 15 min | 5m | 15m | 1H | 30 minutes |
| 1 hour | 15m | 1H | 4H | 2 hours |
| 4 hours | 1H | 4H | 1D | 8 hours |
| 1 day | 4H | 1D | 1D | 2 days |

**Setups** (long only). A *pullback* needs an uptrend, a dip to the EMA20, RSI resetting to 52 or lower, then a bullish candle that closes above the previous high. A *breakout* needs a close above the 20-candle high on 1.8× average volume after a quiet period, and the coin must not be overbought. Both also need Bitcoin's trend not down (for altcoins), the price above today's VWAP (15 min and 1 hour), room to the next swing high, and a stop at least 2.5× the round-trip costs away. With 0.1% fees, a 15-minute scalp on Bitcoin rarely has enough room. The engine says "move too small for fees" instead of pretending.

**Plan.** Entry at the signal candle's close (buy zone up to 0.25R above). Stop under the recent low (0.8 to 2 ATR). TP1 at 1R, or just under a closer swing high, for half the position, then the stop moves to break-even. TP2 at 2R, plus a time exit. Size risks 1% of the portfolio (your risk settings).

**Backtest evidence gate.** Before a coin gets a label, the same rules are replayed over its recent history: 4,000 setup candles (about 14 days for 15 min, 41 days for 1 hour, 166 days for 4 hours). One trade at a time. Entries at the signal close. When a candle hits both the stop and a target, the stop is counted first. Results are net of fees and slippage.

| Label | Requirement |
| --- | --- |
| `STRONG BUY` | At least 25 trades, expectancy +0.25R or better, profit factor 1.5 or higher, win rate 50% or higher |
| `BUY` | At least 15 trades, expectancy +0.1R or better, and profit factor 1.2 or higher. A coin with too few trades can still reach `BUY`, and no higher, if the same rules across all scanned coins pass on 40 or more trades |
| `NO TRADE` | The rules lost money on this coin, or across the coins |
| `WATCH` | Not enough evidence, the setup type alone lost, the price already ran more than 0.25R above the entry, or the trend is right but no trigger has printed |

Live checks the history cannot contain still apply: spread, depth, 24h volume, order-book pressure, price versus the stop and TP1, and the entry window (2 candles). A test verifies the engine never uses future data: every setup found in the full history is identical when the history ends at that candle.

**Waiting coins.** Most of the time a coin is in a usable trend but no trigger candle has closed. That row is `WATCH` with a status line ("waiting for a pullback trigger", "downtrend: no long setups") and, where the rules allow, a *conditional plan*: the trigger price, the stop, the targets, and what the coin's own backtest says it would get if the trigger fires ("would be BUY", "would stay WATCH: not enough evidence", "would be NO TRADE"). A conditional plan is not a signal. Wait for the candle to close beyond the trigger and scan again.

| Kind | When it appears |
| --- | --- |
| Pullback | Uptrend, price above the EMA20. Buy the first close above the last candle's high after a dip |
| Dip | A dip is under way. Same trigger once it turns |
| Breakout | A close above the 20-candle high on volume |

`POST /api/scalp/scan?horizon=1h` (token), `GET /api/scalp?horizon=1h`, `GET /api/scalp/{symbol}?horizon=4h` (token).

## 📚 Strategy library

A scalp label is earned. The rules must have made money after costs on the coins' own history. When they lost (for example −0.08R per trade across 18 coins at 4 hours), the honest answer is no buy. Phase 11 gives the engine more strategies to choose from, and lets your own market data decide which of them work.

Eight strategies, each written from its published rules, with its own stop and its own exit:

| Strategy | Source | Exit |
| --- | --- | --- |
| Trend pullback | L. Raschke's "Holy Grail" family | Half at 1R, rest at 2R |
| Volatility squeeze breakout | J. Bollinger's squeeze, M. Minervini's volatility contraction | Half at 1R, rest at 2R |
| Turtle 55-candle breakout | The Turtle Traders (R. Dennis, W. Eckhardt), R. Donchian | Trailing stop at the 20-candle low |
| EMA 9/21 momentum | Classic crossover with J. Welles Wilder's ADX | Half at 1.5R, trail the rest 3 ATR (C. LeBeau's chandelier) |
| Connors RSI(2) pullback | L. Connors and C. Alvarez, *Short Term Trading Strategies That Work* | Close above the 5-period average |
| Bollinger band reclaim | J. Bollinger, *Bollinger on Bollinger Bands* | Middle band |
| Inside-bar breakout | Price action (A. Brooks, N. Fuller) | Half at 1R, rest at 2.5R |
| Break of structure | Dow theory / market structure ("smart money concepts") | Half at 1.5R, trail the rest 3 ATR |

**Research on every scan.** Each strategy is backtested on every selected coin: one trade at a time, costs included, stop first when a candle hits both, gaps filled at the open. Each coin's history is split in time, the older 70% and the newer 30%. Pooled over the coins, a strategy is **validated** only with 60 or more older trades and 25 or more newer trades, at least +0.05R per trade on the older part, at least +0.02R on the newer part, and a t-statistic of 1.5 or more. Only a validated strategy can give a `BUY`. A coin on which it lost (8 or more trades) is excluded. `STRONG BUY` also needs +0.15R on the newer part and a strong record on the coin itself. When several strategies fire on one coin, the best-validated one is shown ("+1 more" lists the others).

**Why no buy** sits under the scan. It says which strategies are validated, which setups fired but were held back and why (price already ran, lost on this coin, a filter), or that nothing made money at this horizon, and what to try: another horizon, more coins, or lower fees. The **Strategy research** table shows every strategy's result on the older and newer data: validated strategies first, then those with enough trades to judge, then the ones closest to enough trades. A few lucky trades never top the table. The **1-day horizon** (4H candles, 1D trend, up to 2 days per trade) pays much less in fees relative to the move.

Trailing and indicator exits are followed the same way by the track record. The exit rule is stored with each signal, and the plan tells you how to manage the trade ("trail: stop at the lowest low of the last 20 candles, raise it after each candle").

## 🤖 Signova AI analyst

The **Signova AI · single token** card at the top of the page spends OpenAI credits only when you ask, and only on one token. Pick the token and the horizon (15 minutes to 1 day) and press **Analyze token with AI**. One analysis returns both a **spot** signal (buy or no trade) and a **futures** signal (long, short, or no trade): a single AI call, plus one risk-manager call when it proposes a trade. That takes about a minute.

Multi-coin scans (Analyze now, the Scalp card, and the Futures card) use the rule engines only and never call OpenAI. The AI news reading before scans and the automatic AI review after scans are off. The per-signal **AI review** button still works on request.

**What the AI reads** (one JSON dossier per coin, about 15–25k tokens):

- 2–4 timeframes of candles (up to 90 per timeframe, with the taker-buy share of volume). For each: EMAs, RSI, MACD, ATR, Bollinger bands, ADX, volume ratios, the regime, swing highs and lows, the last structure break, and support and resistance with their touches.
- Price, 24h change and volume, spread, order-book depth and imbalance, perpetual mark and basis.
- Futures positioning: funding (now and the 7-day average), open-interest changes (1h to 72h), crowd and top-trader long/short ratios, taker flow, liquidations (1h, 4h, 24h, and the largest), and the estimated liquidation map.
- Every evidence-board factor (flow, relative strength, news, hype, unlocks, and the rest).
- The coin's headlines with the AI news reading, sentiment, scheduled unlocks, and market headlines.
- The market backdrop (Bitcoin trend and RSI, breadth, Fear & Greed, market-wide funding, altcoin season) and Bitcoin's own 4H and 1D charts.
- What the rule engines see on this coin, and their measured walk-forward research, when a Futures scan at this horizon ran in the last 3 hours. Press **Find futures trades** first to include it. That scan is free of OpenAI credits.

**How it decides.** One decision serves both markets. `LONG` is a spot buy and a futures long with the same levels. `SHORT` is a futures short only, and spot shows no trade. The prompt describes a top-down process (market, higher timeframe, setup, positioning and liquidity, news, the measured research) and a strict default: no trade is the usual answer, and conviction of 80 or more is only for the case where everything agrees. The plan needs a stop beyond the level that proves the idea wrong. The answer is enforced by a JSON schema (OpenAI Structured Outputs): decision, conviction, order type (market, limit, or stop), entry, stop, two targets, holding time, the estimated chance of TP1 before the stop, and reasoning per topic.

**Second opinion.** Every proposed trade goes back to the model as an independent risk manager. It looks for what is wrong: counter-trend, chasing, a stop in noise or in front of a liquidation cluster, targets beyond major levels, positioning or news against the idea. It can approve, reduce the conviction, or reject. It can never raise a trade.

**Checks, separate from opinions.** Signova never invents or flips a trade. It only verifies that the plan can be executed and is worth it: stop and targets on the right sides, entry reachable (a limit or stop order within 3 ATR), stop at least 2.5× the round-trip costs and 0.3 ATR and not wider than the horizon allows, net reward:risk at least 1.5 after costs, and conviction at least your minimum (60). A plan that fails is shown as `WATCH` with the reason. Futures plans get the Phase 12 leverage plan. Spot plans get a portfolio allocation.

**The model.** `auto` uses the best GPT model your key can use (the newest `gpt-N.M` flagship, for example gpt-5.1 before gpt-5, then o3, then GPT-4.1) and falls back to the next one if a model is unavailable. In **AI settings** you can pick any model your key lists, the thinking effort, the minimum conviction, and whether the risk manager runs.

**Measured, not trusted.** Every AI trade is stored (`ai_futures_4h`, `ai_spot_1h`, and the other horizons) and followed by the track record like every other signal. Market orders start at once. Limit and stop orders count only once the price fills them. An order that expires, or whose move leaves without a fill, is marked `NOFILL` and is not counted. After a few weeks the track record shows whether the AI beats the rule engines on your coins. An LLM can be wrong with confidence. Size small until it has a record.

`POST /api/ai/analyst/token?symbol=ETH&horizon=4h` (token), `GET /api/ai/analyst?symbol=ETH&horizon=4h`, `GET` and `PUT /api/ai/analyst/settings`, `GET /api/ai/analyst/models` (token).

## 📉 Futures signals

The **Futures signals** card finds long **and** short trades on USDT perpetuals for the coins you select, at 15 minutes, 1 hour, 4 hours, or 1 day. Analysis only. You place every order on your exchange.

**Shorts are proven the same way as longs.** For a short, the candles are mirrored: price becomes K / price, so highs become lows, a downtrend becomes an uptrend, and taker buying becomes taker selling. Every strategy in the library, its backtest, and its exits then describe the short exactly. A close above the 55-candle high on the mirror is a close below the 55-candle low on the real chart. A trailing stop under the mirror's lows is a stop above the real highs. Each of the 8 strategies is researched for both sides (16 pairs). A short can signal only after shorting with that strategy made money on the older 70% and the newer 30% of your coins' history, the same gates as Phase 11. The Turtle rule becomes a 55-candle breakdown. Connors RSI(2) sells a bounce above 90 below EMA200. The research table shows each short rule.

**Futures costs** in every backtest: taker fee 0.05% and slippage 0.02% per side (settings), plus funding at the usual 0.01% per 8 hours over the typical holding time.

**Labels:** `LONG`, `STRONG LONG`, `SHORT`, `STRONG SHORT`, `WATCH`, or `NO TRADE`. When a long and a short both qualify on one coin, the answer is `WATCH` (no clear direction). A coin with no USDT perpetual on Binance, Bybit, OKX, or Hyperliquid is `NO TRADE`. Signals use the spot price, which is the index the perpetual's mark price follows.

**Evidence board for shorts.** The same factors, read for the side you trade. Longs paying funding, price falling while open interest rises, crowded longs, taker selling, weakness against Bitcoin, and a risk-off market all count *for* a short. Short vetoes:

- Crowded, leveraged shorts: funding at −0.05% per 8 hours or lower, with open interest up 10% or more in 24 hours (squeeze risk).
- A short squeeze in progress.
- A major positive catalyst in the news.

The liquidation map is read for the short too. Short liquidations just above the stop can squeeze through it, and the plan suggests a stop above that zone. Long liquidations below the entry can fuel a drop toward the targets.

**The plan** comes with every signal and every held-back setup: entry zone, stop, targets, and the exit rule in real prices. It also includes a leverage plan.

- **Position size** equals your risk per trade (Portfolio, then Risk, 1% by default) divided by (stop distance + costs + funding). At the stop you lose the same share of equity at 1× or at 20×. Leverage only changes the margin you post.
- **Leverage** is the highest value up to your cap (5× by default, 20× at most) whose estimated isolated liquidation price stays beyond the stop. The gap must be at least half the stop distance, and at least 1%. The estimate is `entry × (1 − 1/L + MMR)` for a long and `entry × (1 + 1/L − MMR)` for a short, with a 1% maintenance margin (conservative for altcoins).
- **Funding** is shown per 8 hours, with what you pay or receive over the hold. Longs pay a positive rate. Shorts receive it.
- **Mark versus spot.** A basis of 0.3% or more is flagged.

**Track record.** Futures signals are followed like the scalps and appear as "Futures 15m / 1h / 4h / 1d". Shorts are followed on mirrored candles: a short wins when price falls to its targets and loses when it rises to its stop. Held-back setups are tracked in their own lane. The Phase 7 AI review also works on futures signals.

Leverage does not change a strategy's edge. It multiplies mistakes. The plan keeps the loss at the stop at your risk per trade, but gaps, exchange outages, and auto-deleveraging can fill a real order beyond the stop. Start with 1–3×. Change the cap in the card (**Max leverage**) or with `FUTURES_MAX_LEVERAGE`.

`POST /api/futures/scan?horizon=4h` (token), `GET /api/futures?horizon=4h`, `GET` and `PUT /api/futures/settings` (`PUT` needs the token).

<a id="evidence"></a>

## 🔎 Evidence engine

The chart setup and its backtest decide *whether* a coin can be a buy, and how strong. The evidence board then checks everything else that moves crypto prices, and can only hold a buy back. It never creates or raises one. Every swing signal and every scalp row shows its board: a score from −100 (strongly against) to +100 (strongly for), each factor with its value, what it means, where the data came from, and any veto.

| Group | Factors |
| --- | --- |
| Futures positioning | Funding rate (crowded longs pay a lot), open interest versus price (new money or short covering), the crowd's long/short ratio (contrarian at extremes), top traders ("whales") adding or cutting longs, futures taker buying, recent liquidations (a long flush that ended, or a cascade still running), the estimated liquidation map |
| Order flow | Spot taker buying on the signal candles, CVD divergence (price up while sellers hit the market), 24h volume versus its 20-day average, order-book imbalance, strength versus Bitcoin over 7 days |
| News and hype | Critical headlines (hack, exploit, delisting, insolvency, halted withdrawals, charges), headline tone or the AI's reading of the headlines, catalysts (major-exchange listing, ETF, mainnet, integration), hype (headline count versus the coin's usual, CoinGecko trending) with a warning when hype meets a stretched price |
| Market | Bitcoin's 4H and 1D trend (for altcoins), market breadth, leverage across the market (average funding, crowded coins), Fear & Greed extremes, stablecoin supply, altcoin season |
| Events | Large token unlocks, whale exchange inflows and outflows (BTC and ETH on-chain) |

**Futures data** is public and keyless: Binance futures first, then Bybit, OKX, and Hyperliquid for whatever a blocked or missing exchange cannot answer. Many cloud regions are refused by Binance or Bybit. An exchange that refuses the region is skipped for an hour. Liquidation orders come from OKX, a sample of the market, and are labelled as such. Provider health lists each exchange. The Evidence engine card's *Load futures positioning* shows the market-wide view.

**Liquidation map** (an estimate, labelled everywhere). For every hour open interest rose in the last 7 days, new positions are treated as opened near that hour's price. They are split into longs and shorts by the long/short ratio and spread over typical leverage (5× to 100×). Later candles that traded through a level remove it. Long liquidations below the price are forced selling if price falls there. Short liquidations above are forced buying if price rises there. The board says when more fuel sits above than below, and when a cluster of long liquidations sits at your stop it suggests a stop just under that zone.

**How it changes a signal.** Mode `filter` is the default. `advisory` only shows the board. `off` disables it.

- A veto caps the buy at `WATCH`: critical news about the coin, a large unlock within 3 days, a long-liquidation cascade still running, or extreme funding while open interest jumped.
- A board at −25 or lower, with 4 or more factors that have data, caps the buy at `WATCH`.
- A board at −8 or lower turns `STRONG BUY` into `BUY`.

**AI in the engine.** With an OpenAI key, one request per new set of headlines reads them all, rates each coin's news from −2 to +2, and flags critical events. The reading is cached for 45 minutes. The switch is on the Evidence engine card. The AI can clear a keyword false alarm (a hack of a *different* project) but its reading never raises a signal. The Phase 7 reviewer also sees the evidence board.

**Learning from outcomes.** A setup the board, the learned model, or the statistical filter holds back is stored as `FILTERED` and followed exactly like a shown buy, in its own lane. The track record then shows what the held-back setups did (a filter helps when they did worse) and results by board grade. The Evidence engine card shows each factor's measured edge: the average result when it argued for the trade versus against it. Once 90 setups with a board have closed, a logistic model is trained on the older 70% and validated on the newer 30%, the same rules as Phase 9. Only a validated model joins the filter. It can only lower a buy, and you can switch it off. Until then the documented prior weights are used, and nothing is claimed.

What it cannot do: there is no free source for social-media volume, real per-trader leverage, or exchange order flow beyond these APIs. The liquidation map is an estimate. None of it guarantees a win rate. It removes trades with visible headwinds and measures itself.

`GET /api/evidence/settings`, `PUT` with a body such as `{"mode": "filter", "refresh_news": true, "ai_news": true}` (token), `GET /api/evidence/{symbol}?horizon=swing`, `GET /api/evidence/learning`, `POST /api/evidence/learning/refresh`, `PUT /api/evidence/learning` with `{"enabled": false}` (token), `GET /api/derivatives/market`, `GET /api/derivatives/{symbol}`.

## 🧪 Strategy lab

The published scalp rules are a starting point. The Strategy lab card tests alternatives on your coins' real history and says, honestly, whether any of them is better. Pick a horizon and press **Run the lab** (token). It runs in the background, takes a few minutes, and uses the same candles as a scalp scan. No extra credits.

- **Grid.** 8 entry filters (published rules, volume-confirmed pullbacks, ADX 20 or higher, stop at least 3.5× costs away, pullbacks only, breakouts only, buyers in control at the trigger with taker buying 52% or more of the last 3 candles' volume, coin stronger than Bitcoin) times 6 exit styles (targets 1R/2R, 1R/3R, 1.5R/3R, no break-even, all at 1.5R, double time limit) is 48 variants. The grid is small on purpose. The more variants one tries, the more likely the best-looking one is luck.
- **Walk-forward split.** Each coin's history is split in time. The older 70% chooses the variant. The newer 30%, never used for choosing, tests it.
- **Acceptance.** The variant is applied to the scalp engine only if it earned at least +0.05R per trade on the older data, made at least 20 trades and a profit on the newer data, and did at least as well there as the published rules. Otherwise the lab says why and the published rules stay. You can apply any variant by hand or reset to the published rules.
- **Statistics shown.** Trades, win rate, expectancy (R), profit factor, total R, and max drawdown for all, older, and newer data. The equity curve in R with the split marked. Results by setup type, time of day (UTC), weekday, coin, and outcome (stop, TP1, TP2, time).

The applied variant and the last result are stored in `app_settings` and survive restarts.

`GET /api/lab?horizon=1h`, `POST /api/lab/run`, `POST /api/lab/apply` with `{"filter": "adx", "exit": "x3"}`, `POST /api/lab/reset` (token).

## 📐 Statistical trade filter

Each lab run also trains a small logistic-regression model on the chosen variant's trades. It estimates the chance that a setup ends as a winner, net of costs, from 19 numbers known when the signal candle closes: trend strength (ADX, distance from the EMAs, higher-timeframe RSI and momentum), volatility (ATR, Bollinger width), volume, RSI, distance to the 20-candle high, Bitcoin's momentum, order flow (taker buying, net volume delta), the stop distance versus costs, the setup type, and the hour of day. It is pure Python. No machine-learning library, and little memory.

It is honest by construction. It is trained on the older trades and judged only on the newer ones. Its probability cut is chosen on the older trades. It counts as **validated** only if, on the newer trades, it separates winners from losers (AUC 0.55 or more) and the trades it keeps earned clearly more (+0.05R) than all trades. Only a validated model is used, and it can only lower a scalp label to `WATCH` when the probability is below the cut. It never raises one. An unvalidated model is shown with its numbers and reasons and does nothing. The lab card has a switch to turn a validated model off. Each prediction is stored in `model_predictions`.

`GET /api/ml?horizon=1h`, `PUT /api/ml?horizon=1h` with `{"enabled": false}` (token).

## 🧠 AI review

Press **AI review** on a scalp, swing, or futures signal. An OpenAI model receives the engine's full plan and evidence (levels, backtest, conditions, risks) and answers in JSON with a verdict of *agree*, *caution*, or *reject*, the concrete risks it sees, and a short summary. The verdict shows next to the signal and is stored with it (`signals.ai_output`). The track record groups results by verdict, so after a few weeks you can see whether the objections were worth anything.

| Mode | Effect |
| --- | --- |
| Advisory | Default. The verdict is shown. The label is unchanged. |
| Filter | A *reject* caps `BUY` and `STRONG BUY` at `WATCH`. The AI can only lower a signal, and never overrides the data checks. |
| Auto-review | Off by default. After each scan, the first new buy signals are reviewed automatically. The default count is 3. `auto_max` is 1 to 10 via the API. Each review is one OpenAI call. |

Settings live in the AI assistant card and in `app_settings`.

`POST /api/ai/review` with `{"kind": "scalp", "symbol": "SOL", "horizon": "1h"}` (token). `GET` and `PUT /api/ai/settings` with `{"mode": "filter", "auto": true, "auto_max": 3}` (`PUT` needs the token).

## 🛑 Emergency stop

The red **Emergency stop** button in the header halts everything at once: the running scan (coins in progress may finish for up to 20 seconds, then it is cancelled), Auto-analyze, live prices, scalp and futures scans, the Strategy lab, and AI reviews. While it is engaged the server refuses every request that would reach a provider, a news source, or OpenAI (HTTP 503), so no credits are spent, and the dashboard's auto-refresh stops. Stored signals, the track record, the last scalp scan, and the lab results stay readable. You can still edit the watchlist and the coin selection. The stop survives restarts. **Resume**, in the red banner, releases it. Nothing restarts on its own.

`POST /api/control/kill` with an optional `{"reason": "..."}`, and `POST /api/control/resume` (token).

## 📒 Track record

Every `BUY` and `STRONG BUY` (swing and scalp), and every futures `LONG` and `SHORT`, is followed on the candles after it with the same pessimistic simulator as the backtest: stop first, half at TP1 then break-even, targets, and a time limit (14 days for swing signals). A signal that appears while an earlier one on the same coin and strategy is still running is marked `SKIPPED`, so one move is never counted twice. It uses candles the analysis already fetched, so it costs no extra API calls. The card shows win rate, average R, total R, and profit factor per strategy for the last 90 days (`GET /api/performance?days=90`).

This is the honest measure of accuracy. Unlike a backtest, nothing in it was known when the rules were written. It also groups results by evidence-board grade and shows, separately, what the setups held back by a filter would have made.

## 💬 About accuracy

No indicator set can promise a win rate, and nothing here does. The dashboard refuses trades whose own history says they lose, shows the measured record of every setup, and sizes every trade so a stop costs about 1% of the portfolio.

Some arithmetic for a daily dollar target: with a measured expectancy of +0.2R and 1% risk per trade, each trade earns on average 0.2% of the portfolio. Fifty dollars a day then needs about $25,000 per trade-a-day (for example $5,000 and five good trades every day), and results vary a lot from day to day. Raising risk per trade to reach a target faster is the usual way accounts are lost. Start with small sizes, watch the track record for a few weeks, and trust only what it shows.

More inputs do not automatically mean more accuracy. Every extra factor can also add noise. That is why the evidence board only ever holds trades back, why the setups it holds back are tracked, and why its learned model must prove itself on newer outcomes before it filters anything. Read the "By evidence board" and "Held back by filters" tables after a few weeks. They tell you whether the extra data is helping on your coins.

## 🎛️ Manual control

Nothing analyses in the background by default, so no provider calls or CoinMarketCap credits are spent while you are away from the dashboard.

| Control | Behaviour |
| --- | --- |
| **Analyze now** | Scans the Top 20 plus your watchlist once. Progress shows in the control bar. |
| **Stop** | Cancels a running scan, switches Auto-analyze off, and stops live prices. |
| **Auto-analyze** | Off by default. Re-scans every 15 minutes to 4 hours until you stop it. `AUTO_ANALYZE_MINUTES` sets a schedule at startup (`0` means off). |
| **Live prices** | Streams Binance mini-tickers over WebSocket. No REST calls and no credits. Prices update in place until you stop them. |
| **Refresh data** | Reloads market data once. **Auto-refresh** (off by default) reloads it every 30 seconds. |
| `GET /api/signals` | Returns the latest completed scan. It never starts one. |

**Coins to analyse** sits under the control bar. Pick exactly which coins a scan covers, for example five of them (Top 5, Top 10, Select all, Clear, or click coins), then save. Analyze now, Auto-analyze, and live prices then use only those coins. The market table still lists every coin. Unselected ones are dimmed. Coins you add to the watchlist join the selection. The selection is stored in `app_settings` (migration `0003`). `GET` and `PUT /api/control/selection`.

**Watchlist.** Add any coin by symbol. It joins the next scan, marked "watchlist", and runs through the full pipeline. Remove it with **Remove** in the Watchlist card, or **Remove from watchlist** under its name in the market table. Removing it also takes it out of "Coins to analyse". It is priced from the 200-coin listing (still one CoinMarketCap credit), or from CoinMarketCap quotes when it ranks lower. Stored in the `watchlist` table (migration `0002`).

**Charts.** The coin panel shows a candlestick chart (15m, 1H, 4H, 1D, closed candles) with EMA20, EMA50, the entry zone, the stop, the targets, and a hover readout.

## 💼 Portfolio and risk

Record cash and positions by hand. Nothing is traded. The portfolio shows value, P/L, allocation, total exposure, and warnings (exposure above your limit, concentration), plus P/L scenarios per position (−20% to +20%, and the coin's signal stop and targets).

**Risk settings** (max loss per trade, max position size, first-buy and DCA sizes, max total exposure, fees, slippage) are stored in `risk_settings` and applied to the signal engine immediately. Every signal's suggested size and net reward:risk follow them. Dashboard values take precedence over the environment defaults.

**Plan trade** takes a coin and an optional budget and returns, from its current plan: the risk-based budget, a three-step DCA ladder across the entry zone, the average entry, the loss at the stop as a share of equity, and P/L after costs for "stop hit", "TP1 then stop at entry", "TP1 + TP2", and "all targets".

## 🗞️ Assistant and news

**AI assistant** (homepage, `POST /api/chat`). Chat with an OpenAI model about the next trade. Each question carries the dashboard's own data: the latest scan and market regime, news headlines, market mood, on-chain data, your portfolio and risk settings, and, if you pick a coin or press "Ask AI about this coin", that coin's full analysis. The system prompt makes the engine authoritative. The assistant explains and discusses. It cannot turn a `NO TRADE` or `WATCH` into a buy.

Models: `OPENAI_ANALYSIS_MODEL` (default `gpt-5-mini`), falling back to `OPENAI_FALLBACK_MODEL` (default `gpt-4o-mini`) if the first is unavailable. Rate limited to `CHAT_RATE_LIMIT_PER_MINUTE` (10).

**Model picker.** The chat card lists `CHAT_MODEL_OPTIONS`. Models your key cannot use are greyed out, checked against OpenAI's model list. There is a reasoning-effort setting for GPT-5 and o-series models. Your choice is remembered in the browser. The reply shows which model answered.

GPT-5 models reason before they answer, and the reasoning counts against the output limit. An output limit of 1,500 tokens was often used up by reasoning alone, which produced "The model returned no text". Reasoning models now get `CHAT_REASONING_BUDGET_TOKENS` (6000) on top of `CHAT_MAX_OUTPUT_TOKENS`, and `CHAT_REASONING_EFFORT=low` by default. If a model still returns nothing, the next model answers instead and the reply says so.

**News** (`GET /api/news`, cached 15 minutes). Fetched only when you press Load or Refresh, or on page open if you tick "Load when the page opens". Nothing runs in the background. Sentiment and the chat only read headlines already loaded.

Sources, with no keys: RSS from CoinDesk, Cointelegraph, Decrypt, and Bitcoin Magazine, CryptoCompare's free news API, and CoinGecko trending coins. Headlines are de-duplicated, tagged with the coins they mention, given a keyword-based tone (labelled as such), and stored in the `news` table. A source that fails is listed, never guessed. News is context for you and the assistant. It is not an input to the quantitative signal score. The evidence board reads headlines separately, and can only hold a trade back.

## 🌊 Sentiment and on-chain

**Market mood** (`GET /api/sentiment`, cached 10 minutes). Three transparent components, each scaled from −1 (fear) to +1 (greed) and combined with fixed weights.

| Component | Source | Weight |
| --- | --- | --- |
| Fear & Greed now versus 50, with 7-day and 30-day averages and the 7-day trend | alternative.me | 0.5 |
| News tone of the last 48 hours (keyword method) | The news sources above | 0.3 |
| Average perpetual funding versus the 0.01% / 8h baseline | Binance futures `premiumIndex` (public) | 0.2 |

The score maps to `EXTREME FEAR`, `FEAR`, `NEUTRAL`, `GREED`, or `EXTREME GREED`. Stablecoin supply change (DefiLlama) is shown alongside as a liquidity indicator and is not weighted.

**Per coin:** headline tone, perpetual funding rate, and labelled exchange flows. Notes are raised for crowded longs (funding at or above 0.05% per 8h), crowded shorts (at or below −0.03%), mostly negative headlines, and large labelled exchange inflows. These notes join the signal's "Risks to keep in mind" as `sentiment: ...`. **Sentiment never changes a label or a score.** There is no measured evidence yet that these notes improve them. Later phases measure other factors on their own, and those can only lower a signal.

**On-chain** (`GET /api/onchain`, cached 10 minutes). All free and keyless, except the optional Whale Alert source.

| Data | Source |
| --- | --- |
| BTC fees, mempool size, hashrate, next difficulty adjustment | mempool.space |
| Large BTC transactions (at least `WHALE_MIN_BTC`, default 100 BTC) | blockchain.com unconfirmed transactions |
| ETH gas, network utilisation, large ETH transfers (at least `WHALE_MIN_ETH`, default 1000 ETH) | Blockscout. Public address names identify many exchange wallets |
| USD stablecoin supply and its 1d / 7d / 30d change | DefiLlama |
| Optional labelled whale transfers on major chains | Whale Alert (`WHALE_ALERT_API_KEY`) |

A transfer is called an exchange inflow or outflow only when a source labels one side as an exchange. Everything else is "unlabelled". Large transfers are stored in `whale_events`. Sentiment readings are stored in `sentiment`. Pressing Analyze now refreshes sentiment first, at most once a minute, so each scan carries current notes. Forced refreshes within `MIN_REFRESH_SECONDS` (60) of the last fetch are served from the cache.

## 🎁 Token unlocks and airdrops

`GET /api/events/unlocks` and `GET /api/events/airdrops` feed the "Token unlocks & airdrops" card. The card loads when you press Load, and the cache lasts `EVENTS_CACHE_SECONDS` (6 hours).

- **Token unlocks** use Mobula (`MOBULA_API_KEY`, a free key at [mobula.io](https://mobula.io)). Each analysed coin's release schedule includes the next unlock date, tokens, USD value, share of circulating supply, and who receives them. Unlocks of `UNLOCK_NOTE_MIN_PCT` (1%) of circulating supply or more, within `UNLOCK_NOTE_DAYS` (14), are added to that coin's signal risks ("token unlock in N days"). Like sentiment, this never changes a label or a score. With a key, each scan refreshes unlocks at most once per cache period.
- **Airdrops** use the AlphaDrops Developer API (`ALPHADROPS_API_KEY`, a paid subscription): active, claimable, and upcoming airdrops with chains, estimated reward, and end date. They are unverified third-party listings.
- **Tokenomist** has no free API, so it is not integrated.

Without a key, the card says which variable to set. The Mobula and AlphaDrops response formats were implemented from their published docs and are read defensively. Check the card once after adding a key.

## 🔐 Security

Set `ADMIN_TOKEN` on Railway. Then Analyze now, Stop, Auto-analyze, live prices, watchlist changes, the AI chat, AI reviews, scalp scans, the Strategy lab, evidence-engine settings, the emergency stop and resume, and the portfolio all require it. Those calls spend OpenAI credits or change state. The dashboard asks for the token once and keeps it in the browser. Public market data stays readable. Without `ADMIN_TOKEN`, anyone who finds the URL can use those controls.

<a id="getting-started"></a>

## 🚀 Getting started

### Deploy on Railway

1. Push this repository to GitHub and create a Railway project from it. Railway builds the `Dockerfile` (see `railway.json`).
2. Add a **PostgreSQL** service to the project.
3. In the app service **Variables**, set:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (a reference variable)
   - `OPENAI_API_KEY` = your key
   - `CMC_API_KEY` = your CoinMarketCap Pro key (optional)
   - `ENVIRONMENT` = `production`
4. **Region.** Binance answers HTTP 451 from restricted locations, including the United States. Choose a non-US region in the service settings. If Binance is still restricted, the app uses Kraken and labels the source. Check `/api/provider-health` after deploy.
5. Under **Networking**, generate a public domain and open it. The status dashboard loads. `/health` must return 200.
6. Open `/api/provider-health`. The `coinmarketcap` entry should show `"mode": "pro"` and your plan's credit limit. `"pro_disabled"` means the key was rejected. The reason is shown.

Migrations run automatically on start (`scripts/start.sh`, unless `RUN_MIGRATIONS=false`). The app runs as one worker. Phase 2 needs no new migration. It fills tables created by the initial schema.

### Local development

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export DATABASE_URL=postgresql://app:app@localhost:5432/cryptosig
alembic upgrade head
uvicorn app.main:app --reload
```

Copy `.env.example` to `backend/.env` for local variables. Never commit real keys.

### Tests

```bash
cd backend
pytest -q
# include the real PostgreSQL end-to-end test:
TEST_DATABASE_URL=postgresql://app:app@localhost:5432/cryptosig pytest -q
```

<details>
<summary><b>What the tests cover</b></summary>

<br/>

CoinMarketCap Pro (every error code, plan detection, credit pacing, keyless fallback, the key never sent to the keyless API or written to logs, v3 response shapes, OHLCV parsing, candle-history cross-checks and their effect on the gate), configuration and Railway URL handling, secret redaction, HTTP retry and backoff, `Retry-After`, circuit breaker, 451 and 403, Binance endpoint failover and weight limits, Kraken and listing parsers, listing fallback, stablecoin and derivative exclusion, candle validation (gaps, duplicates, invalid OHLC, misalignment, staleness, history, anomalies), cross-source checks, order book sanity, the volatility breaker, every integrity-gate stage, universe building, market snapshot states and failover, API endpoints and error mapping, persistence and upserts (SQLite and PostgreSQL), and the WebSocket manager (resubscribe, stale watchdog, renewal).

Phase 2: indicator math against published and library reference values, swing pivots, structure, change of character, level clustering, timeframe and market regimes, and the full engine on deterministic synthetic markets sampled into consistent 5m–1D candles (strong uptrend, downtrend, parabolic move, failed integrity gate, wide spread, bear and neutral regimes, missing history, unsupported asset, `STRONG BUY` downgrades), trade-plan math net of costs, sizing, final validation, and the Phase 2 endpoints with persistence and de-duplication (SQLite and PostgreSQL).

Phases 3–5: manual control, watchlist, news parsing, chat, portfolio, and the on-chain and sentiment parsers, scores, notes, endpoints, refresh guard, and persistence, with every free source mocked. Sentiment notes are checked to leave labels and scores unchanged.

Phases 6–9: scalp rules without lookahead, per-coin backtests, the track record, conditional plans, the emergency stop (allowlist, graceful stop, persistence), watchlist removal, the Strategy lab split and acceptance rule, the statistical filter (validation gates, no future data in its features, filtering only when validated), and AI review (JSON parsing, advisory and filter modes, auto-review, storage, and the by-verdict record), on SQLite and PostgreSQL.

Phase 10: every exchange parser against its documented payload (errors, 1000× contracts), exchange failover and region skipping, OKX USD open interest, the liquidation map (levels, crossed levels removed, long/short split), each evidence factor and veto, thin boards, learning (a predictive factor validates, random factors do not), held-back setups tracked in their own lane, and the swing and scalp integration.

Phase 11: trailing and exit-signal simulation, gap fills, every library strategy free of future data, one-trade-at-a-time backtests, the research gates (both parts, trade counts, and luck), verdicts, a validated strategy producing a `BUY` with its exit rule stored and followed by the track record, "why no buy", and the 1-day horizon.

Phase 12: the candle mirror (a downtrend becomes an uptrend), shorts found in a falling market and longs in a rising one, the side-aware evidence board and short liquidation zones, leverage plans (liquidation beyond the stop, size from the risk per trade), a validated short becoming a `SHORT` with its plan, stored and tracked to a win on mirrored candles, `NO TRADE` past the stop or without a perpetual, and the futures endpoints, settings, and emergency stop.

Phase 13: best-model choice from the key's model list, defensive parsing of the model's answer, every plan check (spot cannot short, stop too tight, reward too small, low conviction, price past the stop, limit orders), the risk manager only lowering, limit and stop entries counting once filled (`NOFILL` otherwise), and a single-token analysis (spot and futures from one call) against a fake OpenAI that answers from the dossier. Engine scans never call OpenAI. The tests also cover the dossier's content, Structured Outputs and reasoning effort in the request, storage without duplicates, a tracked win, settings, the model list, the emergency stop, and a missing key.

Fake adapters and synthetic markets exist only in `backend/tests`. Production code never generates data.

</details>

<a id="configuration"></a>

## ⚙️ Configuration

Every setting has a safe default. Override any of them as a Railway variable. Names only are listed here. Set the values in Railway or in `backend/.env`, never in Git.

Max loss per trade, max position size, fees, and slippage can also be managed under Portfolio, then Risk. Those database values take precedence over the environment defaults.

**Runtime**

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | local example `postgresql://app:app@localhost:5432/cryptosig` | PostgreSQL URL. Required in the container. On Railway, `${{Postgres.DATABASE_URL}}`. Railway-style URLs are normalized for asyncpg |
| `ENVIRONMENT` | `development` | Set `production` on Railway. Also accepts `test` |
| `LOG_LEVEL` | `INFO` | Uvicorn log level |
| `RUN_MIGRATIONS` | `true` | Set `false` to skip `alembic upgrade head` on start |
| `ADMIN_TOKEN` | unset | Protects controls, chat, and portfolio. Strongly recommended |
| `AUTO_ANALYZE_MINUTES` | `0` | Scan schedule at startup. `0` means manual only |

**Signals and risk**

| Variable | Default | Meaning |
| --- | --- | --- |
| `SIGNAL_MIN_SCORE_STRONG_BUY` / `_BUY` / `_WATCH` | 80 / 65 / 50 | Score thresholds |
| `RISK_MAX_PER_SIGNAL_PCT` | 1.0 | Portfolio percent lost if a stop is hit. Drives the suggested size |
| `RISK_MAX_ALLOCATION_PCT` | 10.0 | Largest suggested position, percent of the portfolio |
| `RISK_FEE_PCT` / `RISK_SLIPPAGE_PCT` | 0.1 / 0.05 | Per side, used for net reward:risk and sizing |
| `RISK_MIN_REWARD_RISK` / `RISK_STRONG_MIN_REWARD_RISK` | 1.5 / 2.0 | Net reward:risk at TP2 for `BUY` / `STRONG BUY` |
| `RISK_MIN_ROOM_R` / `RISK_STRONG_MIN_ROOM_R` | 0.75 / 1.0 | Room to the nearest resistance, in R |
| `RISK_MAX_STOP_PCT` | 15 | Widest stop allowed |
| `RISK_MAX_SPREAD_BPS` / `RISK_MIN_DEPTH_USD` / `RISK_MIN_VOLUME_24H_USD` | 30 / 25000 / 5000000 | Liquidity limits |
| `RISK_MAX_EXTENSION_ATR` / `RISK_MAX_CHANGE_24H_PCT` | 2.5 / 25 | Chasing limits |
| `SIGNAL_SCAN_CONCURRENCY` | 4 | Coins analysed in parallel during a scan |
| `SIGNAL_PERSIST_ENABLED` / `FEATURE_PERSIST_TIMEFRAMES` | true / `1h,4h,1d` | History storage |
| `REGIME_CACHE_SECONDS` | 600 | Market-regime refresh |
| `CANDLE_FETCH_LIMIT` / `CANDLE_FETCH_LIMIT_LONG` | 500 / 1000 | Candles per request (5m–1H / 4H–1D) |
| `EXCLUDE_WRAPPED_ASSETS` | true | Drop wrapped, bridged, liquid-staking, and commodity-pegged tokens |
| `REQUIRE_PRICE_CROSS_VALIDATION` | true | An unverified reference price blocks signals |
| `REFERENCE_MAX_AGE_SECONDS` | 300 | Oldest reference price that may still be used |

**Providers and chat**

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | unset | Required for chat, AI review, AI news, and the AI analyst |
| `OPENAI_ANALYSIS_MODEL` / `OPENAI_FALLBACK_MODEL` | `gpt-5-mini` / `gpt-4o-mini` | Chat models. An empty value uses these defaults |
| `CMC_API_KEY` | unset | Optional CoinMarketCap Pro key |
| `COINGECKO_API_KEY` / `COINGECKO_PLAN` | unset / `demo` | CoinGecko Demo (free) or Pro key |
| `COINPAPRIKA_TIMEOUT_SECONDS` | 30 | Timeout for the large CoinPaprika download |
| `CHAT_MAX_OUTPUT_TOKENS` | 1500 | Visible chat answer |
| `CHAT_REASONING_EFFORT` / `CHAT_REASONING_BUDGET_TOKENS` | `low` / 6000 | GPT-5 and o-series reasoning |
| `CHAT_RATE_LIMIT_PER_MINUTE` | 10 | Chat requests per minute |
| `CHAT_MODEL_OPTIONS` | gpt-5-mini, gpt-5, gpt-5-nano, gpt-4.1, gpt-4.1-mini, gpt-4o, gpt-4o-mini, o4-mini | Models in the chat picker |
| `NEWS_CACHE_SECONDS` / `NEWS_FEEDS` | 900 / four RSS feeds | News cadence and sources |

**On-chain, events, scalps, futures, and the AI analyst**

| Variable | Default | Meaning |
| --- | --- | --- |
| `SENTIMENT_CACHE_SECONDS` / `ONCHAIN_CACHE_SECONDS` | 600 / 600 | Phase 5 cadence |
| `MIN_REFRESH_SECONDS` | 60 | Forced refreshes inside this window are served from cache |
| `WHALE_MIN_BTC` / `WHALE_MIN_ETH` | 100 / 1000 | Smallest transfer listed as a whale |
| `WHALE_ALERT_API_KEY` / `WHALE_ALERT_MIN_USD` | unset / 1000000 | Optional Whale Alert source |
| `MOBULA_API_KEY` / `ALPHADROPS_API_KEY` | unset | Token unlocks / airdrops |
| `EVENTS_CACHE_SECONDS` / `UNLOCK_WINDOW_DAYS` | 21600 / 30 | Unlock and airdrop cadence, and the unlock window |
| `UNLOCK_NOTE_MIN_PCT` / `UNLOCK_NOTE_DAYS` | 1 / 14 | Unlock size and horizon that become a risk note |
| `SCALP_SLIPPAGE_PCT` / `SCALP_MIN_RISK_COST_MULTIPLE` / `SCALP_MIN_TRADES` | 0.02 / 2.5 / 15 | Scalp costs, fee filter, evidence minimum |
| `FUTURES_ENABLED` / `FUTURES_MAX_LEVERAGE` | true / 5 | Futures signals, and the leverage cap for plans (1–20) |
| `FUTURES_FEE_PCT` / `FUTURES_SLIPPAGE_PCT` / `FUTURES_MAINTENANCE_MARGIN_PCT` | 0.05 / 0.02 / 1.0 | Futures costs per side, and the maintenance margin used in the liquidation estimate |
| `AI_ANALYST_MODEL` / `AI_ANALYST_EFFORT` | `auto` / `high` | Analyst model (`auto` picks the best model on your key) and reasoning effort |
| `AI_ANALYST_REVIEW` / `AI_ANALYST_MIN_CONVICTION` / `AI_ANALYST_MIN_REWARD_RISK` / `AI_ANALYST_CONCURRENCY` | true / 60 / 1.5 / 2 | Risk-manager pass and plan checks. Concurrency is unused in single-token mode |

## 📺 Dashboard

Opening the service URL shows a page built on the API: the market regime, market context (total market cap, BTC dominance, Fear & Greed, Altcoin Season), the signals table (label, score, trend, entry zone, stop, TP1 and TP2, net reward:risk, suggested size, and the main reason), the Top 20 with live price, 24h change, price cross-check, and data state, and provider health including CoinMarketCap credit usage. Selecting an asset shows its signal, trade plan, reasons and risks, score breakdown, the full pipeline, every risk check, indicators and structure per timeframe, and the Phase 1 integrity details.

The page refreshes every 30 seconds only while the tab is visible, so a background tab spends no provider calls or credits. Interactive API docs are at `/api/docs`. Expensive endpoints are cached with single-flight loading, so many open tabs never multiply provider calls.

<a id="api"></a>

## 🔌 API

| Endpoint | Description |
| --- | --- |
| `GET /` | Status dashboard |
| `GET /api` | JSON index of the API |
| `GET /health` | Liveness, database check, processing state. Never calls external providers |
| `GET /api/system/state` | Processing state and aggregate data state, with reasons |
| `GET /api/market` | Top 20 with live prices, per-asset state and score, market context |
| `GET /api/assets` | Universe with market mapping |
| `GET /api/assets/{symbol}` | Full collection on 5 timeframes, order book, cross-check, volatility, integrity gate |
| `GET /api/assets/{symbol}/candles?timeframe=1H&limit=300` | Validated closed candles, plus the forming candle, separately |
| `GET /api/assets/{symbol}/analysis` | Indicators, structure, regimes, score factors, trade plan, risk checks, full pipeline |
| `GET /api/market/regime` | Market regime and the signal cap it applies |
| `GET /api/signals` | Latest completed scan, best first. Null until Analyze now has run |
| `GET /api/signals/history?symbol=BTC&limit=50` | Stored signals with their targets, newest first |
| `GET /api/provider-health` | Status, latency, errors, rate limits, and circuit state per provider |
| `GET /api/control/status` | Scan progress, schedule, live-price state, whether a token is required |
| `POST /api/control/analyze` · `/stop` · `/auto` · `/live/start` · `/live/stop` | Manual control (token) |
| `GET /api/control/selection`, `PUT` (token) | Coins a scan analyses: all, or your selection |
| `POST /api/control/kill`, `POST /api/control/resume` (token) | Emergency stop and resume |
| `GET /api/live` | Latest streamed prices. No provider calls |
| `GET` / `POST /api/watchlist`, `DELETE /api/watchlist/{symbol}` | Watchlist. Changes need the token |
| `GET /api/news?refresh=true` | Headlines, tone, and trending coins |
| `GET /api/sentiment?refresh=true` | Market mood, components, per-coin sentiment and notes |
| `GET /api/onchain?refresh=true` | BTC and ETH network state, stablecoin supply, large transfers, exchange flows |
| `GET /api/events/unlocks?refresh=true` | Upcoming token unlocks for the analysed coins (Mobula key) |
| `GET /api/events/airdrops?refresh=true` | Airdrops (AlphaDrops key) |
| `GET /api/chat/models` | Chat model choices, and which ones your OpenAI key can use (token) |
| `POST /api/chat` | AI assistant (token). Optional `model` and `reasoning_effort` |
| `POST /api/scalp/scan?horizon=15m\|1h\|4h` (token) | Scalp scan of the selected coins, in the background |
| `GET /api/scalp?horizon=1h` | Latest scalp scan and scan status |
| `GET /api/scalp/{symbol}?horizon=1h` (token) | Scalp analysis of one coin now |
| `POST /api/futures/scan?horizon=15m\|1h\|4h\|1d` (token) | Futures scan (long and short), in the background |
| `GET /api/futures?horizon=4h` | Latest futures scan, with leverage plans and research for both sides |
| `GET` / `PUT /api/futures/settings` (`PUT`: token) | Leverage cap, futures fee, slippage, maintenance margin |
| `GET /api/performance?days=90` | Track record per strategy, by AI verdict, and the latest outcomes |
| `GET /api/lab?horizon=1h`, `POST /run` · `/apply` · `/reset` (token) | Strategy lab result, run, apply, or reset a rule variant |
| `GET /api/ml?horizon=1h`, `PUT` (token) | Statistical filter state and validation. Switch it off or on |
| `GET` / `PUT /api/evidence/settings` (`PUT`: token) | Evidence board mode (`filter`, `advisory`, `off`), headline refresh, AI news reading |
| `GET /api/evidence/{symbol}?horizon=swing\|15m\|1h\|4h` | Latest evidence board for a coin. Never fetches |
| `GET /api/evidence/learning`, `POST /refresh`, `PUT` (token) | Measured edge per factor, held-back results, learned model |
| `GET /api/derivatives/market`, `GET /api/derivatives/{symbol}` | Funding, open interest, long/short ratios, taker flow, liquidations |
| `POST /api/ai/review` (token) | AI review of a swing, scalp, or futures signal |
| `GET` / `PUT /api/ai/settings` (`PUT`: token) | AI review mode and auto-review |
| `POST /api/ai/analyst/token?symbol=ETH&horizon=4h` (token) | AI analysis of one token, spot and futures, in the background |
| `GET /api/ai/analyst?symbol=ETH&horizon=4h` | That token's AI analysis, or the latest one when the symbol is omitted, and the tokens analysed |
| `GET` / `PUT /api/ai/analyst/settings`, `GET /api/ai/analyst/models` | Analyst settings (`PUT`: token) and the models your key can use (token) |
| `GET /api/portfolio`, `PUT /cash`, `POST` / `DELETE /positions`, `PUT /risk`, `POST /plan` | Portfolio, risk settings, trade plan (token) |

## 🗂️ Project structure

```text
backend/
  app/
    main.py, config.py, database.py, logging_config.py
    api/                 routes and dependencies
    core/                enums, time helpers
    data/
      providers/         Binance REST + WebSocket, Kraken, CoinMarketCap Pro and
                         keyless (with a credit budget), CoinGecko, CoinPaprika, alternative.me
      adapters/          provider payloads to normalized schemas
      normalization/     schemas, stablecoin and derivative classification
      validation/        candles, candle-history reference, prices, order book, volatility,
                         listings, health score, gate
      health.py, http.py provider health registry, resilient HTTP client
    analysis/            indicators, features, structure, regime, scoring, engine, final validation
    risk/                risk parameters, trade plan, risk checks
    services/            universe, listing, spot router, market, assets, context, persistence,
                         market regime, analysis (signals, scan, history)
    static/              status dashboard (HTML, CSS, JS)
    models/              22 SQLAlchemy tables
    schemas/             API response models
  migrations/            Alembic
  tests/
scripts/start.sh
Dockerfile, railway.json, .env.example
```

<details>
<summary><b>Where each phase lives</b></summary>

<br/>

| Area | Modules |
| --- | --- |
| Manual scans, schedule, live prices | `services/control.py` |
| Watchlist, news, chat | `services/watchlist.py`, `services/news.py`, `services/chat.py`, `services/assistant.py` |
| Portfolio | `services/portfolio.py` |
| On-chain, sentiment, events, coin selection | `services/onchain.py`, `services/sentiment.py`, `services/events.py`, `services/selection.py` |
| Scalps and the shared simulator | `analysis/scalp.py`, `analysis/trade_sim.py`, `services/scalp.py` |
| Track record and emergency stop | `services/outcomes.py`, `services/killswitch.py` |
| AI review | `services/ai_review.py` |
| Strategy lab | `analysis/lab.py`, `services/lab.py` |
| Statistical filter | `analysis/ml.py` |
| Strategy library | `analysis/strategies.py` |
| Signova AI analyst | `analysis/ai_analyst.py`, `services/ai_analyst.py` |
| Futures | `analysis/futures.py`, `services/futures.py` |
| Derivatives, liquidation map, evidence | `data/derivatives.py`, `services/derivatives.py`, `analysis/liqmap.py`, `analysis/evidence.py`, `analysis/evidence_learn.py`, `services/learning.py`, `services/evidence.py`, `services/ai_news.py` |
| JSON settings | `services/settings_store.py` (`app_settings`) |
| Routes for the controls above | `api/controls.py` |

</details>

<a id="credits"></a>

## 👤 Credits

<div align="center">

Signova is built by <a href="https://github.com/fr-ost"><b>Shahriar Ahmed</b></a>, Co-Founder and COO of <a href="https://www.uniquelabs.org"><b>Unique Labs</b></a>.

<br/>

<a href="https://github.com/fr-ost"><img alt="Shahriar Ahmed on GitHub" src="https://img.shields.io/badge/fr--ost-6C2BD9?style=for-the-badge&amp;logo=github&amp;logoColor=white"/></a>
<a href="https://www.uniquelabs.org"><img alt="Unique Labs" src="https://img.shields.io/badge/Unique_Labs-FF2E93?style=for-the-badge"/></a>
<a href="https://shahriarahmed.net"><img alt="shahriarahmed.net" src="https://img.shields.io/badge/shahriarahmed.net-FFB703?style=for-the-badge&amp;labelColor=1A1024"/></a>

<br/>

<img width="100%" alt="Footer wave" src="https://capsule-render.vercel.app/api?type=waving&amp;color=0:FFB703,50:FF2E93,100:6C2BD9&amp;height=120&amp;section=footer"/>

</div>
