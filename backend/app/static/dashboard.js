// Phase 1 status dashboard: renders the existing JSON API. No build step, no dependencies.
// Every provider-supplied string is inserted with textContent, never as HTML.
"use strict";

(() => {
  const REFRESH_MS = 30000;
  const REQUEST_TIMEOUT_MS = 45000;
  const DASH = "—";

  const TONE = {
    HEALTHY: "good", DEGRADED: "warning", EXTREME_VOLATILITY: "serious", STALE_DATA: "serious",
    DATA_CONFLICT: "critical", API_FAILURE: "critical", SIGNAL_PAUSED: "critical",
    UP: "good", RATE_LIMITED: "serious", RESTRICTED: "critical", DOWN: "critical", UNKNOWN: "neutral",
    CONSISTENT: "good", WARNING: "warning", CONFLICT: "critical", UNVERIFIED: "neutral",
    AVAILABLE: "good", UNAVAILABLE: "neutral",
    PASS: "good", "NO TRADE": "critical",
    closed: "good", half_open: "warning", open: "critical",
    IDLE: "neutral", ANALYZING: "good", LIVE_MONITORING: "good", EMERGENCY_STOP: "critical",
  };
  const ICON = { good: "✓", warning: "!", serious: "!", critical: "✕", neutral: "–" };

  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- DOM helper

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value == null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
      if (child == null || child === false) continue;
      el.append(child instanceof Node ? child : String(child));
    }
    return el;
  }

  // ---------------------------------------------------------------- formatting

  const nf = (opts) => new Intl.NumberFormat("en-US", opts);
  const compact = nf({ notation: "compact", maximumFractionDigits: 2 });
  const integer = nf({ maximumFractionDigits: 0 });

  function humanize(value) {
    if (value == null) return DASH;
    const text = String(value).replace(/_/g, " ").toLowerCase();
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function fmtPrice(value) {
    if (value == null) return DASH;
    const abs = Math.abs(value);
    if (abs >= 1000) return nf({ minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
    if (abs >= 1) return nf({ minimumFractionDigits: 2, maximumFractionDigits: 4 }).format(value);
    return nf({ minimumSignificantDigits: 4, maximumSignificantDigits: 4 }).format(value);
  }

  const fmtUsd = (value) => (value == null ? DASH : "$" + compact.format(value));
  const fmtNum = (value) => (value == null ? DASH : compact.format(value));
  const fmtInt = (value) => (value == null ? DASH : integer.format(value));

  function fmtPct(value, digits = 2) {
    if (value == null) return DASH;
    const sign = value > 0 ? "+" : value < 0 ? "−" : "";
    return sign + Math.abs(value).toFixed(digits) + "%";
  }

  function fmtAge(seconds) {
    if (seconds == null) return DASH;
    const s = Math.max(0, seconds);
    if (s < 90) return `${Math.round(s)}s`;
    if (s < 5400) return `${Math.round(s / 60)}m`;
    if (s < 129600) return `${Math.round(s / 3600)}h`;
    return `${Math.round(s / 86400)}d`;
  }

  function ageOf(iso) {
    if (!iso) return null;
    const t = Date.parse(iso);
    return Number.isNaN(t) ? null : (Date.now() - t) / 1000;
  }

  const fmtAgo = (iso) => (iso ? `${fmtAge(ageOf(iso))} ago` : DASH);

  function fmtTime(iso) {
    if (!iso) return DASH;
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? DASH : d.toLocaleTimeString();
  }

  // ---------------------------------------------------------------- building blocks

  function badge(value, label, extraClass) {
    const tone = TONE[value] || "neutral";
    return h(
      "span",
      { class: `badge tone-${tone}${extraClass ? " " + extraClass : ""}` },
      h("span", { class: "badge-icon", "aria-hidden": "true", text: ICON[tone] }),
      label ?? humanize(value),
    );
  }

  function delta(value, digits = 2) {
    if (value == null) return h("span", { class: "muted", text: DASH });
    const dir = value > 0 ? "up" : value < 0 ? "down" : "flat";
    const arrow = dir === "up" ? "▲ " : dir === "down" ? "▼ " : "";
    return h("span", { class: `delta ${dir}` }, arrow + fmtPct(value, digits));
  }

  function kv(label, value) {
    return h("div", { class: "kv" }, h("dt", { text: label }), h("dd", {}, value ?? DASH));
  }

  function card(title, meta, ...body) {
    return h(
      "section",
      { class: "card" },
      h("div", { class: "card-head" }, h("h2", { text: title }), meta ? h("span", { class: "muted small" }, meta) : null),
      ...body,
    );
  }

  function table(headers, rows) {
    return h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        {},
        h("thead", {}, h("tr", {}, headers.map(([label, cls]) => h("th", { class: cls, text: label })))),
        h("tbody", {}, rows),
      ),
    );
  }

  function setMessage(el, title, items) {
    el.replaceChildren(h("strong", { text: title }));
    if (items && items.length) el.append(h("ul", {}, items.map((t) => h("li", { text: t }))));
    el.hidden = false;
  }

  // ---------------------------------------------------------------- network

  async function getJSON(url) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), REQUEST_TIMEOUT_MS);
    try {
      const res = await fetch(url, { headers: { Accept: "application/json" }, signal: ctrl.signal });
      let body = null;
      try {
        body = await res.json();
      } catch {
        body = null;
      }
      if (!res.ok) {
        const detail = body && typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`;
        const err = new Error(detail);
        err.status = res.status;
        err.body = body;
        throw err;
      }
      return body;
    } catch (err) {
      if (err.name === "AbortError") throw new Error(`request to ${url} timed out`);
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  // ---------------------------------------------------------------- market overview

  function tile(label, value, unit, sub, available = true) {
    return h(
      "div",
      { class: `tile${available ? "" : " unavailable"}` },
      h("div", { class: "tile-label", text: label }),
      h("div", { class: "tile-value" }, value, unit ? h("span", { class: "unit", text: unit }) : null),
      sub ? h("div", { class: "tile-sub" }, sub) : null,
    );
  }

  const unavailableTile = (label, reason) => tile(label, "Unavailable", null, reason || "source did not respond", false);

  function renderTiles(ctx) {
    const tiles = [];
    const g = ctx.global_metrics;
    if (g) {
      tiles.push(
        tile("Total market cap", fmtUsd(g.total_market_cap_usd), null, [
          delta(g.market_cap_change_24h_pct), " 24h · ", g.source,
        ]),
      );
      tiles.push(
        tile(
          "BTC dominance",
          g.btc_dominance_pct == null ? DASH : g.btc_dominance_pct.toFixed(1),
          g.btc_dominance_pct == null ? null : "%",
          `ETH ${g.eth_dominance_pct == null ? DASH : g.eth_dominance_pct.toFixed(1) + "%"} · ${g.source}`,
        ),
      );
    } else {
      tiles.push(unavailableTile("Total market cap"), unavailableTile("BTC dominance"));
    }
    const fg = ctx.fear_greed;
    tiles.push(
      fg
        ? tile("Fear & Greed", String(fg.value), "/ 100", `${fg.classification} · ${fg.source}`)
        : unavailableTile("Fear & Greed"),
    );
    const alt = ctx.altcoin_season;
    tiles.push(
      alt
        ? tile(
            "Altcoin season",
            String(alt.value),
            "/ 100",
            alt.yearly_low != null && alt.yearly_high != null
              ? `1y range ${alt.yearly_low}–${alt.yearly_high} · ${alt.source}`
              : alt.source,
          )
        : unavailableTile("Altcoin season", "shown only when CoinMarketCap serves it"),
    );
    $("tiles").replaceChildren(...tiles);
  }

  function priceCheckCell(cross) {
    if (!cross) return h("span", { class: "muted", text: DASH });
    const label = cross.deviation_pct == null ? humanize(cross.status) : `${cross.deviation_pct.toFixed(2)}%`;
    const el = badge(cross.status, label);
    el.title = `${humanize(cross.status)}: ${cross.reason}`;
    return el;
  }

  function stateCell(row) {
    const wrap = h("div", {}, badge(row.data_state));
    if (row.reasons && row.reasons.length) {
      const extra = row.reasons.length > 1 ? ` (+${row.reasons.length - 1} more)` : "";
      wrap.append(h("span", { class: "reason", title: row.reasons.join("\n") }, row.reasons[0] + extra));
    }
    return wrap;
  }

  function marketRow(row) {
    const open = () => openDetail(row.symbol);
    return h(
      "tr",
      { class: `clickable${row.supported ? "" : " unsupported"}`, onclick: open },
      h("td", { class: "num muted hide-sm", text: row.universe_rank }),
      h(
        "td",
        {},
        h(
          "div",
          { class: "asset-cell" },
          h("button", {
            type: "button",
            class: "link-button",
            text: row.symbol,
            "aria-label": `Open integrity details for ${row.symbol}`,
            onclick: (e) => { e.stopPropagation(); open(); },
          }),
          h("span", { class: "muted small", text: row.name }),
        ),
      ),
      h(
        "td",
        { class: "num" },
        fmtPrice(row.price),
        row.price != null && row.quote_asset ? h("span", { class: "muted small hide-sm", text: " " + row.quote_asset }) : null,
      ),
      h("td", { class: "num", title: row.pct_change_24h_source ? `source: ${row.pct_change_24h_source}` : null }, delta(row.pct_change_24h)),
      h("td", { class: "num hide-sm", title: row.volume_24h_source ? `source: ${row.volume_24h_source}` : null, text: fmtUsd(row.volume_24h_usd) }),
      h("td", { class: "num hide-sm", text: fmtUsd(row.market_cap_usd) }),
      h(
        "td",
        { class: "hide-sm" },
        row.market_source ? h("div", {}, row.market_source, h("div", { class: "muted small", text: row.market_symbol })) : DASH,
      ),
      h("td", { class: "hide-sm" }, priceCheckCell(row.cross_check)),
      h("td", {}, stateCell(row)),
      h("td", { class: "num hide-sm", title: `health score (${row.health_scope} level)`, text: row.data_health_score }),
    );
  }

  function renderMarket(m) {
    renderTiles(m.context);

    const overall = $("overall-state");
    overall.replaceChildren(badge(m.data_state, `Data: ${humanize(m.data_state)}`));

    const reasons = $("state-reasons");
    if (m.data_state !== "HEALTHY" && m.data_state_reasons.length) {
      setMessage(reasons, `Overall data state: ${humanize(m.data_state)}`, m.data_state_reasons);
    } else {
      reasons.hidden = true;
    }

    const u = m.universe;
    const access = u.listing_access ? ` (${u.listing_access})` : "";
    const counts = Object.entries(m.state_counts)
      .map(([state, n]) => `${n} ${humanize(state).toLowerCase()}`)
      .join(", ");
    $("universe-meta").textContent =
      `Ranking: ${u.listing_source}${access}${u.fallback_used ? ", fallback" : ""}${u.stale ? ", STALE" : ""} · ${counts}`;

    const rows = m.assets.map(marketRow);
    $("market-rows").replaceChildren(
      ...(rows.length ? rows : [h("tr", {}, h("td", { colspan: 10, class: "empty", text: "No assets in the universe." }))]),
    );

    const excluded = $("excluded");
    if (u.excluded.length) {
      excluded.querySelector("summary").textContent = `${u.excluded.length} excluded from the Top 20 (stablecoins, wrapped and pegged tokens)`;
      excluded.querySelector("ul").replaceChildren(
        ...u.excluded.map((e) => h("li", {}, h("strong", { text: e.symbol }), ` ${e.name} · ${fmtUsd(e.market_cap_usd)} · ${e.reason}`)),
      );
      excluded.hidden = false;
    } else {
      excluded.hidden = true;
    }
  }

  // ---------------------------------------------------------------- provider health

  function providerNotes(providers, stream) {
    const notes = [];
    for (const p of providers) {
      const cmc = p.details && p.details.cmc;
      if (cmc) {
        let text = `CoinMarketCap: mode ${cmc.mode}`;
        if (cmc.last_access) text += `, last served by ${cmc.last_access}`;
        const b = cmc.budget;
        if (b && b.plan_known) {
          text += ` · ${fmtInt(b.credits_left_month)} of ${fmtInt(b.credit_limit_monthly)} credits left this month`;
          if (b.monthly_reset_at) text += ` (resets ${new Date(b.monthly_reset_at).toLocaleDateString()})`;
          if (b.credits_used_today != null) text += ` · ${fmtInt(b.credits_used_today)} used today`;
          if (b.paced_credits_per_day != null) text += ` · paced at ${fmtInt(b.paced_credits_per_day)}/day`;
        }
        if (cmc.pro_disabled_reason) text += ` · key rejected: ${cmc.pro_disabled_reason}`;
        notes.push(text);
      }
      if (p.provider === "binance" && p.details) {
        const d = p.details;
        const parts = [];
        if (d.active_base_url) parts.push(`endpoint ${d.active_base_url}`);
        if (d.used_weight_1m != null) parts.push(`request weight ${d.used_weight_1m}/min`);
        if (d.restricted_bases && d.restricted_bases.length) parts.push(`restricted: ${d.restricted_bases.join(", ")}`);
        if (parts.length) notes.push(`Binance: ${parts.join(" · ")}`);
      }
    }
    if (stream) {
      let text = `Live stream: ${stream.connected ? "connected" : stream.running ? "connecting" : "not running (starts on demand)"}`;
      if (stream.connected && stream.base_url) text += ` to ${stream.base_url}`;
      if (stream.streams && stream.streams.length) text += ` · ${stream.streams.length} streams`;
      if (stream.reconnects) text += ` · ${stream.reconnects} reconnects`;
      if (stream.last_error) text += ` · last error: ${stream.last_error}`;
      notes.push(text);
    }
    return notes;
  }

  function renderProviders(ph) {
    const rows = ph.providers.map((p) =>
      h(
        "tr",
        {},
        h("td", {}, h("strong", { text: p.provider })),
        h("td", { class: "hide-sm muted", text: humanize(p.role) }),
        h("td", {}, badge(p.status)),
        h("td", { class: "hide-sm" }, badge(p.circuit_state)),
        h("td", { class: "num hide-sm", text: p.avg_latency_ms == null ? DASH : `${Math.round(p.avg_latency_ms)} ms` }),
        h("td", { class: "num hide-sm", text: fmtInt(p.total_requests) }),
        h("td", { class: "num hide-sm", text: fmtInt(p.total_failures) }),
        h("td", { class: "num hide-sm", text: fmtInt(p.rate_limit_hits) }),
        h("td", { class: "muted", text: fmtAgo(p.last_success_at) }),
        h("td", { class: "hide-sm" }, p.last_error ? h("span", { class: "reason", title: p.last_error, text: p.last_error }) : DASH),
      ),
    );
    $("provider-rows").replaceChildren(
      ...(rows.length
        ? rows
        : [h("tr", {}, h("td", { colspan: 10, class: "empty", text: "No provider has been called yet." }))]),
    );

    const notes = providerNotes(ph.providers, ph.live_stream);
    const box = $("provider-notes");
    box.replaceChildren(...notes.map((n) => h("div", { class: "muted", text: n })));
    box.hidden = notes.length === 0;
    $("stream-meta").textContent = `${ph.providers.length} providers`;
  }

  // ---------------------------------------------------------------- refresh loop

  let timer = null;
  let loading = false;

  async function refresh() {
    if (loading) return;
    loading = true;
    const button = $("refresh");
    button.disabled = true;
    button.textContent = "Refreshing…";
    const errors = [];
    const [market, providers, system] = await Promise.allSettled([
      getJSON("/api/market"),
      getJSON("/api/provider-health"),
      getJSON("/api/system/state"),
    ]);
    if (market.status === "fulfilled") renderMarket(market.value);
    else errors.push(`Market data: ${market.reason.message}`);
    if (providers.status === "fulfilled") renderProviders(providers.value);
    else errors.push(`Provider health: ${providers.reason.message}`);
    if (system.status === "fulfilled") {
      const s = system.value;
      if (s.signals_paused) errors.push(`Signals paused: ${s.signal_paused_reason || "no reason given"}`);
      if (s.processing_state === "EMERGENCY_STOP") errors.push("Emergency stop is active.");
    }

    const errorBox = $("error");
    if (errors.length) setMessage(errorBox, "Some data could not be loaded", errors);
    else errorBox.hidden = true;
    if (market.status === "rejected" && $("market-rows").querySelector(".empty")) {
      $("market-rows").replaceChildren(
        h("tr", {}, h("td", { colspan: 10, class: "empty", text: `Market data unavailable: ${market.reason.message}` })),
      );
    }

    $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    button.disabled = false;
    button.textContent = "Refresh";
    loading = false;
  }

  function schedule() {
    clearInterval(timer);
    timer = null;
    if (document.visibilityState === "visible") timer = setInterval(refresh, REFRESH_MS);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
    schedule();
  });

  // ---------------------------------------------------------------- asset detail

  const dialog = () => $("detail");
  let detailToken = 0;

  function closeDetail() {
    const d = dialog();
    if (d.open) d.close();
  }

  async function openDetail(symbol) {
    const d = dialog();
    const token = ++detailToken;
    $("detail-title").textContent = symbol;
    $("detail-body").replaceChildren(
      h("p", { class: "muted", text: `Collecting ${symbol}: 5 timeframes of candles, order book and cross-checks…` }),
    );
    if (!d.open) d.showModal();
    if (location.hash !== `#${symbol}`) history.replaceState(null, "", `#${symbol}`);
    try {
      const detail = await getJSON(`/api/assets/${encodeURIComponent(symbol)}`);
      if (token === detailToken) renderDetail(detail);
    } catch (err) {
      if (token !== detailToken) return;
      const body = $("detail-body");
      const box = h("div", { class: "alert", role: "alert" });
      const extra = err.body && Array.isArray(err.body.errors) ? err.body.errors : [];
      setMessage(box, `Could not load ${symbol}: ${err.message}`, extra);
      body.replaceChildren(box);
    }
  }

  function renderDetail(a) {
    $("detail-title").textContent = `#${a.universe_rank} ${a.symbol} · ${a.name}`;
    const g = a.integrity;
    const parts = [];

    if (!a.supported) {
      const box = h("div", { class: "alert" });
      setMessage(box, "No supported spot market", [a.unsupported_reason || "unsupported"]);
      parts.push(box);
    }

    const components = Object.entries(g.components).map(([k, v]) => kv(humanize(k), `${Math.round(v * 100)}%`));
    parts.push(
      card(
        "Integrity gate",
        `checked ${fmtTime(g.checked_at)}`,
        h(
          "div",
          { class: "gate-summary" },
          badge(g.decision, g.decision, "large"),
          badge(g.state),
          h("div", {}, h("span", { class: "score", text: g.data_health_score }), h("span", { class: "unit", text: " / 100 health" })),
        ),
        h("dl", { class: "detail-grid" }, components),
        h(
          "ol",
          { class: "stages" },
          g.stages.map((s) =>
            h(
              "li",
              {},
              h(
                "div",
                { class: "stage-row" },
                badge(s.passed ? "PASS" : "NO TRADE", humanize(s.stage.replace(/_CHECK$/, "")), "stage-name"),
                badge(s.state),
              ),
              s.reasons.length ? h("ul", {}, s.reasons.map((r) => h("li", { text: r }))) : null,
            ),
          ),
        ),
      ),
    );

    const t = a.ticker;
    const c = a.cross_check;
    parts.push(
      card(
        "Price",
        t ? `${t.source} ${t.symbol}` : "no live ticker",
        h(
          "dl",
          { class: "detail-grid" },
          kv("Last price", t ? `${fmtPrice(t.last_price)} ${t.quote_asset}` : DASH),
          kv("Bid / ask", t ? `${fmtPrice(t.bid)} / ${fmtPrice(t.ask)}` : DASH),
          kv("24h high / low", t ? `${fmtPrice(t.high_24h)} / ${fmtPrice(t.low_24h)}` : DASH),
          kv("24h change", t ? delta(t.pct_change_24h) : DASH),
          kv(`Reference (${a.listing.source})`, `$${fmtPrice(a.listing.price_usd)}`),
          kv("Reference 1h / 7d", h("span", {}, delta(a.listing.pct_change_1h), " / ", delta(a.listing.pct_change_7d))),
          kv("Cross-check", c ? badge(c.status) : DASH),
          kv("Deviation", c && c.deviation_pct != null ? `${c.deviation_pct.toFixed(3)}%` : DASH),
        ),
        c ? h("div", { class: "card-foot muted small", text: c.reason }) : null,
      ),
    );

    parts.push(
      card(
        "Candles",
        "closed candles only; the forming candle is excluded",
        table(
          [["Timeframe"], ["Source"], ["Closed", "num"], ["Complete", "num"], ["Missing", "num"], ["Last close age", "num"], ["Status"]],
          a.timeframes.map((tf) =>
            h(
              "tr",
              {},
              h("td", {}, h("strong", { text: tf.label })),
              h("td", { class: "muted", text: tf.source || DASH }),
              h("td", { class: "num", text: fmtInt(tf.closed) }),
              h("td", { class: "num", text: `${tf.completeness_pct.toFixed(1)}%` }),
              h("td", { class: "num", text: fmtInt(tf.missing_candles) }),
              h("td", { class: "num", text: fmtAge(tf.last_closed_age_seconds) }),
              h(
                "td",
                {},
                badge(tf.ok ? "PASS" : "NO TRADE", tf.ok ? (tf.issues.length ? "OK with notes" : "OK") : "Failed"),
                [tf.error, ...tf.critical_issues, ...tf.issues.filter((i) => !tf.critical_issues.includes(i))]
                  .filter(Boolean)
                  .map((i) => h("span", { class: "reason", text: i })),
              ),
            ),
          ),
        ),
      ),
    );

    if (a.candle_cross_checks.length) {
      parts.push(
        card(
          "Candle history cross-check",
          "exchange candles vs CoinMarketCap OHLCV",
          table(
            [["Timeframe"], ["Status"], ["Compared", "num"], ["Median dev.", "num"], ["Max dev.", "num"]],
            a.candle_cross_checks.map((x) =>
              h(
                "tr",
                {},
                h("td", {}, h("strong", { text: x.label })),
                h("td", {}, badge(x.status), h("span", { class: "reason", text: x.reason })),
                h("td", { class: "num", text: fmtInt(x.compared) }),
                h("td", { class: "num", text: x.median_deviation_pct == null ? DASH : `${x.median_deviation_pct.toFixed(3)}%` }),
                h("td", { class: "num", text: x.max_deviation_pct == null ? DASH : `${x.max_deviation_pct.toFixed(3)}%` }),
              ),
            ),
          ),
        ),
      );
    }

    const ob = a.order_book;
    const v = a.volatility;
    parts.push(
      card(
        "Order book and volatility",
        ob ? `${ob.source} ${ob.symbol}` : null,
        h(
          "dl",
          { class: "detail-grid" },
          kv("Order book", ob ? badge(ob.valid ? "PASS" : "NO TRADE", ob.valid ? "Valid" : "Invalid") : DASH),
          kv("Spread", ob && ob.spread_bps != null ? `${ob.spread_bps.toFixed(1)} bps` : DASH),
          kv(`Bid depth ±${ob ? ob.band_pct : 1}%`, ob ? fmtNum(ob.bid_depth_quote) : DASH),
          kv(`Ask depth ±${ob ? ob.band_pct : 1}%`, ob ? fmtNum(ob.ask_depth_quote) : DASH),
          kv("Imbalance", ob && ob.imbalance != null ? fmtPct(ob.imbalance * 100, 1) : DASH),
          kv("Volatility", v ? badge(v.extreme ? "EXTREME_VOLATILITY" : v.available ? "HEALTHY" : "UNVERIFIED", v.extreme ? "Extreme" : v.available ? "Normal" : "Unknown") : DASH),
          kv("Vol. vs normal", v && v.ratio != null ? `${v.ratio.toFixed(2)}×` : DASH),
          kv("Recent move", v && v.move_pct != null ? fmtPct(v.move_pct) : DASH),
        ),
        [...(ob ? ob.issues : []), v ? v.reason : null]
          .filter(Boolean)
          .map((text) => h("div", { class: "card-foot muted small", text })),
      ),
    );

    if (a.errors.length) {
      const box = h("div", { class: "alert" });
      setMessage(box, "Collection errors", a.errors);
      parts.push(box);
    }

    parts.push(
      h(
        "p",
        { class: "muted small" },
        `Generated ${fmtTime(a.generated_at)} · `,
        h("a", { href: `/api/assets/${encodeURIComponent(a.symbol)}`, target: "_blank", rel: "noopener", text: "raw JSON" }),
      ),
    );
    $("detail-body").replaceChildren(...parts);
  }

  // ---------------------------------------------------------------- theme

  function currentTheme() {
    const forced = document.documentElement.dataset.theme;
    if (forced) return forced;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function applyTheme(theme) {
    if (theme) document.documentElement.dataset.theme = theme;
    $("theme").textContent = currentTheme() === "dark" ? "Light mode" : "Dark mode";
  }

  // ---------------------------------------------------------------- start

  document.addEventListener("DOMContentLoaded", () => {
    let saved = null;
    try {
      saved = localStorage.getItem("theme");
    } catch {
      saved = null;
    }
    applyTheme(saved === "light" || saved === "dark" ? saved : null);
    $("theme").addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        localStorage.setItem("theme", next);
      } catch {
        /* storage unavailable: theme lasts for this page view */
      }
    });

    $("refresh").addEventListener("click", refresh);
    $("detail-close").addEventListener("click", closeDetail);
    dialog().addEventListener("close", () => {
      detailToken++;
      if (location.hash) history.replaceState(null, "", location.pathname + location.search);
    });
    dialog().addEventListener("click", (e) => {
      if (e.target === dialog()) closeDetail();
    });

    refresh();
    schedule();
    const hash = decodeURIComponent(location.hash.slice(1));
    if (/^[A-Za-z0-9]{1,20}$/.test(hash)) openDetail(hash.toUpperCase());
  });
})();
