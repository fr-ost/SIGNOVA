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
| 3 | On-demand controls, dashboard UI, charts, live updates | **Done** |
| 4 | Portfolio, risk, allocation, DCA, P/L scenarios | **Done** |
| 5 | News, sentiment, whale / on-chain | **Done** |
| 6 | Scalp signals, track record, emergency stop | **Done** (price alerts: not yet) |
| 7 | OpenAI reasoning layer | **Done**: AI chat assistant and AI review of signals (can only lower a signal) |
| 8 | Signal tracking, backtesting, statistics | **Done**: track record, per-coin backtests, Strategy lab (walk-forward test of rule variants) |
| 9 | ML / statistical prediction | **Done**: statistical trade filter, used only after it validates on newer data |
| 10 | Evidence engine | **Done**: futures positioning, liquidations and an estimated liquidation map, order flow, news read by the AI, hype, market trend; tracks the setups it holds back and learns from outcomes |

## Decision hierarchy

```
data integrity -> fail-safe validation -> deterministic calculations -> risk engine
-> quantitative signal engine -> evidence board (futures, flow, news, market; can only lower)
-> statistical/ML evidence -> OpenAI reasoning -> final deterministic validation -> dashboard
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

**CoinGecko key (optional):** set `COINGECKO_API_KEY` to your free Demo key
(coingecko.com/en/developers/dashboard). It is sent as the `x-cg-demo-api-key` header to
`https://api.coingecko.com/api/v3`, which raises the rate limit to about 30 calls a minute for the
listing and market-cap fallback and for trending coins. For a paid key also set
`COINGECKO_PLAN=pro` (uses `pro-api.coingecko.com` and `x-cg-pro-api-key`).

**CoinPaprika** uses its free plan, `https://api.coinpaprika.com/v1`, with no key. Its `/tickers`
answer lists every coin (several MB), so it gets `COINPAPRIKA_TIMEOUT_SECONDS` (30) instead of the
default 10 seconds, which is what made it time out before. It is only called when CoinMarketCap
and CoinGecko both fail.

## CoinMarketCap API key

If an endpoint is not in your plan (error 1006, for example OHLCV history on lower plans), the app
skips it for a day and shows it as a note on the provider card, not as a provider error.

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
ranks setups; it is **not a probability**. How the signals actually performed is measured in the
Track record (below).

**Engine audit (quant-2.1.0):** the indicator maths was re-verified (Wilder RSI/ATR/ADX, EMA,
MACD, Bollinger, OBV against reference values; swing points only once confirmed, no repainting).
The weak spots were in the rules, and three checks were added:

* **Entry trigger** (cap at WATCH): at least 2 of 3 on 1H (close above EMA20, MACD histogram
  rising, RSI rising). A pullback is no longer bought while 1H momentum is still falling.
* **Bitcoin 4H trend** (cap at WATCH, altcoins only): when Bitcoin's 4H trend is down, altcoin
  buys wait. Altcoins rarely rise against a falling Bitcoin.
* **Order book pressure** (no STRONG BUY): sellers stacked in the book (imbalance -0.25 or lower
  within 1%) prevent a STRONG BUY.

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
| Cap (at most `WATCH`) | 1D or 4H trend down, net reward:risk below 1.5R, nearest resistance closer than 0.75R, stop wider than 15%, price more than 2.5 ATR above the 4H EMA20, RSI above 78 (4H) or 80 (1D), 24h move above +25%, bear or unknown market regime, no 1H entry trigger, Bitcoin 4H trend down (altcoins) |
| Downgrade (at most `BUY`) | 1D and 4H not both up, net reward:risk below 2R, resistance closer than 1R, 15m RSI above 85, 4H volatility at the 95th percentile, neutral market regime, sellers dominating the order book |

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

**Provider usage:** scans run only on request. A scan analyses the universe with at most 4 coins
in parallel (about 350 Binance request weight of the 6,000 per-minute limit). CoinMarketCap candle
cross-checks stay cached per asset (30 minutes for 1H, 6 hours for 1D) under the credit pacing
described above.

## Scalp signals (15m / 1h / 4h)

The Scalp signals card finds short-term spot longs for a horizon you pick, on the coins in
"Coins to analyse". Press **Find scalps**; nothing runs in the background.

| Horizon | Setup candles | Trend must be up | Must not be down | Time limit |
|---|---|---|---|---|
| 15 min | 5m | 15m | 1H | 30 minutes |
| 1 hour | 15m | 1H | 4H | 2 hours |
| 4 hours | 1H | 4H | 1D | 8 hours |

**Setups** (long only): a *pullback* (uptrend, dip to the EMA20, RSI resets to 52 or lower, then
a bullish candle closes above the previous high) or a *breakout* (close above the 20-candle
high on 1.8x average volume after a quiet period, not overbought). Both also need Bitcoin's trend
not down (for altcoins), the price above today's VWAP (15 min and 1 hour), room to the next swing
high, and a stop at least 2.5x the round-trip costs away. With 0.1% fees a 15-minute scalp on
Bitcoin rarely has enough room: the engine says "move too small for fees" instead of pretending.

**Plan:** entry at the signal candle's close (buy zone up to 0.25R above), stop under the recent
low (0.8 to 2 ATR), TP1 at 1R (or just under a closer swing high) for half, stop to break-even,
TP2 at 2R, and a time exit. Size risks 1% of the portfolio (your risk settings).

**Backtest evidence gate:** before a coin gets a label, the *same rules* are replayed over its
recent history (4,000 setup candles: about 14 days for 15 min, 41 days for 1 hour, 166 days for 4
hours), one trade at a time, entries at the signal close, stop counted first when a candle hits
both, net of fees and slippage. The result decides the label:

* `STRONG BUY`: at least 25 trades, expectancy +0.25R or better, profit factor 1.5+, win rate 50%+.
* `BUY`: at least 15 trades, expectancy +0.1R or better and profit factor 1.2+; or, for a coin with
  too few trades, the same rules across all scanned coins (40+ trades) pass (capped at `BUY`).
* `NO TRADE`: the rules lost money on this coin (or across the coins).
* `WATCH`: not enough evidence, the setup type alone lost, the price already ran more than 0.25R
  above the entry, or the trend is right but no trigger yet.

Live checks the history cannot contain still apply: spread, depth, 24h volume, order-book
pressure, price versus the stop and TP1, and the entry window (2 candles). A test verifies the
engine never uses future data: every setup found in the full history is identical when the history
ends at that candle.

**Waiting coins (conditional plans):** most of the time a coin is in a usable trend but no
trigger candle has closed yet. Such a row is `WATCH` with a status line ("waiting for a pullback
trigger", "downtrend: no long setups"...) and, where the rules allow, a *conditional plan*: the
trigger price, the stop and the targets it would use, and what the coin's own backtest says it
would get if the trigger fires ("would be BUY", "would stay WATCH: not enough evidence", "would
be NO TRADE"). A conditional plan is not a signal: wait for the candle to close beyond the
trigger and scan again. Kinds: *pullback* (uptrend, price above the EMA20: buy the first close
above the last candle's high after a dip), *dip* (a dip under way: same trigger once it turns)
and *breakout* (a close above the 20-candle high on volume).

`POST /api/scalp/scan?horizon=1h` (token), `GET /api/scalp?horizon=1h`, `GET /api/scalp/{symbol}?horizon=4h` (token).

## Evidence engine (Phase 10)

The chart setup and its backtest decide *whether* a coin can be a buy and how strong. The
evidence board then checks everything else that moves crypto prices, and can only hold a buy
back (never create or raise one). Every swing signal (coin panel) and every scalp row shows its
board: a score from −100 (strongly against) to +100 (strongly for), each factor with its value,
what it means and where the data came from, and any veto.

| Group | Factors |
|---|---|
| Futures positioning | funding rate (crowded longs pay a lot), open interest versus price (new money or short covering), the crowd's long/short ratio (contrarian at extremes), **top traders ("whales")** adding or cutting longs, futures taker buying, **recent liquidations** (a long flush that ended, or a cascade still running), the **estimated liquidation map** |
| Order flow | spot taker buying on the signal candles, CVD divergence (price up while sellers hit the market), 24h volume versus its 20-day average, order-book imbalance, strength versus Bitcoin over 7 days |
| News and hype | critical headlines (hack, exploit, delisting, insolvency, halted withdrawals, charges), headline tone or the **AI's reading of the headlines**, catalysts (major-exchange listing, ETF, mainnet, integration), **hype** (headline count versus the coin's usual, CoinGecko trending) with a warning when hype meets a stretched price |
| Market | Bitcoin's 4H and 1D trend (for altcoins), market breadth, leverage across the market (average funding, crowded coins), Fear & Greed extremes, stablecoin supply, altcoin season |
| Events | large token unlocks, whale exchange inflows/outflows (BTC/ETH on-chain) |

**Futures data** is public and keyless: Binance futures first, then Bybit, OKX and Hyperliquid
for whatever a blocked or missing exchange cannot answer (many cloud regions are refused by
Binance or Bybit; an exchange that refuses the region is skipped for an hour). Liquidation
orders come from OKX (a sample of the market, labelled so). Provider health lists each exchange.
The **Evidence engine** card's *Load futures positioning* shows the market-wide view.

**Liquidation map** (estimate, labelled everywhere): every hour open interest rose in the last
7 days, new positions were opened near that hour's price; they are split into longs and shorts
by the long/short ratio and spread over typical leverage (5x-100x); later candles that traded
through a level remove it. Long liquidations below the price are forced selling if it falls
there, short liquidations above are forced buying if it rises there. The board says when more
fuel sits above than below, and when a cluster of long liquidations sits at your stop it
suggests a stop just under that zone.

**How it changes a signal** (mode *filter*, the default; *advisory* only shows it; *off*):
* a veto caps the buy at WATCH: critical news about the coin, a large unlock within 3 days,
  a long-liquidation cascade still running, or extreme funding while open interest jumped;
* a board at −25 or lower (strong headwinds, 4+ factors with data) caps it at WATCH;
* a board at −8 or lower turns STRONG BUY into BUY.

**AI in the engine:** with an OpenAI key, one request per new set of headlines reads them all
and rates each coin's news from −2 to +2 and flags critical events (cached 45 minutes; switch
in the Evidence engine card). The AI can clear a keyword false alarm (for example a hack of a
*different* project) but its reading never raises a signal. The AI reviewer (Phase 7) now also
sees the evidence board.

**Learning from outcomes:** a setup the board (or the learned model, or the statistical filter)
holds back is stored as FILTERED and followed exactly like a shown buy, in its own lane. The
Track record then shows what the held-back setups did (a filter helps when they did worse) and
results by board grade; the Evidence engine card shows each factor's measured edge (average
result when it argued for the trade versus against it). Once 90 setups with a board have
closed, a logistic model is trained on the older 70% and validated on the newer 30% (same rules
as Phase 9); only a validated model joins the filter, it can only lower a buy, and you can
switch it off. Until then the documented prior weights are used and nothing is claimed.

What it cannot do: there is no free source for social-media volume, real per-trader leverage, or
exchange order flow beyond these APIs; the liquidation map is an estimate; and none of it
guarantees a win rate. It removes trades with visible headwinds and measures itself.

`GET /api/evidence/settings`, `PUT` (`{"mode": "filter", "refresh_news": true, "ai_news": true}`, token),
`GET /api/evidence/{symbol}?horizon=swing`, `GET /api/evidence/learning`, `POST /api/evidence/learning/refresh` and
`PUT /api/evidence/learning` (`{"enabled": false}`, token), `GET /api/derivatives/market`, `GET /api/derivatives/{symbol}`.

## Strategy lab (Phase 8)

The published scalp rules are a starting point, not the best rules for every market. The
Strategy lab card tests alternatives on *your* coins' real history and tells you, honestly,
whether any of them is better. Pick a horizon and press **Run the lab** (token; it runs in the
background, takes a few minutes, and uses the same candles as a scalp scan, no extra credits).

* **Grid:** 8 entry filters (published rules, volume-confirmed pullbacks, ADX 20+, stop at least
  3.5x costs away, pullbacks only, breakouts only, buyers in control at the trigger (taker
  buying 52%+ of the last 3 candles' volume), coin stronger than Bitcoin) x 6 exit styles
  (targets 1R/2R, 1R/3R, 1.5R/3R, no break-even, all at 1.5R, double time limit) = 48 variants.
  The grid is small on purpose: the more variants one tries, the more likely the best-looking
  one is luck.
* **Walk-forward split:** each coin's history is split in time. The older 70% chooses the
  variant; the newer 30%, never used for choosing, tests it.
* **Acceptance:** the variant is applied to the scalp engine only if it earned at least +0.05R
  per trade on the older data, made at least 20 trades and a profit on the newer data, and did
  at least as well there as the published rules. Otherwise the lab says why and the published
  rules stay. You can apply any variant by hand or reset to the published rules.
* **Statistics shown:** trades, win rate, expectancy (R), profit factor, total R and max
  drawdown for all / older / newer data; the equity curve in R with the split marked; results by
  setup type, by time of day (UTC), by weekday, by coin and by outcome (stop, TP1, TP2, time).

The applied variant and the last result are stored in `app_settings` and survive restarts.
`GET /api/lab?horizon=1h`, `POST /api/lab/run`, `POST /api/lab/apply` (`{"filter": "adx", "exit": "x3"}`),
`POST /api/lab/reset` (token).

## Statistical trade filter (Phase 9)

Each lab run also trains a small logistic-regression model on the chosen variant's trades. It
estimates the chance that a setup ends as a winner (net of costs) from 19 numbers known when the
signal candle closes: trend strength (ADX, distance from the EMAs, higher-timeframe RSI and
momentum), volatility (ATR, Bollinger width), volume, RSI, distance to the 20-candle high,
Bitcoin's momentum, order flow (taker buying, net volume delta), the stop distance versus
costs, the setup type and the hour of day. It is
pure Python (no machine-learning libraries, little memory).

It is honest by construction: it is trained on the older trades and judged only on the newer
ones; its probability cut is chosen on the older trades; and it counts as **validated** only if,
on the newer trades, it separates winners from losers (AUC 0.55 or more) and the trades it keeps
earned clearly more (+0.05R) than all trades. Only a validated model is used, and it can only
lower a scalp label to `WATCH` (probability below the cut), never raise one. An unvalidated model
is shown with its numbers and reasons and does nothing. The lab card has a switch to turn a
validated model off. Each prediction is stored in `model_predictions`.
`GET /api/ml?horizon=1h`, `PUT /api/ml?horizon=1h` (`{"enabled": false}`, token).

## AI review (Phase 7)

Press **AI review** on a scalp or swing signal: an OpenAI model gets the engine's full plan and
evidence (levels, backtest, conditions, risks) and answers in JSON with a verdict, *agree*,
*caution* or *reject*, the concrete risks it sees and a short summary. The verdict shows next to
the signal and is stored with it (`signals.ai_output`), and the Track record groups results by
verdict, so after a few weeks you can see whether the AI's objections were worth anything.

* **Advisory** (default): the verdict is shown, the label is unchanged.
* **Filter**: a *reject* caps BUY / STRONG BUY at WATCH. The AI can only lower a signal, never
  raise one, and never overrides the data checks.
* **Auto-review** (off by default): after each scan, the first new buy signals (3; `auto_max` 1-10 via
  the API) are reviewed automatically. Each review is one OpenAI call.

Settings are in the AI assistant card and stored in `app_settings`.
`POST /api/ai/review` (`{"kind": "scalp", "symbol": "SOL", "horizon": "1h"}`, token),
`GET/PUT /api/ai/settings` (`{"mode": "filter", "auto": true, "auto_max": 3}`, token).

## Emergency stop

The red **Emergency stop** button in the header halts everything at once: the running scan
(coins in progress may finish for up to 20 seconds, then it is cancelled), Auto-analyze, live
prices, scalp scans, the Strategy lab and AI reviews. While it is engaged the server refuses
every request that would reach a provider, news source or OpenAI (HTTP 503), so no credits are
spent, and the dashboard's auto-refresh stops. Stored signals, the track record, the last scalp
scan and the lab results stay readable, and you can still edit the watchlist and coin
selection. It survives restarts. **Resume** (in the red banner) releases it; nothing restarts on
its own. `POST /api/control/kill` (optional `{"reason": "..."}`) and `POST /api/control/resume`
(token).

## Track record

Every BUY and STRONG BUY (swing and scalp) is followed on the candles after it with the same
pessimistic simulator as the backtest: stop first, half at TP1 then break-even, targets, and a
time limit (14 days for swing signals). A signal that appears while an earlier one on the same
coin and strategy is still running is marked SKIPPED, so one move is never counted twice. It
uses candles the analysis already fetched, so it costs no extra API calls. The card shows win
rate, average R, total R and profit factor per strategy for the last 90 days
(`GET /api/performance?days=90`). This is the honest measure of accuracy: unlike a backtest,
nothing in it was known when the rules were written. It also groups results by evidence-board
grade and shows, separately, what the setups held back by a filter would have made.

## About accuracy and "$50 a day"

No indicator set can promise a win rate, and nothing here does. What this dashboard does is
refuse trades whose own history says they lose, show the measured record of every setup, and size
every trade so a stop costs about 1% of the portfolio. Some arithmetic for a daily target: with a
measured expectancy of +0.2R and 1% risk per trade, each trade earns on average 0.2% of the
portfolio; $50 a day then needs about $25,000 per trade-a-day (for example $5,000 and five good
trades every day), and results vary a lot from day to day. Raising risk per trade to reach a
target faster is the usual way accounts are lost. Start with small sizes, watch the Track record
for a few weeks, and trust only what it shows.

More inputs do not automatically mean more accuracy: every extra factor can also add noise.
That is why the evidence board only ever holds trades back, why the setups it holds back are
tracked, and why its learned model must prove itself on newer outcomes before it filters
anything. Read the "By evidence board" and "Held back by filters" tables after a few weeks: they
tell you whether the extra data is helping on your coins.

## Manual control (Phase 3)

Nothing analyses in the background by default, so no provider calls or CoinMarketCap credits
are spent while you are not using the dashboard.

* **Analyze now** scans the Top 20 plus your watchlist once. Progress shows in the control bar.
* **Stop** cancels a running scan, switches Auto-analyze off and stops live prices.
* **Auto-analyze** (off by default) re-scans every 15 minutes to 4 hours until you stop it.
  `AUTO_ANALYZE_MINUTES` sets a schedule at startup (0 = off).
* **Live prices** stream Binance mini-tickers over WebSocket (no REST calls, no credits) and update
  prices in place until you stop them.
* **Refresh data** reloads market data once; the **Auto-refresh** checkbox (off by default)
  reloads it every 30 seconds.
* `GET /api/signals` only returns the latest completed scan; it never starts one.
* **Coins to analyse** (under the control bar): pick exactly which coins a scan covers, for
  example just five (Top 5 / Top 10 / Select all / Clear, or click coins), then Save selection.
  Analyze now, Auto-analyze and live prices then use only those coins, which saves provider
  calls. The market table still lists every coin (unselected ones are dimmed). Coins you add
  to the watchlist join the selection. Stored in `app_settings` (migration 0003);
  `GET/PUT /api/control/selection`.

**Watchlist:** add any coin by symbol. It joins the next scan (marked "watchlist") with the full
pipeline. Remove it with **Remove** in the Watchlist card, or **Remove from watchlist** under its name in
the market table (it also leaves "Coins to analyse"). It is priced from the 200-coin listing (still one CoinMarketCap credit), or from
CoinMarketCap quotes when it ranks lower. Stored in the `watchlist` table (migration 0002).

**Charts:** the coin panel shows a candlestick chart (15m/1H/4H/1D, closed candles) with EMA20,
EMA50, the entry zone, stop and targets, and a hover readout.

## Portfolio and risk (Phase 4)

Record cash and positions (spot, manual; nothing is traded). The portfolio shows value, P/L,
allocation, total exposure and warnings (exposure above your limit, concentration), plus P/L
scenarios per position (-20% to +20%, and the coin's signal stop and targets).

**Risk settings** (max loss per trade, max position size, first-buy and DCA sizes, max total
exposure, fees, slippage) are stored in `risk_settings` and applied to the signal engine
immediately: every signal's suggested size and net reward:risk follow them.

**Plan trade** takes a coin (and an optional budget) and returns, from its current plan: the
risk-based budget, a three-step DCA ladder across the entry zone, average entry, loss at the stop
as a share of equity, and P/L after costs for "stop hit", "TP1 then stop at entry",
"TP1 + TP2" and "all targets".

## AI assistant and news

**AI assistant** (homepage, `POST /api/chat`): chat with an OpenAI model about the next trade.
Each question carries the dashboard's own data as context: the latest scan and market regime,
news headlines, market mood and on-chain data, your portfolio and risk settings and, if you pick a coin (or press "Ask AI about
this coin"), its full analysis. The system prompt makes the engine authoritative: the assistant
explains and discusses but cannot turn a NO TRADE or WATCH into a buy. Models:
`OPENAI_ANALYSIS_MODEL` (default `gpt-5-mini`), falling back to `OPENAI_FALLBACK_MODEL`
(default `gpt-4o-mini`) if the first is unavailable. Rate limited to
`CHAT_RATE_LIMIT_PER_MINUTE` (10).

**Model picker:** the chat card has a model list (`CHAT_MODEL_OPTIONS`; models your key cannot
use are greyed out, checked with OpenAI's model list) and a reasoning-effort setting for GPT-5
and o-series models. Your choice is remembered in the browser. The reply shows which model
answered.

*Fix for "The model returned no text":* GPT-5 models reason before they answer, and the
reasoning counts against the output limit. The old limit (1500 tokens) was often used up by
reasoning alone, leaving an empty answer. Reasoning models now get
`CHAT_REASONING_BUDGET_TOKENS` (6000) on top of `CHAT_MAX_OUTPUT_TOKENS` and
`CHAT_REASONING_EFFORT=low` by default; if a model still returns nothing, the next model
answers instead and the reply says so.

**News** (`GET /api/news`, cached 15 minutes, fetched only when you press Load/Refresh, or on page
open if you tick "Load when the page opens"; nothing runs in the background, and sentiment and
the chat only read headlines already loaded):
RSS from CoinDesk, Cointelegraph, Decrypt and Bitcoin Magazine, CryptoCompare's free news API
and CoinGecko trending coins, with no keys needed. Headlines are de-duplicated, tagged with the
coins they mention, given a keyword-based tone (labelled as such) and stored in the `news` table.
A source that fails is listed, never guessed. News is context for you and the assistant; it is
not an input to the signal engine.

## Sentiment and on-chain (Phase 5)

**Market mood** (`GET /api/sentiment`, cached 10 minutes): three transparent components, each
scaled from -1 (fear) to +1 (greed) and combined with fixed weights:

| Component | Source | Weight |
|---|---|---|
| Fear & Greed now vs 50, with 7-day and 30-day averages and the 7-day trend | alternative.me | 0.5 |
| News tone of the last 48 hours (keyword method) | the news sources above | 0.3 |
| Average perpetual funding vs the 0.01%/8h baseline | Binance futures `premiumIndex` (public) | 0.2 |

The score maps to EXTREME FEAR, FEAR, NEUTRAL, GREED or EXTREME GREED. Stablecoin supply
change (DefiLlama) is shown alongside as a liquidity indicator but not weighted.

**Per coin**: headline tone, perpetual funding rate and labelled exchange flows. Notes are
raised for crowded longs (funding >= 0.05% per 8h), crowded shorts (<= -0.03%), mostly
negative headlines and large labelled exchange inflows. These notes join the signal's
"Risks to keep in mind" as `sentiment: ...`. **Sentiment never changes a label or a score**;
there is no measured evidence yet that it improves them (that is Phase 8's job).

**On-chain** (`GET /api/onchain`, cached 10 minutes), all free and keyless:

| Data | Source |
|---|---|
| BTC fees, mempool size, hashrate, next difficulty adjustment | mempool.space |
| Large BTC transactions (>= `WHALE_MIN_BTC`, default 100 BTC) | blockchain.com unconfirmed transactions |
| ETH gas, network utilisation, large ETH transfers (>= `WHALE_MIN_ETH`, default 1000 ETH) | Blockscout, whose public address names identify many exchange wallets |
| USD stablecoin supply and its 1d / 7d / 30d change | DefiLlama |
| Optional: labelled whale transfers on every major chain | Whale Alert (`WHALE_ALERT_API_KEY`) |

A transfer is only called an exchange inflow or outflow when a source labels one side as an
exchange; everything else is "unlabelled". Large transfers are stored in `whale_events`,
sentiment readings in `sentiment`. Pressing Analyze now refreshes sentiment first (at most
once a minute), so each scan carries current notes. Forced refreshes within
`MIN_REFRESH_SECONDS` (60) of the last fetch are served from the cache to protect the free
sources.

## Token unlocks and airdrops

`GET /api/events/unlocks` and `GET /api/events/airdrops`, shown in the "Token unlocks &
airdrops" card (loaded when you press Load, cached `EVENTS_CACHE_SECONDS`, 6 hours).

* **Token unlocks** (Mobula, `MOBULA_API_KEY`; free key at mobula.io): each analysed coin's
  release schedule, with the next unlock date, tokens, USD value, share of circulating supply
  and who receives them. Unlocks of `UNLOCK_NOTE_MIN_PCT` (1%) of circulating supply or more
  within `UNLOCK_NOTE_DAYS` (14) are added to that coin's signal risks ("token unlock in N
  days..."). Like sentiment, this never changes a label or score. With a key, each scan
  refreshes unlocks at most once per cache period.
* **Airdrops** (AlphaDrops Developer API, `ALPHADROPS_API_KEY`, a paid subscription): active,
  claimable and upcoming airdrops with chains, estimated reward and end date. They are
  unverified third-party listings.
* **Tokenomist** has no free API, so it is not integrated.

Without a key, the card says which variable to set. The Mobula and AlphaDrops response formats
were implemented from their published docs and read defensively; check the card once after
adding a key.

## Security

Set `ADMIN_TOKEN` on Railway. Then Analyze now, Stop, Auto-analyze, live prices, watchlist
changes, the AI chat and AI reviews (they spend your OpenAI credits), scalp scans, the Strategy
lab, evidence-engine settings, the emergency stop and resume, and the portfolio all require it. The
dashboard asks for it once and keeps it in your browser. Public market data stays readable.
Without `ADMIN_TOKEN`, anyone who finds the URL can use those controls.

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
| `GET /api/signals` | Latest completed scan, best first (null until Analyze now has run) |
| `GET /api/control/status` | Scan progress, schedule, live-price state, whether a token is required |
| `POST /api/control/analyze` · `/stop` · `/auto` · `/live/start` · `/live/stop` | Manual control (token) |
| `GET /api/live` | Latest streamed prices (no provider calls) |
| `GET/POST /api/watchlist`, `DELETE /api/watchlist/{symbol}` | Watchlist (changes need the token) |
| `GET /api/news?refresh=true` | Headlines, tone, trending coins from free sources |
| `GET /api/sentiment?refresh=true` | Phase 5: market mood, components, per-coin sentiment and notes |
| `GET /api/onchain?refresh=true` | Phase 5: BTC/ETH network state, stablecoin supply, large transfers, exchange flows |
| `GET /api/control/selection`, `PUT` (token) | Coins a scan analyses: all, or your selection |
| `GET /api/events/unlocks?refresh=true` | Upcoming token unlocks for the analysed coins (Mobula key) |
| `GET /api/events/airdrops?refresh=true` | Airdrops (AlphaDrops key) |
| `GET /api/chat/models` | Chat model choices and which ones your OpenAI key can use (token) |
| `POST /api/scalp/scan?horizon=15m\|1h\|4h` (token) | Scalp scan of the selected coins, in the background |
| `GET /api/scalp?horizon=1h` | Latest scalp scan (signals, per-coin backtest, pooled record) and scan status |
| `GET /api/scalp/{symbol}?horizon=1h` (token) | Scalp analysis of one coin now |
| `GET /api/performance?days=90` | Track record per strategy, by AI verdict, and the latest outcomes |
| `POST /api/control/kill`, `POST /api/control/resume` (token) | Emergency stop and resume |
| `GET /api/lab?horizon=1h`, `POST /run` · `/apply` · `/reset` (token) | Phase 8: Strategy lab result, run, apply or reset a rule variant |
| `GET /api/ml?horizon=1h`, `PUT` (token) | Phase 9: statistical filter state and validation; switch it off or on |
| `GET/PUT /api/evidence/settings` (PUT: token) | Phase 10: evidence board mode (filter / advisory / off), headline refresh, AI news reading |
| `GET /api/evidence/{symbol}?horizon=swing\|15m\|1h\|4h` | Phase 10: the latest evidence board for a coin (never fetches) |
| `GET /api/evidence/learning`, `POST /refresh`, `PUT` (token) | Phase 10: measured edge per factor, held-back results, learned model |
| `GET /api/derivatives/market`, `GET /api/derivatives/{symbol}` | Phase 10: futures funding, open interest, long/short ratios, taker flow, liquidations |
| `POST /api/ai/review` (token) | Phase 7: AI review of a scalp or swing signal (agree / caution / reject) |
| `GET/PUT /api/ai/settings` (PUT: token) | AI review mode (advisory / filter) and auto-review |
| `POST /api/chat` | AI assistant (token); optional `model` and `reasoning_effort` |
| `GET /api/portfolio`, `PUT /cash`, `POST/DELETE /positions`, `PUT /risk`, `POST /plan` | Portfolio, risk settings, trade plan (token) |
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
Max loss per trade, max position size, fees and slippage are then managed in the dashboard's
Portfolio > Risk settings, which are stored in the database and take precedence.

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
| `SIGNAL_SCAN_CONCURRENCY` | 4 | coins analysed in parallel during a scan |
| `ADMIN_TOKEN` | unset | protects controls, chat and portfolio (strongly recommended) |
| `AUTO_ANALYZE_MINUTES` | 0 | scan schedule at startup (0 = manual only) |
| `OPENAI_ANALYSIS_MODEL` / `OPENAI_FALLBACK_MODEL` | gpt-5-mini / gpt-4o-mini | chat models |
| `NEWS_CACHE_SECONDS` / `NEWS_FEEDS` | 900 / four RSS feeds | news cadence and sources |
| `SENTIMENT_CACHE_SECONDS` / `ONCHAIN_CACHE_SECONDS` | 600 / 600 | Phase 5 cadence |
| `WHALE_MIN_BTC` / `WHALE_MIN_ETH` | 100 / 1000 | smallest transfer listed as a whale |
| `WHALE_ALERT_API_KEY` / `WHALE_ALERT_MIN_USD` | unset / 1000000 | optional Whale Alert source |
| `COINGECKO_API_KEY` / `COINGECKO_PLAN` | unset / demo | CoinGecko Demo (free) or Pro key |
| `COINPAPRIKA_TIMEOUT_SECONDS` | 30 | timeout for the large CoinPaprika free-plan download |
| `CHAT_REASONING_EFFORT` / `CHAT_REASONING_BUDGET_TOKENS` | low / 6000 | GPT-5 / o-series reasoning |
| `CHAT_MODEL_OPTIONS` | gpt-5-mini, gpt-5, gpt-5-nano, gpt-4.1, gpt-4.1-mini, gpt-4o, gpt-4o-mini, o4-mini | models in the chat picker |
| `MOBULA_API_KEY` / `ALPHADROPS_API_KEY` | unset | token unlocks / airdrops |
| `EVENTS_CACHE_SECONDS` / `UNLOCK_WINDOW_DAYS` | 21600 / 30 | unlock and airdrop cadence, unlock window |
| `SCALP_SLIPPAGE_PCT` / `SCALP_MIN_RISK_COST_MULTIPLE` / `SCALP_MIN_TRADES` | 0.02 / 2.5 / 15 | scalp costs, fee filter, evidence minimum |
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
Phase 3-5: manual control, watchlist, news parsing, chat, portfolio, and the on-chain and
sentiment parsers, scores, notes, endpoints, refresh guard and persistence, with every free
source mocked. Sentiment notes are checked to leave labels and scores unchanged.
Phase 6-9: scalp rules without lookahead, per-coin backtests, the track record, conditional
plans, the emergency stop (allowlist, graceful stop, persistence), watchlist removal, the
Strategy lab split and acceptance rule, the statistical filter (validation gates, no future data
in its features, filtering only when validated) and AI review (JSON parsing, advisory and filter
modes, auto-review, storage and the by-verdict record), on SQLite and PostgreSQL.
Phase 10: every exchange parser against its documented payload (errors, 1000x contracts),
exchange failover and region skipping, OKX USD open interest, the liquidation map (levels,
crossed levels removed, long/short split), each evidence factor and veto, thin boards,
learning (a predictive factor validates, random factors never do), held-back setups tracked in
their own lane, and the swing and scalp integration.
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

Phase 3/4 services: `control.py` (manual scans, schedule, live prices), `watchlist.py`,
`news.py`, `chat.py` and `assistant.py` (OpenAI chat and its context), `portfolio.py`;
Phase 5: `onchain.py` (network data, whales, stablecoins), `sentiment.py` (market mood),
`events.py` (token unlocks, airdrops), `selection.py` (coins to analyse);
Phase 6: `analysis/scalp.py` (scalp rules and backtest), `analysis/trade_sim.py` (shared trade
simulator), `services/scalp.py`, `services/outcomes.py` (track record),
`services/killswitch.py` (emergency stop);
Phase 7: `services/ai_review.py`;
Phase 8: `analysis/lab.py` (variant grid, walk-forward split, statistics), `services/lab.py`;
Phase 9: `analysis/ml.py` (logistic regression and its validation);
Phase 10: `data/derivatives.py` (exchange parsers), `services/derivatives.py` (failover, cache),
`analysis/liqmap.py` (liquidation map), `analysis/evidence.py` (factors, score, vetoes),
`analysis/evidence_learn.py` and `services/learning.py` (learning from outcomes),
`services/evidence.py`, `services/ai_news.py` (the AI reads the headlines);
`services/settings_store.py` (small JSON settings in `app_settings`);
routes in `api/controls.py`.
