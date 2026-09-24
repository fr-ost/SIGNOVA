// Status dashboard: Phase 1 market data and fail-safe checks, Phase 2 signals and analysis.
// Renders the JSON API with no build step and no dependencies. Every provider-supplied
// string is inserted with textContent, never as HTML.
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
  const SIGNALS_TIMEOUT_MS = 120000;
  const SIGNAL_LABELS = ["STRONG BUY", "BUY", "WATCH", "NO TRADE"];
  const SIGNAL_TONE = { "STRONG BUY": "good", BUY: "good", WATCH: "warning", "NO TRADE": "neutral" };
  const OUTCOME_TONE = { PASS: "good", CAP: "warning", DOWNGRADE: "warning", BLOCK: "critical", NOT_RUN: "neutral" };
  const REGIME_TONE = { BULL: "good", NEUTRAL: "warning", BEAR: "critical", UNKNOWN: "neutral" };

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

  function toneBadge(tone, label, extraClass) {
    return h(
      "span",
      { class: `badge tone-${tone}${extraClass ? " " + extraClass : ""}` },
      h("span", { class: "badge-icon", "aria-hidden": "true", text: ICON[tone] }),
      label,
    );
  }

  const badge = (value, label, extraClass) => toneBadge(TONE[value] || "neutral", label ?? humanize(value), extraClass);
  const signalBadge = (label, text, extraClass) => toneBadge(SIGNAL_TONE[label] || "neutral", text ?? humanize(label), extraClass);

  function meter(value, max = 100) {
    const fill = h("span", { class: "meter-fill" });
    fill.style.width = `${Math.max(0, Math.min(100, (value / max) * 100))}%`; // CSSOM, allowed by the CSP
    return h("span", { class: "meter", "aria-hidden": "true" }, fill);
  }

  const plainList = (items, cls) => h("ul", { class: `plain-list${cls ? " " + cls : ""}` }, items.map((t) => h("li", { text: t })));

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

  async function getJSON(url, timeoutMs = REQUEST_TIMEOUT_MS) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
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

  // ---------------------------------------------------------------- signals (Phase 2)

  function signalRow(row) {
    const actionable = row.signal === "BUY" || row.signal === "STRONG BUY";
    const planCls = `num hide-sm${actionable ? "" : " muted"}`;
    const open = () => openDetail(row.symbol);
    const stopPct = row.stop_loss != null && row.entry_high ? ((row.entry_high - row.stop_loss) / row.entry_high) * 100 : null;
    return h(
      "tr",
      { class: "clickable", onclick: open },
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
            "aria-label": `Open the analysis for ${row.symbol}`,
            onclick: (e) => { e.stopPropagation(); open(); },
          }),
          h("span", { class: "muted small", text: row.name }),
        ),
      ),
      h("td", {}, signalBadge(row.signal)),
      h("td", {}, h("div", { class: "score-cell" }, h("span", { class: "num", text: row.score }), meter(row.score))),
      h("td", { class: "hide-sm", text: humanize(row.trend) }),
      h("td", { class: planCls, title: actionable ? null : "WATCH plan: not a buy signal" },
        row.entry_low != null ? [fmtPrice(row.entry_low), h("div", { class: "small muted", text: `to ${fmtPrice(row.entry_high)}` })] : DASH),
      h("td", { class: planCls },
        row.stop_loss != null ? [fmtPrice(row.stop_loss), h("div", { class: "small muted", text: `−${stopPct.toFixed(1)}%` })] : DASH),
      h("td", { class: planCls },
        row.take_profit_1 != null ? [fmtPrice(row.take_profit_1), h("div", { class: "small muted", text: fmtPrice(row.take_profit_2) })] : DASH),
      h("td", { class: planCls, text: row.reward_risk != null ? `${row.reward_risk.toFixed(1)}R` : DASH }),
      h("td", { class: "num hide-sm", text: row.suggested_allocation_pct != null ? `${row.suggested_allocation_pct.toFixed(1)}%` : DASH }),
      h("td", {}, h("span", { class: "reason why", title: row.reasons.join("\n") || null, text: row.reasons[0] || row.summary })),
    );
  }

  function renderSignals(scan) {
    const m = scan.market_regime;
    $("regime-state").replaceChildren(toneBadge(REGIME_TONE[m.regime] || "neutral", `Market: ${humanize(m.regime)}`));
    const breadth = m.breadth_pct == null ? "breadth n/a" : `${Math.round(m.breadth_pct)}% above daily EMA50`;
    $("signals-meta").textContent =
      `${humanize(m.regime)} market, signals up to ${humanize(m.max_signal)} · BTC daily ${m.btc_trend ? m.btc_trend.toLowerCase() : "n/a"} · ${breadth} · scanned ${fmtTime(scan.generated_at)}`;
    $("signal-counts").replaceChildren(
      ...SIGNAL_LABELS.map((label) => signalBadge(label, `${humanize(label)} ${scan.counts[label] || 0}`, "chip")),
    );
    const rows = scan.signals.map(signalRow);
    $("signal-rows").replaceChildren(
      ...(rows.length ? rows : [h("tr", {}, h("td", { colspan: 10, class: "empty", text: "No assets to analyse." }))]),
    );
    const box = $("signals-error");
    if (scan.errors.length) setMessage(box, "Some assets could not be analysed", scan.errors.slice(0, 5));
    else box.hidden = true;
  }

  let signalsLoading = false;

  async function refreshSignals() {
    if (signalsLoading) return;
    signalsLoading = true;
    try {
      renderSignals(await getJSON("/api/signals", SIGNALS_TIMEOUT_MS));
    } catch (err) {
      setMessage($("signals-error"), "Signals unavailable", [err.message]);
      if ($("signal-rows").querySelector(".empty")) {
        $("signal-rows").replaceChildren(h("tr", {}, h("td", { colspan: 10, class: "empty", text: "Signals unavailable." })));
      }
    } finally {
      signalsLoading = false;
    }
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

  function refreshAll() {
    refresh();
    refreshSignals();
  }

  function schedule() {
    clearInterval(timer);
    timer = null;
    if (document.visibilityState === "visible") timer = setInterval(refreshAll, REFRESH_MS);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshAll();
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
      h("p", { class: "muted", text: `Analysing ${symbol}: 5 timeframes of candles, order book, cross-checks and the signal engine…` }),
    );
    if (!d.open) d.showModal();
    if (location.hash !== `#${symbol}`) history.replaceState(null, "", `#${symbol}`);
    const path = `/api/assets/${encodeURIComponent(symbol)}`;
    const [analysis, detail] = await Promise.allSettled([getJSON(`${path}/analysis`, SIGNALS_TIMEOUT_MS), getJSON(path)]);
    if (token !== detailToken) return;
    const failure = (title, err) => {
      const box = h("div", { class: "alert", role: "alert" });
      const extra = err.body && Array.isArray(err.body.errors) ? err.body.errors : [];
      setMessage(box, `${title}: ${err.message}`, extra);
      return box;
    };
    const parts = [];
    const head = analysis.status === "fulfilled" ? analysis.value : detail.status === "fulfilled" ? detail.value : null;
    if (head) $("detail-title").textContent = `#${head.universe_rank} ${head.symbol} · ${head.name}`;
    if (analysis.status === "fulfilled") parts.push(...renderAnalysis(analysis.value));
    else parts.push(failure(`Analysis of ${symbol} unavailable`, analysis.reason));
    if (detail.status === "fulfilled") parts.push(...renderDetail(detail.value));
    else if (analysis.status === "fulfilled") parts.push(failure("Data collection details unavailable", detail.reason));
    $("detail-body").replaceChildren(...parts);
  }

  // Phase 2: signal, plan, reasons, score, pipeline, risk checks, indicators, structure.
  function planCard(plan) {
    const q = plan.quote_asset;
    const targets = table(
      [["Target"], ["Price", "num"], ["R", "num"], ["Sell", "num"], ["Basis", "hide-sm"]],
      plan.targets.map((t, i) =>
        h(
          "tr",
          {},
          h("td", {}, h("strong", { text: `TP${i + 1}` })),
          h("td", { class: "num", text: fmtPrice(t.price) }),
          h("td", { class: "num", text: `${t.r_multiple.toFixed(2)}R` }),
          h("td", { class: "num", text: `${Math.round(t.allocation_pct)}%` }),
          h("td", { class: "hide-sm muted", text: t.basis }),
        ),
      ),
    );
    const notes = [
      `Stop: ${plan.stop_basis}.`,
      `Invalidation: ${plan.invalidation}.`,
      plan.better_entry_below != null ? `Reward:risk reaches the minimum at an entry of ${fmtPrice(plan.better_entry_below)} or lower.` : null,
      ...plan.notes.map((n) => `Note: ${n}.`),
    ].filter(Boolean);
    return card(
      plan.actionable ? "Trade plan" : "Watch plan (not a buy signal)",
      `${q} · spot only`,
      h(
        "dl",
        { class: "detail-grid" },
        kv("Entry zone", `${fmtPrice(plan.entry_low)} to ${fmtPrice(plan.entry_high)}`),
        kv("Stop loss", `${fmtPrice(plan.stop_loss)} (−${plan.stop_distance_pct.toFixed(2)}%)`),
        kv("Reward:risk (net)", `${plan.reward_risk.toFixed(2)}R to TP2 · ${plan.reward_risk_tp1.toFixed(2)}R to TP1`),
        kv("Room to resistance", plan.room_to_resistance_r == null
          ? "none overhead"
          : `${plan.room_to_resistance_r.toFixed(2)}R (${fmtPrice(plan.nearest_resistance)})`),
        kv("Suggested size", plan.actionable
          ? `${plan.suggested_allocation_pct.toFixed(1)}% of portfolio (risks ${plan.risk_at_allocation_pct.toFixed(2)}%)`
          : DASH),
        kv("Costs", `${plan.cost_pct.toFixed(2)}% round trip (fees + slippage)`),
      ),
      targets,
      h("div", { class: "card-foot small muted" }, notes.map((n) => h("div", { text: n }))),
    );
  }

  function factorList(factors) {
    return h(
      "div",
      { class: "factors" },
      factors.map((f) =>
        h(
          "div",
          { class: "factor" },
          h(
            "div",
            { class: "factor-head" },
            h("span", { class: "stage-name", text: f.name }),
            h("span", { class: "num", text: `${Math.round(f.score * 10) / 10} / ${f.max_score}` }),
          ),
          meter(f.score, f.max_score),
          f.positives.length ? plainList(f.positives.map((t) => `+ ${t}`), "small") : null,
          f.negatives.length ? plainList(f.negatives.map((t) => `− ${t}`), "small muted") : null,
        ),
      ),
    );
  }

  const outcomeLabel = { PASS: "Pass", CAP: "Caps at Watch", DOWNGRADE: "Caps at Buy", BLOCK: "Blocks", NOT_RUN: "Not run" };
  const severityOutcome = { block: "BLOCK", cap: "CAP", downgrade: "DOWNGRADE" };

  function renderAnalysis(a) {
    const regimeByTf = Object.fromEntries(a.regimes.map((r) => [r.timeframe, r]));
    const parts = [];
    parts.push(
      h(
        "section",
        { class: "card" },
        h(
          "div",
          { class: "signal-headline" },
          signalBadge(a.signal, null, "large"),
          h("div", {}, h("span", { class: "score", text: a.score }), h("span", { class: "unit", text: " / 100 score" })),
          h("span", { class: "muted small", text: `Trend ${humanize(a.trend).toLowerCase()} · ${a.setup_timeframe} setup · ${humanize(a.market_regime.regime).toLowerCase()} market` }),
        ),
        h("p", { class: "signal-summary", text: a.summary }),
      ),
    );
    if (a.plan) parts.push(planCard(a.plan));
    parts.push(
      card(
        "Why",
        a.signal === "BUY" || a.signal === "STRONG BUY" ? "supporting evidence" : "what holds it back, then what supports it",
        plainList(a.reasons),
        a.risks.length ? h("div", { class: "card-foot" }, h("strong", { class: "small", text: "Risks to keep in mind" }), plainList(a.risks, "small")) : null,
      ),
    );
    parts.push(card("Score breakdown", `${a.score}/100 · ${humanize(a.score_label)} before risk and data checks`, factorList(a.factors)));
    parts.push(
      card(
        "Signal pipeline",
        "a block anywhere means NO TRADE",
        h(
          "ol",
          { class: "stages" },
          a.pipeline.map((s) =>
            h(
              "li",
              {},
              h(
                "div",
                { class: "stage-row" },
                toneBadge(OUTCOME_TONE[s.outcome] || "neutral", humanize(s.stage.replace(/_CHECK$/, "")), "stage-name"),
                toneBadge(OUTCOME_TONE[s.outcome] || "neutral", outcomeLabel[s.outcome] || s.outcome),
              ),
              s.reasons.length ? h("ul", {}, s.reasons.map((r) => h("li", { text: r }))) : null,
            ),
          ),
        ),
      ),
    );
    parts.push(
      card(
        "Risk checks",
        null,
        table(
          [["Check"], ["Result"], ["Detail", "hide-sm"]],
          a.risk_checks.map((c) =>
            h(
              "tr",
              {},
              h("td", { text: c.name }),
              h("td", {}, c.passed ? toneBadge("good", "Pass") : toneBadge(OUTCOME_TONE[severityOutcome[c.severity]], outcomeLabel[severityOutcome[c.severity]])),
              h("td", { class: "hide-sm muted", text: c.detail }),
            ),
          ),
        ),
      ),
    );
    const num = (v, d = 2) => (v == null ? DASH : v.toFixed(d));
    parts.push(
      card(
        "Indicators",
        "last closed candle per timeframe",
        table(
          [["TF"], ["Close", "num"], ["EMA20", "num hide-sm"], ["EMA50", "num hide-sm"], ["EMA200", "num hide-sm"], ["RSI", "num"],
            ["MACD hist", "num hide-sm"], ["ADX", "num hide-sm"], ["ATR %", "num hide-sm"], ["%B", "num hide-sm"], ["Regime"]],
          a.indicators.map((i) => {
            const r = regimeByTf[i.timeframe];
            return h(
              "tr",
              {},
              h("td", {}, h("strong", { text: i.label })),
              h("td", { class: "num", text: fmtPrice(i.close) }),
              h("td", { class: "num hide-sm", text: fmtPrice(i.ema20) }),
              h("td", { class: "num hide-sm", text: fmtPrice(i.ema50) }),
              h("td", { class: "num hide-sm", text: fmtPrice(i.ema200) }),
              h("td", { class: "num", text: num(i.rsi14, 0) }),
              h("td", { class: "num hide-sm", text: i.macd_hist == null ? DASH : fmtPrice(i.macd_hist) }),
              h("td", { class: "num hide-sm", text: num(i.adx14, 0) }),
              h("td", { class: "num hide-sm", text: num(i.atr_pct) }),
              h("td", { class: "num hide-sm", text: num(i.bb_pct_b) }),
              h("td", { class: "small", text: r ? humanize(r.regime) : DASH }),
            );
          }),
        ),
      ),
    );
    const levels = (list) => (list.length ? list.slice(0, 2).map((l) => fmtPrice(l.price)).join(", ") : DASH);
    parts.push(
      card(
        "Market structure",
        "confirmed swing points; levels nearest first",
        table(
          [["TF"], ["Structure"], ["Last break", "hide-sm"], ["Support"], ["Resistance"]],
          a.structure.map((s) =>
            h(
              "tr",
              {},
              h("td", {}, h("strong", { text: s.label })),
              h("td", { title: s.reason }, humanize(s.trend)),
              h("td", { class: "hide-sm small" },
                s.last_break ? `${s.last_break.direction === "UP" ? "Above" : "Below"} ${fmtPrice(s.last_break.level)}, ${s.last_break.candles_ago} candles ago` : DASH),
              h("td", { class: "num", text: levels(s.supports) }),
              h("td", { class: "num", text: levels(s.resistances) }),
            ),
          ),
        ),
      ),
    );
    parts.push(
      h(
        "p",
        { class: "muted small" },
        `${a.disclaimer} Engine ${a.engine_version} · generated ${fmtTime(a.generated_at)} · `,
        h("a", { href: `/api/assets/${encodeURIComponent(a.symbol)}/analysis`, target: "_blank", rel: "noopener", text: "raw JSON" }),
      ),
    );
    return parts;
  }

  // Phase 1: data integrity, price, candles, cross-checks, order book, volatility.
  function renderDetail(a) {
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
        "Data integrity gate",
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
        `Data collected ${fmtTime(a.generated_at)} · `,
        h("a", { href: `/api/assets/${encodeURIComponent(a.symbol)}`, target: "_blank", rel: "noopener", text: "raw JSON" }),
      ),
    );
    return parts;
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

    $("refresh").addEventListener("click", refreshAll);
    $("detail-close").addEventListener("click", closeDetail);
    dialog().addEventListener("close", () => {
      detailToken++;
      if (location.hash) history.replaceState(null, "", location.pathname + location.search);
    });
    dialog().addEventListener("click", (e) => {
      if (e.target === dialog()) closeDetail();
    });

    refreshAll();
    schedule();
    const hash = decodeURIComponent(location.hash.slice(1));
    if (/^[A-Za-z0-9]{1,20}$/.test(hash)) openDetail(hash.toUpperCase());
  });
})();
