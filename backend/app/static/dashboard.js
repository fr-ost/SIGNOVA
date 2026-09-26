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

  function kv(label, value, cls) {
    return h("div", { class: cls ? `kv ${cls}` : "kv" }, h("dt", { text: label }), h("dd", {}, value ?? DASH));
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

  function storedToken() {
    try {
      return localStorage.getItem("adminToken") || "";
    } catch {
      return "";
    }
  }

  function pref(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, value);
    } catch {
      /* storage unavailable: preference lasts for this page view */
    }
    return value ?? null;
  }

  function askToken() {
    const value = window.prompt("This action needs the admin token (ADMIN_TOKEN on the server):", "");
    if (value == null) return false;
    try {
      localStorage.setItem("adminToken", value.trim());
    } catch {
      /* storage unavailable */
    }
    return true;
  }

  async function api(url, { method = "GET", body, timeoutMs = REQUEST_TIMEOUT_MS, retried = false } = {}) {
    try {
      return await getJSON(url, timeoutMs, method, body);
    } catch (err) {
      if (err.status === 401 && !retried && askToken()) return api(url, { method, body, timeoutMs, retried: true });
      throw err;
    }
  }

  async function getJSON(url, timeoutMs = REQUEST_TIMEOUT_MS, method = "GET", body = undefined) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const headers = { Accept: "application/json" };
      const token = storedToken();
      if (token) headers["X-Admin-Token"] = token;
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const res = await fetch(url, {
        method, headers, signal: ctrl.signal, body: body === undefined ? undefined : JSON.stringify(body),
      });
      let payload = null;
      try {
        payload = await res.json();
      } catch {
        payload = null;
      }
      if (!res.ok) {
        const detail = payload && typeof payload.detail === "string" ? payload.detail : `HTTP ${res.status}`;
        const err = new Error(detail);
        err.status = res.status;
        err.body = payload;
        throw err;
      }
      return payload;
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
      { class: `clickable${row.supported ? "" : " unsupported"}${selectionState && !isSelected(row.symbol) ? " deselected" : ""}`,
        onclick: open, title: selectionState && !isSelected(row.symbol) ? "Not in your analysis selection" : null },
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
          row.watchlist ? h("button", {
            type: "button", class: "remove tiny", text: "Remove from watchlist",
            onclick: (e) => { e.stopPropagation(); removeWatch(row.symbol); },
          }) : null,
        ),
      ),
      h(
        "td",
        { class: "num" },
        h("span", { "data-live-price": row.symbol, text: fmtPrice(row.price) }),
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

    setChatSymbols(m.assets.filter((a) => a.supported).map((a) => a.symbol));
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
    loadSelection(); // the universe may have changed (no provider calls)
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
        if (cmc.unsupported_endpoints && cmc.unsupported_endpoints.length) {
          text += ` · skipped (not in your plan, retried daily): ${cmc.unsupported_endpoints.join(", ")}`;
        }
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
          h("span", { class: "muted small" }, row.name, row.watchlist ? h("span", { class: "watch-tag", text: "watchlist" }) : null),
        ),
      ),
      h("td", {}, signalBadge(row.signal), aiChip(row.ai_review), evidenceChip(row.evidence_score, row.evidence_grade)),
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
      const scan = await getJSON("/api/signals");
      if (scan) renderSignals(scan);
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
    button.textContent = "Refresh data";
    loading = false;
  }

  function refreshAll() {
    refresh();
    refreshSignals();
  }

  const autoRefreshOn = () => $("auto-refresh").checked;

  function schedule() {
    clearInterval(timer);
    timer = null;
    if (document.visibilityState === "visible" && autoRefreshOn() && !emergency) timer = setInterval(refresh, REFRESH_MS);
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && autoRefreshOn()) refresh();
    schedule();
  });

  // ---------------------------------------------------------------- asset detail

  const dialog = () => $("detail");
  let detailToken = 0;

  function closeDetail() {
    const d = dialog();
    if (d.open) d.close();
  }

  let detailSymbol = null;

  async function openDetail(symbol) {
    const d = dialog();
    const token = ++detailToken;
    detailSymbol = symbol;
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

  function swingReview(a) {
    const box = h("div", {}, a.ai_review ? aiReviewBlock(a.ai_review) : null);
    const btn = h("button", { type: "button", class: "small", text: a.ai_review ? "Review again with AI" : "AI review of this signal",
      title: "Ask the OpenAI model for a second opinion (uses your OpenAI credits; it can only lower the signal)" });
    btn.addEventListener("click", () => requestReview("swing", a.symbol, null, box, btn));
    return h("div", { class: "pad review-row-wrap" }, h("div", { class: "review-row" }, btn), box);
  }

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
        heldBackNote(a.filtered_by, a.would_be),
        swingReview(a),
      ),
    );
    parts.push(chartCard(a.symbol, a.plan));
    if (a.plan) parts.push(planCard(a.plan));
    if (a.sentiment) parts.push(sentimentCard(a.sentiment));
    parts.push(
      card(
        "Why",
        a.signal === "BUY" || a.signal === "STRONG BUY" ? "supporting evidence" : "what holds it back, then what supports it",
        plainList(a.reasons),
        a.risks.length ? h("div", { class: "card-foot" }, h("strong", { class: "small", text: "Risks to keep in mind" }), plainList(a.risks, "small")) : null,
      ),
    );
    const ev = a.evidence;
    parts.push(card("Evidence board",
      ev ? `${signedNum(ev.score)} · ${ev.vetoes.length ? "veto" : GRADE_TEXT[ev.grade] || ev.grade} · futures, order flow, news, market` : "not computed",
      evidenceBlock(ev, { entry: a.plan ? a.plan.entry_high : null, stop: a.plan ? a.plan.stop_loss : null,
        tp2: a.plan && a.plan.targets[1] ? a.plan.targets[1].price : null })));
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

  // ---------------------------------------------------------------- controls (Phase 3)

  let controlTimer = null;
  let liveTimer = null;
  let lastScanAt = null;
  let controlState = null;

  function describeStatus(s) {
    const scan = s.scan;
    const parts = [];
    if (scan.running) parts.push(`Analysing… ${scan.done}/${scan.total || "?"}`);
    else if (scan.outcome === "completed") parts.push(`Last scan ${fmtTime(scan.finished_at)}${scan.trigger === "auto" ? " (auto)" : ""}`);
    else if (scan.outcome === "stopped") parts.push(`Stopped ${fmtTime(scan.finished_at)}`);
    else if (scan.outcome === "failed") parts.push(`Scan failed: ${scan.error || "unknown error"}`);
    else parts.push("Idle: no scan yet");
    parts.push(s.auto_minutes ? `auto every ${s.auto_minutes} min (next ${fmtTime(s.next_auto_at)})` : "auto off");
    if (s.live.running) parts.push(`live prices on (${s.live.symbols} coins)`);
    return parts.join(" · ");
  }

  let emergency = false;

  function applyEmergency(st) {
    const on = !!(st && st.active);
    const changed = on !== emergency;
    emergency = on;
    document.body.classList.toggle("killed", on);
    $("kill-banner").hidden = !on;
    $("kill").hidden = on;
    if (on) {
      $("kill-text").textContent = `Since ${fmtTime(st.since)}: all analysis, live prices, news and AI calls are halted. Stored results stay readable.`;
      clearInterval(timer);
      timer = null;
      if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
    }
    for (const id of ["analyze", "live", "auto-analyze", "scalp-scan", "refresh", "news-refresh", "mood-refresh", "events-refresh", "lab-run", "ev-market", "futures-scan"]) {
      const el = $(id);
      if (el) el.disabled = on || (id === "analyze" && controlState && controlState.scan.running);
    }
    return changed;
  }

  async function killAll() {
    if (!window.confirm("Emergency stop: halt all scans, auto-analyze, live prices, news, on-chain and AI calls now? Nothing restarts until you press Resume.")) return;
    try {
      renderControl(await api("/api/control/kill", { method: "POST", body: { reason: "dashboard emergency stop" } }));
    } catch (err) {
      window.alert(`Emergency stop failed: ${err.message}`);
    }
  }

  async function resumeAll() {
    try {
      renderControl(await api("/api/control/resume", { method: "POST" }));
      refreshAll();
      schedule();
    } catch (err) {
      window.alert(`Resume failed: ${err.message}`);
    }
  }

  function renderControl(s) {
    controlState = s;
    $("control-status").textContent = describeStatus(s);
    $("analyze").disabled = s.scan.running;
    $("analyze").textContent = s.scan.running ? "Analysing…" : "Analyze now";
    $("live").textContent = s.live.running ? "Stop live prices" : "Start live prices";
    $("auto-analyze").value = String(s.auto_minutes || 0);
    if (![...$("auto-analyze").options].some((o) => o.value === String(s.auto_minutes))) $("auto-analyze").value = "0";
    const pct = s.scan.running && s.scan.total ? (s.scan.done / s.scan.total) * 100 : 0;
    $("scan-progress").style.width = `${pct}%`;
    $("unlock").hidden = !s.auth_required || !!storedToken();
    if (s.last_scan_at && s.last_scan_at !== lastScanAt) {
      lastScanAt = s.last_scan_at;
      refreshSignals();
      refreshPortfolio();
      refreshRecord();
      if (moodLoaded) refreshMood();  // the scan refreshed sentiment; this reads the cache
      if (unlocksLoaded) refreshEvents(false, true);
    }
    if (s.scalp) renderScalpStatus(s.scalp);
    if (s.lab) renderLabStatus(s.lab);
    if (s.futures) renderFuturesStatus(s.futures);
    applyEmergency(s.emergency_stop);
    const scalpRunning = (s.scalp && Object.values(s.scalp).some((x) => x.running)) || (s.lab && Object.values(s.lab).some((x) => x.running))
      || (s.futures && Object.values(s.futures).some((x) => x.running));
    scheduleControl(s.scan.running || scalpRunning ? 2000 : 30000);
    if (s.live.running && !liveTimer) liveTimer = setInterval(refreshLive, 5000);
    if (!s.live.running && liveTimer) {
      clearInterval(liveTimer);
      liveTimer = null;
    }
  }

  async function refreshControl() {
    try {
      renderControl(await getJSON("/api/control/status"));
    } catch (err) {
      $("control-status").textContent = `Status unavailable: ${err.message}`;
      scheduleControl(30000);
    }
  }

  function scheduleControl(ms) {
    clearTimeout(controlTimer);
    controlTimer = setTimeout(refreshControl, ms);
  }

  async function control(path, body) {
    try {
      renderControl(await api(path, { method: "POST", body }));
    } catch (err) {
      $("control-status").textContent = `${err.message}`;
    }
  }

  async function refreshLive() {
    try {
      const live = await getJSON("/api/live");
      for (const [symbol, quote] of Object.entries(live.prices)) {
        for (const el of document.querySelectorAll(`[data-live-price="${symbol}"]`)) {
          const text = fmtPrice(quote.price);
          if (el.textContent !== text) {
            el.textContent = text;
            el.classList.remove("flash");
            void el.offsetWidth;
            el.classList.add("flash");
          }
        }
      }
    } catch {
      /* next poll retries */
    }
  }

  // ---------------------------------------------------------------- watchlist

  async function removeWatch(symbol) {
    if (!window.confirm(`Remove ${symbol} from the watchlist? It will no longer be analysed.`)) return;
    try {
      renderWatchlist((await api(`/api/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" })).items);
      loadSelection();
      if (!emergency) refresh();
    } catch (err) {
      window.alert(err.message);
    }
  }

  function renderWatchlist(items) {
    $("watch-items").replaceChildren(
      ...(items.length
        ? items.map((item) =>
            h(
              "li",
              { title: item.note || null },
              h("strong", { text: item.symbol }),
              item.note ? h("span", { class: "muted small", text: item.note }) : null,
              h("span", { class: "spacer" }),
              h("button", {
                type: "button", class: "remove small", "aria-label": `Remove ${item.symbol} from the watchlist`,
                text: "Remove", onclick: () => removeWatch(item.symbol),
              }),
            ),
          )
        : [h("li", { class: "muted small", text: "Add any coin (for example PEPE). It is analysed on the next scan." })]),
    );
  }

  // ---------------------------------------------------------------- news

  const SENTIMENT_TONE = { positive: "good", negative: "critical", neutral: "neutral" };

  async function refreshNews(force = false) {
    const list = $("news-list");
    const button = $("news-refresh");
    button.disabled = true;
    button.classList.add("busy");
    if (!list.querySelector("a")) list.replaceChildren(...skeletonItems(4));
    try {
      const d = await getJSON(`/api/news${force ? "?refresh=true" : ""}`, 60000);
      const counts = d.sentiment || {};
      $("news-meta").replaceChildren(
        h("span", { text: `${d.items.length} headlines from ${d.sources_ok.length} sources · ${fmtTime(d.fetched_at)}` }),
        h("span", { text: `tone (keyword): ${counts.positive || 0} positive, ${counts.negative || 0} negative` }),
        ...(d.errors.length ? [h("span", { title: d.errors.join("\n"), text: `${d.errors.length} source(s) unavailable` })] : []),
      );
      $("trending").replaceChildren(
        ...(d.trending.length ? [h("span", { class: "muted small", text: "Trending on CoinGecko:" })] : []),
        ...d.trending.slice(0, 10).map((t) =>
          h("span", { class: "chip-remove", title: t.name }, t.symbol,
            t.price_change_24h_pct != null ? h("span", { class: t.price_change_24h_pct >= 0 ? "pnl-up small" : "pnl-down small", text: ` ${fmtPct(t.price_change_24h_pct, 1)}` }) : null),
        ),
      );
      list.replaceChildren(
        ...(d.items.length
          ? d.items.slice(0, 30).map((n) =>
              h(
                "li",
                {},
                h("a", { href: n.url, target: "_blank", rel: "noopener noreferrer", text: n.title }),
                h(
                  "div",
                  { class: "news-sub" },
                  h("span", { text: n.source }),
                  n.published_at ? h("span", { text: fmtAgo(n.published_at) }) : null,
                  n.sentiment !== "neutral" ? toneBadge(SENTIMENT_TONE[n.sentiment], humanize(n.sentiment)) : null,
                  n.assets.length ? h("span", { text: n.assets.join(" · ") }) : null,
                ),
              ),
            )
          : [h("li", { class: "muted small", text: "No headlines available right now." })]),
      );
    } catch (err) {
      list.replaceChildren(h("li", { class: "muted small", text: `News unavailable: ${err.message}` }));
    } finally {
      button.disabled = false;
      button.classList.remove("busy");
      button.textContent = "Refresh";
    }
  }

  function skeletonItems(n) {
    return Array.from({ length: n }, () => h("li", { class: "skeleton-row", "aria-hidden": "true" }, h("span", { class: "skeleton" }), h("span", { class: "skeleton short" })));
  }

  // ---------------------------------------------------------------- Phase 5: market mood, on-chain, whales

  const MOOD_TONE = { EXTREME_FEAR: "critical", FEAR: "serious", NEUTRAL: "neutral", GREED: "warning", EXTREME_GREED: "critical", UNKNOWN: "neutral" };
  const COIN_MOOD_TONE = { POSITIVE: "good", NEGATIVE: "critical", MIXED: "warning", QUIET: "neutral" };
  const FLOW_TONE = { exchange_inflow: "serious", exchange_outflow: "good", inter_exchange: "neutral", unknown: "neutral" };
  const FLOW_LABEL = { exchange_inflow: "To exchange", exchange_outflow: "From exchange", inter_exchange: "Between exchanges", unknown: "Unlabelled" };
  const fmtScore = (v) => (v == null ? DASH : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(2));

  function renderMood(s) {
    const c = s.components || {};
    const fg = c.fear_greed;
    const funding = c.funding;
    const news = c.news;
    const stables = c.stablecoins;
    $("mood-summary").replaceChildren(
      h(
        "div",
        { class: "mood-head" },
        toneBadge(MOOD_TONE[s.state] || "neutral", humanize(s.state)),
        h("span", { class: "small", text: `score ${fmtScore(s.score)} (−1 fear … +1 greed)` }),
        h("span", { class: "muted small", text: `trend ${humanize(s.trend).toLowerCase()}` }),
      ),
      h(
        "dl",
        { class: "detail-grid compact" },
        kv("Fear & Greed", fg ? `${fg.value} ${fg.classification} · 7d avg ${fg.avg_7d} (${fg.change_7d > 0 ? "+" : ""}${fg.change_7d})` : DASH),
        kv("Funding (avg)", funding ? `${funding.avg_pct_8h.toFixed(4)}% / 8h · ${funding.coins} coins` : DASH),
        kv("Crowded longs", funding ? (funding.crowded_longs.length ? funding.crowded_longs.join(", ") : "none") : DASH),
        kv("News tone 48h", news ? `${news.positive}+ / ${news.negative}− of ${news.headlines_48h}` : DASH),
        kv("Stablecoins 7d", stables ? `${fmtPct(stables.change_7d_pct)} · ${fmtUsd(stables.total_usd)}` : DASH),
      ),
      h("p", { class: "muted small", text: "Context only: sentiment adds risk notes to signals, it never changes a label or score." }),
    );
    return s.errors || [];
  }

  function renderOnchain(o) {
    const btc = o.btc || {};
    const eth = o.eth || {};
    const fees = btc.fees_sat_vb || {};
    const gas = eth.gas_gwei || {};
    const gwei = (v) => (v == null ? DASH : v.toFixed(v < 10 ? 2 : 0));
    $("onchain-summary").replaceChildren(
      h(
        "dl",
        { class: "detail-grid compact" },
        kv("BTC fees", fees.halfHourFee == null ? DASH : `${fees.fastestFee} / ${fees.halfHourFee} / ${fees.hourFee} sat/vB`),
        kv("BTC mempool", btc.mempool_tx_count == null ? DASH : `${fmtInt(btc.mempool_tx_count)} tx · ${btc.mempool_vsize_mb} MvB`),
        kv("Hashrate", btc.hashrate_ehs == null ? DASH : `${fmtInt(btc.hashrate_ehs)} EH/s`),
        kv("Next difficulty", btc.difficulty_change_pct == null ? DASH : `${fmtPct(btc.difficulty_change_pct)} in ${fmtInt(btc.blocks_to_retarget)} blocks`),
        kv("ETH gas", gas.average == null ? DASH : `${gwei(gas.slow)} / ${gwei(gas.average)} / ${gwei(gas.fast)} gwei`),
        kv("ETH usage", eth.network_utilization_pct == null ? DASH : `${eth.network_utilization_pct.toFixed(0)}% · ${fmtNum(eth.transactions_today)} tx today`),
      ),
    );
    const flows = Object.entries(o.flows || {});
    $("whale-count").textContent = `(${o.whales.length}${flows.length ? " · " + flows.map(([sym, f]) => `${sym} in ${fmtUsd(f.exchange_inflow)} / out ${fmtUsd(f.exchange_outflow)}`).join(" · ") : ""})`;
    $("whale-list").replaceChildren(
      ...(o.whales.length
        ? o.whales.slice(0, 25).map((w) =>
            h(
              "li",
              {},
              h(
                "div",
                { class: "whale-row" },
                h("strong", { text: `${fmtNum(w.amount)} ${w.symbol}` }),
                h("span", { class: "muted", text: fmtUsd(w.amount_usd) }),
                toneBadge(FLOW_TONE[w.classification] || "neutral", FLOW_LABEL[w.classification] || humanize(w.classification)),
              ),
              h(
                "div",
                { class: "news-sub" },
                h("span", { text: `${w.from_label || "unknown"} → ${w.to_label || "unknown"}` }),
                h("span", { text: fmtAgo(w.occurred_at) }),
                w.url && /^https:\/\//.test(w.url) ? h("a", { href: w.url, target: "_blank", rel: "noopener noreferrer", text: w.source }) : h("span", { text: w.source }),
              ),
            ),
          )
        : [h("li", { class: "muted small", text: "No large transfers in the latest data." })]),
    );
    return o.errors || [];
  }

  let moodLoading = false;
  let moodLoaded = false;

  async function refreshMood(force = false) {
    if (moodLoading) return;
    moodLoading = true;
    const button = $("mood-refresh");
    button.disabled = true;
    button.classList.add("busy");
    if (!moodLoaded) $("mood-summary").replaceChildren(h("div", { class: "pad" }, ...skeletonItems(3).map((li) => h("div", { class: "skeleton-row" }, ...li.childNodes))));
    const q = force ? "?refresh=true" : "";
    // on-chain first: sentiment reuses its cached result instead of fetching twice
    const onchain = await Promise.allSettled([getJSON(`/api/onchain${q}`, 60000)]);
    const sentiment = await Promise.allSettled([getJSON(`/api/sentiment${q}`, 60000)]);
    const errors = [];
    let stamp = null;
    if (sentiment[0].status === "fulfilled") {
      errors.push(...renderMood(sentiment[0].value));
      stamp = sentiment[0].value.computed_at;
    } else {
      $("mood-summary").replaceChildren(h("p", { class: "muted small", text: `Sentiment unavailable: ${sentiment[0].reason.message}` }));
    }
    let sources = [];
    if (onchain[0].status === "fulfilled") {
      errors.push(...renderOnchain(onchain[0].value));
      sources = onchain[0].value.sources_ok;
    } else {
      $("onchain-summary").replaceChildren(h("p", { class: "muted small", text: `On-chain data unavailable: ${onchain[0].reason.message}` }));
    }
    const unique = [...new Set(errors)];
    $("mood-meta").replaceChildren(
      h("span", { text: `${stamp ? fmtTime(stamp) + " · " : ""}sources: alternative.me, Binance funding, news${sources.length ? ", " + sources.join(", ") : ""}` }),
      ...(unique.length ? [h("span", { title: unique.join("\n"), text: `${unique.length} source(s) unavailable` })] : []),
    );
    button.disabled = false;
    button.classList.remove("busy");
    button.textContent = "Refresh";
    moodLoaded = true;
    moodLoading = false;
  }

  function sentimentCard(s) {
    const funding = s.funding_rate_pct;
    return card(
      "Sentiment & flows",
      "context only, never changes the signal",
      h(
        "dl",
        { class: "detail-grid" },
        kv("News (48h)", s.headlines ? h("span", {}, toneBadge(COIN_MOOD_TONE[s.state] || "neutral", humanize(s.state)), ` ${s.positive}+ / ${s.negative}− of ${s.headlines}`) : "no recent headlines"),
        kv("Funding rate", funding == null ? "no perpetual on Binance" : `${funding.toFixed(4)}% per 8h`),
        kv("Exchange flows", s.exchange_inflow_usd == null && s.exchange_outflow_usd == null
          ? "no labelled transfers"
          : `in ${fmtUsd(s.exchange_inflow_usd)} · out ${fmtUsd(s.exchange_outflow_usd)}`),
      ),
      s.notes.length ? plainList(s.notes, "small") : null,
      s.recent_titles.length ? h("div", { class: "card-foot small muted" }, s.recent_titles.map((t) => h("div", { text: t }))) : null,
    );
  }

  // ---------------------------------------------------------------- scalp signals (15m / 1h / 4h)

  let scalpHorizon = "1h";
  const scalpSeen = {};
  const scalpOpen = new Set();

  function fmtR(v) {
    if (v == null) return DASH;
    const r = Math.round(v * 100) / 100; // no "−0.00R"
    return `${r > 0 ? "+" : r < 0 ? "−" : ""}${Math.abs(r).toFixed(2)}R`;
  }

  function backtestText(bt) {
    if (!bt || !bt.trades) return "no trades";
    return `${bt.trades} tr · ${Math.round(bt.win_rate)}% · ${fmtR(bt.expectancy_r)}`;
  }

  function scalpDetail(sig) {
    const bt = sig.backtest;
    const parts = [];
    const reviewBox = h("div", { class: "pad" }, sig.ai_review ? aiReviewBlock(sig.ai_review) : null);
    const reviewBtn = h("button", { type: "button", class: "small", text: sig.ai_review ? "Review again with AI" : "AI review",
      title: "Ask the OpenAI model for a second opinion on this setup (uses your OpenAI credits)" });
    reviewBtn.addEventListener("click", (e) => { e.stopPropagation(); requestReview("scalp", sig.symbol, sig.horizon, reviewBox, reviewBtn); });
    parts.push(h("div", { class: "pad review-row" }, reviewBtn, sig.ml && sig.ml.validated
      ? h("span", { class: "small muted", text: `statistical filter: ${Math.round(sig.ml.probability * 100)}% win probability (threshold ${Math.round(sig.ml.threshold * 100)}%)` })
      : null), reviewBox);
    if (!sig.plan && sig.pending) {
      const w = sig.pending;
      parts.push(h("div", { class: "pad conditional" },
        h("strong", { class: "small", text: "Not a signal yet: conditional plan" }),
        h("p", { class: "small", text: w.text }),
        h("dl", { class: "detail-grid compact" },
          kv(w.kind === "dip" ? "Dip buy near" : "Trigger (close above)", fmtPrice(w.trigger)),
          kv("Stop", `${fmtPrice(w.stop)} (−${w.risk_pct.toFixed(2)}%)`),
          kv("TP1 / TP2", `${fmtPrice(w.tp1)} / ${fmtPrice(w.tp2)}`),
          kv("If it triggers", sig.if_triggered ? humanize(sig.if_triggered) : DASH),
        ),
        h("p", { class: "muted small", text: "Levels move with every candle. Re-run Find scalps after the trigger candle closes; the backtest decides whether it becomes a buy." }),
      ));
    }
    if (sig.strategy) {
      const stg = sig.strategy;
      parts.push(h("div", { class: "pad strategy-box" },
        h("strong", { class: "small", text: `Strategy: ${stg.name}` }),
        h("p", { class: "small", text: stg.rule }),
        h("p", { class: "muted small", text: `Source: ${stg.source}` }),
        sig.also && sig.also.length ? h("p", { class: "muted small", text: `Also fired now: ${sig.also.join(" · ")}` }) : null));
    }
    if (sig.plan) {
      const p = sig.plan;
      parts.push(h("dl", { class: "detail-grid compact" },
        kv("Buy zone", `${fmtPrice(p.entry_low)} to ${fmtPrice(p.entry_high)}`),
        kv("Stop", `${fmtPrice(p.stop)} (−${p.risk_pct.toFixed(2)}%)`),
        kv("Net R:R", `${p.reward_risk_tp1.toFixed(2)} at TP1 · ${p.reward_risk_tp2.toFixed(2)} at TP2`),
        kv("Size", `${p.suggested_allocation_pct.toFixed(1)}% of portfolio (risks ${p.risk_at_allocation_pct.toFixed(2)}%)`),
        kv("Costs", `${p.cost_pct.toFixed(2)}% round trip`),
        kv("Take profit", p.exit_rule || `TP1 ${fmtPrice(p.tp1)}, TP2 ${fmtPrice(p.tp2)}`, "wide"),
        kv("Valid until", `${fmtTime(p.valid_until)} · ${p.time_exit}`, "wide"),
      ));
    }
    if (sig.expected && sig.expected.risk_per_trade) {
      const e = sig.expected;
      parts.push(h("p", { class: "small pad expected" },
        `At your settings you risk ${fmtUsd(e.risk_per_trade)} per trade. Measured average: ${fmtR(e.expectancy_r)} ≈ `,
        h("strong", { text: `${e.expected_per_trade >= 0 ? "+" : "−"}$${Math.abs(e.expected_per_trade).toFixed(2)} per trade` }),
        e.trades_per_day ? ` · about ${e.trades_per_day} setups a day on this coin` : "",
        ` (${e.note}).`,
      ));
    } else if (sig.expected) {
      parts.push(h("p", { class: "small pad muted", text: "Enter your cash in Portfolio to see the measured average in dollars per trade." }));
    }
    if (sig.would_be) parts.unshift(heldBackNote(sig.filtered_by, sig.would_be));
    parts.push(h("div", { class: "pad" }, h("strong", { class: "small", text: "Why" }), plainList(sig.reasons, "small")));
    if (sig.risks.length) parts.push(h("div", { class: "pad" }, h("strong", { class: "small", text: "Risks" }), plainList(sig.risks, "small")));
    if (sig.board) {
      const lv = sig.plan ? { entry: sig.plan.entry, stop: sig.plan.stop, tp2: sig.plan.tp2 }
        : sig.pending ? { entry: sig.pending.trigger, stop: sig.pending.stop, tp2: sig.pending.tp2 } : {};
      parts.push(h("div", { class: "ev-inline" }, h("div", { class: "pad-tight" }, h("strong", { class: "small", text: "Evidence board" })), evidenceBlock(sig.board, lv)));
    }
    if (bt && bt.trades) {
      const outcomes = Object.entries(bt.outcomes).map(([k, v]) => `${humanize(k)} ${v}`).join(" · ");
      const setups = Object.entries(bt.by_setup).filter(([, v]) => v.trades).map(([k, v]) => `${k}: ${v.trades} trades, ${fmtR(v.expectancy_r)}`).join(" · ");
      parts.push(h("dl", { class: "detail-grid compact" },
        kv("Backtest", `${bt.days} days of ${bt.setup_timeframe} candles`),
        kv("Win rate", `${Math.round(bt.win_rate)}% of ${bt.trades}`),
        kv("Expectancy", `${fmtR(bt.expectancy_r)} per trade`),
        kv("Profit factor", bt.no_losses ? "no losses" : bt.profit_factor == null ? DASH : bt.profit_factor.toFixed(2)),
        kv("Max drawdown", fmtR(-bt.max_drawdown_r)),
        kv("Exits", outcomes),
        kv("By setup", setups || DASH),
      ));
      if (bt.recent.length) {
        parts.push(h("div", { class: "pad recent-trades" },
          h("span", { class: "small muted", text: "Last backtest trades: " }),
          ...bt.recent.slice(-10).map((t) => h("span", { class: `r-chip ${t.r_multiple > 0 ? "pos" : "neg"}`, title: `${t.kind} · ${new Date(t.entry_time).toLocaleString()} · ${humanize(t.outcome)}`, text: fmtR(t.r_multiple) })),
        ));
      }
    }
    return parts;
  }

  function scalpRows(sig) {
    const open = scalpOpen.has(sig.symbol);
    const toggle = () => {
      if (scalpOpen.has(sig.symbol)) scalpOpen.delete(sig.symbol); else scalpOpen.add(sig.symbol);
      renderScalp(lastScalp);
    };
    const p = sig.plan;
    const w = !p ? sig.pending : null; // conditional levels while waiting for a trigger
    const actionable = sig.signal === "BUY" || sig.signal === "STRONG BUY";
    const planCls = actionable ? "num" : "num muted";
    const entryCell = p
      ? [fmtPrice(p.entry)]
      : w ? [h("span", { class: "cond", text: `${w.kind === "dip" ? "≈ " : "> "}${fmtPrice(w.trigger)}` }),
          h("div", { class: "muted small", text: w.kind === "dip" ? "dip buy" : "trigger" })]
        : [DASH];
    const level = (value, sub) => (value == null ? [DASH] : [fmtPrice(value), sub ? h("div", { class: "muted small", text: sub }) : null]);
    const row = h(
      "tr",
      { class: `clickable${open ? " open" : ""}${w ? " waiting" : ""}`, onclick: toggle, "aria-expanded": open ? "true" : "false" },
      h("td", {}, h("div", { class: "asset-cell" }, h("strong", { text: sig.symbol }), h("span", { class: "muted small", text: sig.name }))),
      h("td", {}, signalBadge(sig.signal, null), sig.status ? h("div", { class: "muted small status-line", text: sig.status }) : null, aiChip(sig.ai_review),
        sig.board ? evidenceChip(sig.board.score, sig.board.grade, sig.board.vetoes && sig.board.vetoes.length) : null),
      h("td", { class: "hide-sm", title: sig.strategy ? sig.strategy.source : null },
        sig.strategy ? sig.strategy.name : sig.setup ? humanize(sig.setup) : DASH,
        sig.also && sig.also.length ? h("div", { class: "muted small", text: `+${sig.also.length} more` }) : null),
      h("td", { class: planCls }, ...entryCell),
      h("td", { class: planCls }, ...(p ? level(p.stop, `−${p.risk_pct.toFixed(2)}%`) : w ? level(w.stop, `−${w.risk_pct.toFixed(2)}%`) : [DASH])),
      h("td", { class: planCls }, ...(p ? level(p.tp1, fmtPrice(p.tp2)) : w ? level(w.tp1, fmtPrice(w.tp2)) : [DASH])),
      h("td", { class: "num", title: sig.evidence === "pooled" ? "coin has too few trades: pooled record of all scanned coins decides" : null },
        backtestText(sig.backtest),
        sig.if_triggered ? h("div", { class: `small if-${sig.if_triggered === "NO TRADE" ? "no" : sig.if_triggered === "WATCH" ? "watch" : "buy"}`, text: `if triggered: ${humanize(sig.if_triggered)}` })
          : sig.evidence === "pooled" ? h("div", { class: "muted small", text: "pooled" }) : null),
      h("td", { class: "hide-sm" }, h("span", { class: "reason", text: sig.reasons[0] || "" })),
    );
    if (!open) return [row];
    return [row, h("tr", { class: "detail-row" }, h("td", { colspan: 8 }, h("div", { class: "scalp-detail" }, ...scalpDetail(sig))))];
  }

  let lastScalp = null;

  function renderResearch(res) {
    const why = $("scalp-why");
    if (res && res.no_buy_reason) {
      why.replaceChildren(h("strong", { text: "Why no buy: " }), res.no_buy_reason);
      why.hidden = false;
    } else {
      why.hidden = true;
    }
    const box = $("scalp-research");
    const rows = (res && res.research) || [];
    if (!rows.length) { box.hidden = true; return; }
    const valid = rows.filter((r) => r.validated).length;
    $("scalp-research-meta").textContent = ` · ${valid} of ${rows.length} validated on these coins`;
    const cell = (sp) => (sp && sp.trades ? [fmtR(sp.expectancy_r), h("div", { class: "muted small", text: `${sp.trades} tr · ${Math.round(sp.win_rate ?? 0)}%` })] : [DASH]);
    const sorted = rows; // the server orders them: validated, then enough trades to judge, then closest to enough
    $("scalp-research-body").replaceChildren(
      table([["Strategy"], ["Older 70%", "num"], ["Newer 30%", "num"], ["All", "num hide-sm"], ["Exit", "hide-sm"], ["Verdict"]],
        sorted.map((r) => h("tr", { class: r.validated ? "chosen" : null },
          h("td", { title: r.source }, h("strong", { class: "small", text: r.name }), h("div", { class: "muted small rule-text", text: r.rule })),
          h("td", { class: `num ${r.train.expectancy_r > 0 ? "pnl-up" : r.train.expectancy_r < 0 ? "pnl-down" : ""}` }, ...cell(r.train)),
          h("td", { class: `num ${r.test.expectancy_r > 0 ? "pnl-up" : r.test.expectancy_r < 0 ? "pnl-down" : ""}` }, ...cell(r.test)),
          h("td", { class: "num hide-sm" }, ...cell(r.all)),
          h("td", { class: "hide-sm small", text: r.exit }),
          h("td", {}, toneBadge(r.validated ? "good" : "neutral", r.validated ? "Validated" : "Not validated"),
            h("div", { class: "muted small", text: r.reasons[0] || "" }))))),
      h("p", { class: "muted small", text: "Results are per trade in R (1R = the amount risked), after fees and slippage, pooled over "
        + "the scanned coins. A strategy may give a BUY only when both the older and the newer part of the history made money "
        + "and the result is unlikely to be luck." }));
    box.hidden = false;
  }

  function renderScalp(res) {
    lastScalp = res;
    const rows = $("scalp-rows");
    if (!res) {
      rows.replaceChildren(h("tr", {}, h("td", { colspan: 8, class: "empty", text: "No scan yet for this horizon. Press “Find scalps”." })));
      $("scalp-meta").textContent = "";
      $("scalp-pooled").hidden = true;
      renderResearch(null);
      return;
    }
    const c = res.counts;
    $("scalp-meta").textContent = `${res.label}: setup ${res.setup_timeframe}, trend ${res.trend_timeframe}, filter ${res.filter_timeframe} · `
      + `${c["STRONG BUY"] + c.BUY} buy, ${c.WATCH} watch · ${fmtTime(res.generated_at)}`;
    const pool = res.pooled;
    if (pool && pool.trades) {
      $("scalp-pooled").textContent = `Same rules across ${pool.coins} coins, ${pool.days} days: ${pool.trades} trades, win rate ${Math.round(pool.win_rate)}%, `
        + `expectancy ${fmtR(pool.expectancy_r)}, profit factor ${pool.no_losses ? "no losses" : pool.profit_factor == null ? DASH : pool.profit_factor.toFixed(2)} `
        + `(net of ${res.cost_pct.toFixed(2)}% costs).`;
      $("scalp-pooled").hidden = false;
    } else {
      $("scalp-pooled").hidden = true;
    }
    renderResearch(res);
    const err = $("scalp-error");
    if (res.errors.length) setMessage(err, "Some coins could not be analysed", res.errors.slice(0, 5)); else err.hidden = true;
    rows.replaceChildren(...(res.signals.length ? res.signals.flatMap(scalpRows)
      : [h("tr", {}, h("td", { colspan: 8, class: "empty", text: "No coins analysed (check Coins to analyse)." }))]));
  }

  async function refreshScalp() {
    const horizon = scalpHorizon;
    try {
      const r = await getJSON(`/api/scalp?horizon=${horizon}`);
      if (horizon !== scalpHorizon) return; // the user switched horizon while this was loading
      renderScalp(r.result);
      renderScalpStatus(r.status);
    } catch (err) {
      if (horizon === scalpHorizon) setMessage($("scalp-error"), `Scalp signals unavailable: ${err.message}`);
    }
  }

  function renderScalpStatus(all) {
    if (!all) return;
    const st = all[scalpHorizon];
    const button = $("scalp-scan");
    button.disabled = !!(st && st.running);
    button.textContent = st && st.running ? "Scanning…" : "Find scalps";
    let text = "";
    if (st) {
      if (st.running) text = `Backtesting and scanning… ${st.done}/${st.total || "?"}`;
      else if (st.outcome === "stopped") text = `Stopped ${fmtTime(st.finished_at)}`;
      else if (st.outcome === "failed") text = `Scan failed: ${st.error || "unknown error"}`;
    }
    $("scalp-status").textContent = text;
    $("scalp-progress").style.width = st && st.running && st.total ? `${(st.done / st.total) * 100}%` : "0%";
    for (const [key, value] of Object.entries(all)) {
      const seen = scalpSeen[key];
      if (value.finished_at && seen !== undefined && seen !== value.finished_at && key === scalpHorizon) {
        refreshScalp();
        refreshRecord();
      }
      scalpSeen[key] = value.finished_at;
    }
  }

  function setHorizon(key) {
    scalpHorizon = key;
    renderScalp(null); // clear the other horizon's rows at once
    pref("scalpHorizon", key);
    document.querySelectorAll(".segmented [data-horizon]").forEach((b) => b.setAttribute("aria-checked", b.dataset.horizon === key ? "true" : "false"));
    scalpOpen.clear();
    refreshScalp();
  }

  async function startScalp() {
    try {
      const r = await api(`/api/scalp/scan?horizon=${scalpHorizon}`, { method: "POST" });
      renderScalpStatus(r.status);
      scheduleControl(1000);
    } catch (err) {
      setMessage($("scalp-error"), `Could not start the scan: ${err.message}`);
    }
  }

  // ---------------------------------------------------------------- track record

  async function refreshRecord() {
    const body = $("record-body");
    try {
      const d = await getJSON("/api/performance?days=90");
      if (!d.strategies.length && !(d.held_back && d.held_back.length)) {
        body.replaceChildren(h("p", { class: "muted small pad", text: d.persistence === "ok"
          ? "No buy signals tracked yet. Every BUY from now on (swing and scalp) is followed here until it hits its stop, targets or time limit."
          : "The track record needs the database." }));
        return;
      }
      body.replaceChildren(...[
        table(
          [["Strategy"], ["Closed", "num"], ["Win rate", "num"], ["Avg", "num"], ["Total", "num hide-sm"], ["PF", "num hide-sm"], ["Open", "num"]],
          d.strategies.map((st) => h("tr", {},
            h("td", {}, h("strong", { text: st.label })),
            h("td", { class: "num", text: st.closed }),
            h("td", { class: "num", text: st.win_rate == null ? DASH : `${Math.round(st.win_rate)}%` }),
            h("td", { class: `num ${st.avg_r > 0 ? "pnl-up" : st.avg_r < 0 ? "pnl-down" : ""}`, text: fmtR(st.avg_r) }),
            h("td", { class: "num hide-sm", text: fmtR(st.total_r) }),
            h("td", { class: "num hide-sm", text: st.profit_factor == null ? DASH : st.profit_factor.toFixed(2) }),
            h("td", { class: "num", title: st.skipped ? `${st.skipped} overlapping signals not counted` : null, text: st.open }),
          )),
        ),
        d.by_ai && d.by_ai.some((r) => r.verdict !== "not reviewed") ? h("div", { class: "pad" },
          h("strong", { class: "small", text: "By AI verdict (does the reviewer help?)" }),
          table([["AI verdict"], ["Closed", "num"], ["Win rate", "num"], ["Avg", "num"]],
            d.by_ai.map((r) => h("tr", {}, h("td", { text: humanize(r.verdict) }), h("td", { class: "num", text: r.closed }),
              h("td", { class: "num", text: `${Math.round(r.win_rate)}%` }),
              h("td", { class: `num ${r.avg_r > 0 ? "pnl-up" : r.avg_r < 0 ? "pnl-down" : ""}`, text: fmtR(r.avg_r) }))))) : null,
        d.by_evidence && d.by_evidence.length ? h("div", { class: "pad" },
          h("strong", { class: "small", text: "By evidence board (does it help?)" }),
          table([["Board"], ["Closed", "num"], ["Win rate", "num"], ["Avg", "num"]],
            d.by_evidence.map((r) => h("tr", {}, h("td", { text: GRADE_TEXT[r.grade] || r.grade }), h("td", { class: "num", text: r.closed }),
              h("td", { class: "num", text: `${Math.round(r.win_rate)}%` }),
              h("td", { class: `num ${r.avg_r > 0 ? "pnl-up" : r.avg_r < 0 ? "pnl-down" : ""}`, text: fmtR(r.avg_r) }))))) : null,
        d.held_back && d.held_back.length ? h("div", { class: "pad" },
          h("strong", { class: "small", text: "Held back by filters: what those setups did (not in the record above)" }),
          table([["Held back by"], ["Closed", "num"], ["Win rate", "num"], ["Avg", "num"]],
            d.held_back.map((r) => h("tr", {}, h("td", { text: humanize(FILTER_TEXT[r.filtered_by] || r.filtered_by) }), h("td", { class: "num", text: r.closed }),
              h("td", { class: "num", text: `${Math.round(r.win_rate)}%` }),
              h("td", { class: `num ${r.avg_r > 0 ? "pnl-up" : r.avg_r < 0 ? "pnl-down" : ""}`, text: fmtR(r.avg_r) })))),
          h("p", { class: "muted small", text: "A filter helps when what it held back did worse than what was shown." })) : null,
        d.recent.length ? h("div", { class: "pad recent-trades" },
          h("span", { class: "small muted", text: "Latest: " }),
          ...d.recent.slice(0, 12).map((t) => h("span", {
            class: `r-chip ${t.r_multiple > 0 ? "pos" : "neg"}`,
            title: `${t.symbol} · ${t.label} · ${t.signal} · ${new Date(t.created_at).toLocaleString()} · ${humanize(t.outcome)} after ${t.hours} h`,
            text: `${t.symbol} ${fmtR(t.r_multiple)}`,
          })),
        ) : h("p", { class: "muted small pad", text: "No finished trades yet." }),
      ].filter(Boolean));
    } catch (err) {
      body.replaceChildren(h("p", { class: "muted small pad", text: `Track record unavailable: ${err.message}` }));
    }
  }

  // ---------------------------------------------------------------- strategy lab (phases 8 and 9)

  let labHorizon = "1h";
  const labSeen = {};

  function niceStep(span, target = 4) {
    const raw = span / target;
    const pow = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const m = raw / pow;
    return (m >= 5 ? 10 : m >= 2 ? 5 : m >= 1 ? 2 : 1) * pow;
  }

  function equityChart(points, splitTime) {
    if (!points || points.length < 2) return h("p", { class: "muted small pad", text: "Not enough trades for an equity curve." });
    const W = 760, H = 220, padL = 10, padR = 58, padT = 14, padB = 24;
    const t = points.map((p) => new Date(p.time).getTime());
    const t0 = t[0], t1 = t[t.length - 1] || t0 + 1;
    let lo = Math.min(0, ...points.map((p) => p.r)), hi = Math.max(0, ...points.map((p) => p.r));
    const step = niceStep(hi - lo || 1);
    lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
    if (hi === lo) hi = lo + step;
    const x = (ms) => padL + ((ms - t0) / (t1 - t0 || 1)) * (W - padL - padR);
    const y = (v) => padT + ((hi - v) / (hi - lo)) * (H - padT - padB);
    const ticks = [];
    for (let v = lo; v <= hi + 1e-9; v += step) {
      ticks.push(s("line", { x1: padL, x2: W - padR, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 }),
        s("text", { x: W - padR + 6, y: y(v) + 4, "font-size": 11, fill: "var(--muted)" }, document.createTextNode(`${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(step < 1 ? 1 : 0)}R`)));
    }
    const pts = points.map((p, i) => `${x(t[i]).toFixed(1)},${y(p.r).toFixed(1)}`);
    const area = s("polygon", { points: `${x(t0).toFixed(1)},${y(0)} ${pts.join(" ")} ${x(t1).toFixed(1)},${y(0)}`, fill: "var(--viz-pos)", opacity: 0.1 });
    const line = s("polyline", { points: pts.join(" "), fill: "none", stroke: "var(--viz-pos)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
    const zero = s("line", { x1: padL, x2: W - padR, y1: y(0), y2: y(0), stroke: "var(--muted)", "stroke-width": 1 });
    const marks = [];
    if (splitTime) {
      const sx = x(new Date(splitTime).getTime());
      if (sx > padL && sx < W - padR) {
        marks.push(s("line", { x1: sx, x2: sx, y1: padT, y2: H - padB, stroke: "var(--muted)", "stroke-width": 1 }),
          s("text", { x: sx - 6, y: padT + 10, "font-size": 11, fill: "var(--muted)", "text-anchor": "end" }, document.createTextNode("older 70% (chooses)")),
          s("text", { x: sx + 6, y: padT + 10, "font-size": 11, fill: "var(--muted)" }, document.createTextNode("newer 30% (checks)")));
      }
    }
    const last = points[points.length - 1];
    const endDot = s("circle", { cx: x(t1), cy: y(last.r), r: 4, fill: "var(--viz-pos)", stroke: "var(--surface)", "stroke-width": 2 });
    const cross = s("line", { y1: padT, y2: H - padB, stroke: "var(--muted)", "stroke-width": 1, opacity: 0, "pointer-events": "none" });
    const dot = s("circle", { r: 4, fill: "var(--viz-pos)", stroke: "var(--surface)", "stroke-width": 2, opacity: 0, "pointer-events": "none" });
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: 0,
      "aria-label": `Cumulative result in R over ${points.length} points, ending at ${last.r.toFixed(1)}R` },
      ticks, area, zero, marks, line, endDot, cross, dot);
    const tip = h("div", { class: "chart-tip", text: `Ends at ${fmtR(last.r)} after all trades. Hover for the running total.` });
    const show = (i) => {
      const p = points[i];
      cross.setAttribute("x1", x(t[i])); cross.setAttribute("x2", x(t[i])); cross.setAttribute("opacity", 0.6);
      dot.setAttribute("cx", x(t[i])); dot.setAttribute("cy", y(p.r)); dot.setAttribute("opacity", 1);
      tip.textContent = `${fmtR(p.r)} total by ${new Date(p.time).toLocaleString()}`;
    };
    svg.addEventListener("mousemove", (ev) => {
      const rect = svg.getBoundingClientRect();
      const ms = t0 + (((ev.clientX - rect.left) / rect.width) * W - padL) / (W - padL - padR) * (t1 - t0);
      let best = 0;
      for (let i = 1; i < t.length; i++) if (Math.abs(t[i] - ms) < Math.abs(t[best] - ms)) best = i;
      show(best);
    });
    svg.addEventListener("focus", () => show(points.length - 1));
    svg.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); });
    return h("div", { class: "lab-chart" }, svg, tip);
  }

  function barPath(x, w, yBase, yTip, r = 4) {
    const up = yTip < yBase;
    const hgt = Math.abs(yBase - yTip);
    const rr = Math.min(r, hgt, w / 2);
    if (hgt < 0.5) return `M${x},${yBase}h${w}`;
    return up
      ? `M${x},${yBase}V${yTip + rr}Q${x},${yTip} ${x + rr},${yTip}H${x + w - rr}Q${x + w},${yTip} ${x + w},${yTip + rr}V${yBase}Z`
      : `M${x},${yBase}V${yTip - rr}Q${x},${yTip} ${x + rr},${yTip}H${x + w - rr}Q${x + w},${yTip} ${x + w},${yTip - rr}V${yBase}Z`;
  }

  function hourChart(byHour) {
    const keys = Object.keys(byHour || {}).sort();
    if (!keys.length) return null;
    const W = 760, H = 180, padL = 10, padR = 58, padT = 18, padB = 26;
    const vals = keys.map((k) => byHour[k].expectancy_r ?? 0);
    let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    const step = niceStep(hi - lo || 1, 3);
    lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
    if (hi === lo) hi = lo + step;
    const y = (v) => padT + ((hi - v) / (hi - lo)) * (H - padT - padB);
    const band = (W - padL - padR) / keys.length;
    const bw = Math.min(24, band * 0.5);
    const tip = h("div", { class: "chart-tip", text: "Average result per trade by the hour it started (UTC). Hover a bar." });
    const grid = [];
    for (let v = lo; v <= hi + 1e-9; v += step) {
      grid.push(s("line", { x1: padL, x2: W - padR, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 }),
        s("text", { x: W - padR + 6, y: y(v) + 4, "font-size": 11, fill: "var(--muted)" }, document.createTextNode(`${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(step < 1 ? 1 : 0)}R`)));
    }
    const bars = keys.map((k, i) => {
      const st = byHour[k];
      const v = st.expectancy_r ?? 0;
      const cx = padL + band * i + band / 2;
      const bar = s("path", { d: barPath(cx - bw / 2, bw, y(0), y(v)), fill: v >= 0 ? "var(--viz-pos)" : "var(--viz-neg)", class: "viz-bar" });
      const label = s("text", { x: cx, y: v >= 0 ? y(v) - 5 : y(v) + 13, "font-size": 11, fill: "var(--ink-2)", "text-anchor": "middle" },
        document.createTextNode(fmtR(v)));
      const xl = s("text", { x: cx, y: H - 8, "font-size": 11, fill: "var(--muted)", "text-anchor": "middle" }, document.createTextNode(k));
      const hit = s("rect", { x: cx - band / 2, y: padT, width: band, height: H - padT - padB, fill: "transparent", tabindex: 0 });
      const describe = () => {
        bar.classList.add("hover");
        tip.textContent = `${k} UTC: ${fmtR(v)} per trade over ${st.trades} trades, ${Math.round(st.win_rate ?? 0)}% winners`;
      };
      hit.addEventListener("mousemove", describe);
      hit.addEventListener("focus", describe);
      hit.addEventListener("mouseleave", () => bar.classList.remove("hover"));
      hit.addEventListener("blur", () => bar.classList.remove("hover"));
      return s("g", {}, bar, label, xl, hit);
    });
    const zero = s("line", { x1: padL, x2: W - padR, y1: y(0), y2: y(0), stroke: "var(--muted)", "stroke-width": 1 });
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Average result per trade by starting hour (UTC)" }, grid, zero, bars);
    return h("div", { class: "lab-chart" }, svg, tip);
  }

  function statsTable(groups, label) {
    const rows = Object.entries(groups || {});
    if (!rows.length) return null;
    return table(
      [[label], ["Trades", "num"], ["Win rate", "num"], ["Avg", "num"], ["Total", "num hide-sm"]],
      rows.map(([k, st]) => h("tr", {},
        h("td", { text: k }),
        h("td", { class: "num", text: st.trades }),
        h("td", { class: "num", text: st.win_rate == null ? DASH : `${Math.round(st.win_rate)}%` }),
        h("td", { class: `num ${st.expectancy_r > 0 ? "pnl-up" : st.expectancy_r < 0 ? "pnl-down" : ""}`, text: fmtR(st.expectancy_r) }),
        h("td", { class: "num hide-sm", text: fmtR(st.total_r) }))),
    );
  }

  function splitCell(st) {
    if (!st || !st.trades) return [DASH];
    return [fmtR(st.expectancy_r), h("div", { class: "muted small", text: `${st.trades} tr · ${Math.round(st.win_rate ?? 0)}%` })];
  }

  function mlBlock(view) {
    const m = view.model;
    if (!m) return h("p", { class: "muted small pad", text: "Statistical filter: not trained yet (run the lab)." });
    const mt = m.metrics || {};
    const fmt2 = (v) => (v == null ? DASH : v.toFixed(2));
    const toggle = h("input", { type: "checkbox", checked: view.ml_enabled ? true : null });
    toggle.addEventListener("change", async () => {
      try { renderLab(await api(`/api/ml?horizon=${labHorizon}`, { method: "PUT", body: { enabled: toggle.checked } }).then(() => getJSON(`/api/lab?horizon=${labHorizon}`))); }
      catch (err) { window.alert(err.message); toggle.checked = !toggle.checked; }
    });
    return h("div", { class: "pad ml-block" },
      h("div", { class: "mood-head" },
        h("strong", { class: "small", text: "Statistical filter (logistic regression)" }),
        toneBadge(m.validated ? "good" : "neutral", m.validated ? "Validated" : "Not validated"),
        h("label", { class: "toggle small" }, toggle, " use it when validated")),
      h("dl", { class: "detail-grid compact" },
        kv("Test AUC", `${fmt2(mt.test_auc)} (0.5 = no skill)`),
        kv("Brier (test)", `${fmt2(mt.test_brier)} vs ${fmt2(mt.test_brier_baseline)} guessing`),
        kv("Kept trades", `${mt.test_kept ?? 0} of ${mt.test_trades ?? 0} at ≥ ${Math.round((m.threshold ?? 0) * 100)}%`),
        kv("Kept vs all (test)", `${fmtR(mt.test_kept_expectancy_r)} vs ${fmtR(mt.test_expectancy_r)}`),
        kv("Strongest inputs", (mt.top_features || []).slice(0, 3).map((f) => `${f.name} ${f.weight > 0 ? "+" : "−"}`).join(", ") || DASH),
      ),
      plainList(m.reasons || [], "small"),
    );
  }

  function renderLab(view) {
    const body = $("lab-body");
    const res = view.result;
    const applied = view.applied || {};
    const appliedRow = h("div", { class: "pad lab-applied" },
      h("span", { class: "small" }, "In use for ", h("strong", { text: horizonLabel(view.horizon) }), ": ",
        h("strong", { text: applied.label || "published rules" }), h("span", { class: "muted", text: ` (${applied.source || "default"})` })),
      applied.filter && (applied.filter !== "base" || applied.exit !== "x1")
        ? h("button", { type: "button", class: "ghost small", text: "Reset to published rules", onclick: async () => {
            try { renderLab(await api(`/api/lab/reset?horizon=${view.horizon}`, { method: "POST" })); } catch (err) { window.alert(err.message); }
          } })
        : null);
    if (!res) {
      body.replaceChildren(appliedRow, h("p", { class: "muted small pad", text: "No lab run yet for this horizon. Press Run the lab (it downloads about 4,000 candles per coin, like Find scalps, and takes a minute)." }), mlBlock(view));
      $("lab-meta").textContent = "";
      return;
    }
    $("lab-meta").textContent = `${res.coins.length} coins · ${fmtTime(res.generated_at)} · costs ${res.cost_pct.toFixed(2)}%`;
    const verdict = h("div", { class: `pad lab-verdict ${res.accepted ? "ok" : "no"}` },
      toneBadge(res.accepted ? "good" : "warning", res.accepted ? "Better variant found and confirmed" : "Nothing beat the rules on newer data"),
      plainList(res.reasons, "small"));
    const byKey = Object.fromEntries((view.filters || []).map((f) => [f.key, f.label]));
    const exitKey = Object.fromEntries((view.exits || []).map((f) => [f.key, f.label]));
    const combos = res.combos.slice(0, 12).map((c) => h("tr", { class: c.filter === res.chosen[0] && c.exit === res.chosen[1] ? "chosen" : null },
      h("td", {}, h("div", { text: byKey[c.filter] || c.filter }), h("div", { class: "muted small", text: exitKey[c.exit] || c.exit })),
      h("td", { class: "num" }, ...splitCell(c.train)),
      h("td", { class: "num" }, ...splitCell(c.test)),
      h("td", { class: "num hide-sm", text: c.test.profit_factor == null ? DASH : c.test.profit_factor.toFixed(2) }),
      h("td", {}, h("button", { type: "button", class: "ghost small", text: "Use", title: "Apply this variant by hand (your decision, even if the lab did not confirm it)",
        onclick: async () => {
          if (!window.confirm(`Use "${byKey[c.filter]}; ${exitKey[c.exit]}" for ${horizonLabel(view.horizon)} scalps?`)) return;
          try { renderLab(await api(`/api/lab/apply?horizon=${view.horizon}`, { method: "POST", body: { filter: c.filter, exit: c.exit } })); }
          catch (err) { window.alert(err.message); }
        } })),
    ));
    const st = res.stats || {};
    body.replaceChildren(...[
      appliedRow,
      verdict,
      h("div", { class: "pad" }, h("strong", { class: "small", text: `Cumulative result in R: ${applied.label || "published rules"} (all coins)` })),
      equityChart(st.equity, res.split_time),
      h("div", { class: "pad" }, h("strong", { class: "small", text: "Average result per trade by starting hour (UTC)" })),
      hourChart(st.by_hour_utc),
      h("div", { class: "pad" }, h("strong", { class: "small", text: "Best variants (sorted by the older 70%, the part that chooses)" })),
      table([["Variant"], ["Older 70%", "num"], ["Newer 30%", "num"], ["PF new", "num hide-sm"], [""]], combos),
      h("details", { class: "pad lab-tables" }, h("summary", { class: "small", text: "Breakdown tables (by coin, setup, weekday, hour)" }),
        statsTable(st.by_coin, "Coin"), statsTable(st.by_setup, "Setup"), statsTable(st.by_weekday, "Weekday"), statsTable(st.by_hour_utc, "Hour (UTC)")),
      mlBlock(view),
      res.errors && res.errors.length ? h("p", { class: "muted small pad", title: res.errors.join("\n"), text: `${res.errors.length} coin(s) skipped (not enough clean history)` }) : null,
    ].filter(Boolean));
  }

  function horizonLabel(k) {
    return { "15m": "15-minute scalps", "1h": "1-hour trades", "4h": "4-hour trades", "1d": "1-day trades" }[k] || k;
  }

  async function refreshLab() {
    const horizon = labHorizon;
    try {
      const view = await getJSON(`/api/lab?horizon=${horizon}`);
      if (horizon !== labHorizon) return;
      renderLab(view);
      renderLabStatus({ [horizon]: view.status });
    } catch (err) {
      $("lab-body").replaceChildren(h("p", { class: "muted small pad", text: `Lab unavailable: ${err.message}` }));
    }
  }

  function renderLabStatus(all) {
    if (!all) return;
    const st = all[labHorizon];
    if (!st) return;
    const button = $("lab-run");
    button.disabled = st.running || emergency;
    button.textContent = st.running ? "Running…" : "Run the lab";
    $("lab-status").textContent = st.running ? st.phase : st.outcome === "failed" ? `Failed: ${st.error || "unknown"}` : st.outcome === "stopped" ? "Stopped" : "";
    $("lab-progress").style.width = st.running && st.total ? `${Math.min(100, (st.done / st.total) * 100)}%` : "0%";
    for (const [key, value] of Object.entries(all)) {
      if (labSeen[key] !== undefined && labSeen[key] !== value.finished_at && key === labHorizon && value.finished_at) refreshLab();
      labSeen[key] = value.finished_at;
    }
  }

  function setLabHorizon(key) {
    labHorizon = key;
    pref("labHorizon", key);
    document.querySelectorAll("[data-lab-horizon]").forEach((b) => b.setAttribute("aria-checked", b.dataset.labHorizon === key ? "true" : "false"));
    $("lab-body").replaceChildren(h("p", { class: "muted small pad", text: "Loading…" }));
    refreshLab();
  }

  async function startLab() {
    try {
      const r = await api(`/api/lab/run?horizon=${labHorizon}`, { method: "POST", timeoutMs: 60000 });
      renderLabStatus({ [labHorizon]: r.status });
      scheduleControl(1000);
    } catch (err) {
      window.alert(`Could not start the lab: ${err.message}`);
    }
  }

  // ---------------------------------------------------------------- evidence board (phase 10)

  const GRADE_TEXT = { strong_for: "strong support", for: "supportive", neutral: "mixed", against: "headwinds",
    strong_against: "strong headwinds", thin: "too little data" };
  const GRADE_TONE = { strong_for: "good", for: "good", neutral: "neutral", against: "warning", strong_against: "critical", thin: "neutral" };
  const FILTER_TEXT = { evidence: "the evidence board", learned: "the learned evidence model", ml: "the statistical filter", filter: "a filter" };
  let evSettings = null;

  const signedNum = (v, digits = 0) => (v == null ? DASH : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(digits)}`);
  const factorName = (key) => (evSettings && evSettings.labels && evSettings.labels[key]) || humanize(key);

  function evidenceChip(score, grade, vetoed) {
    if (score == null && !vetoed) return null;
    const text = vetoed ? "Evidence: veto" : `Evidence ${signedNum(score)}`;
    return h("span", { class: "ev-chip", title: vetoed ? "the evidence board vetoes buying now" : `evidence board: ${GRADE_TEXT[grade] || grade}` },
      toneBadge(vetoed ? "critical" : GRADE_TONE[grade] || "neutral", text));
  }

  function confluenceMeter(score) {
    const W = 240, H = 14, mid = W / 2;
    const v = Math.max(-100, Math.min(100, score ?? 0));
    const x = mid + (v / 100) * mid;
    return s("svg", { viewBox: `0 0 ${W} ${H}`, class: "confluence", role: "img", "aria-label": `Evidence score ${signedNum(score)} on a scale of −100 to +100` },
      s("rect", { x: 0, y: 4, width: W, height: H - 8, rx: 3, fill: "var(--grid)" }),
      Math.abs(x - mid) >= 1 ? s("path", { d: v >= 0 ? `M${mid},4H${x - 3}Q${x},4 ${x},7V${H - 7}Q${x},${H - 4} ${x - 3},${H - 4}H${mid}Z`
        : `M${mid},4H${x + 3}Q${x},4 ${x},7V${H - 7}Q${x},${H - 4} ${x + 3},${H - 4}H${mid}Z`, fill: v >= 0 ? "var(--viz-pos)" : "var(--viz-neg)" }) : null,
      s("line", { x1: mid, x2: mid, y1: 0, y2: H, stroke: "var(--muted)", "stroke-width": 1 }));
  }

  function factorRow(f) {
    const dir = f.direction > 0 ? "for" : f.direction < 0 ? "against" : "neutral";
    const tag = f.veto ? "veto" : dir === "neutral" ? "neutral" : `${dir} · ${Math.round(f.strength * 100)}%`;
    return h("li", { class: `ev-factor ${dir}${f.veto ? " veto" : ""}` },
      h("span", { class: "ev-dir", "aria-hidden": "true", text: f.direction > 0 ? "▲" : f.direction < 0 ? "▼" : "•" }),
      h("div", { class: "ev-main" },
        h("div", { class: "ev-line" }, h("strong", { class: "small", text: f.label }), h("span", { class: "small ev-tag", text: tag })),
        h("div", { class: "small ev-value", text: f.value }),
        h("div", { class: "muted small", text: f.veto ? `${f.detail}. ${f.veto}` : f.detail }),
        h("div", { class: "muted tiny", text: f.source })));
  }

  function hbarPath(x, y, w, hgt, r = 2) {
    const rr = Math.min(r, w / 2, hgt / 2);
    if (w < 0.5) return `M${x},${y}v${hgt}`;
    return `M${x},${y}H${x + w - rr}Q${x + w},${y} ${x + w},${y + rr}V${y + hgt - rr}Q${x + w},${y + hgt} ${x + w - rr},${y + hgt}H${x}Z`;
  }

  function liqChart(map, levels = {}) {
    const all = map.bands || [];
    const used = all.filter((b) => b.long_usd > 0 || b.short_usd > 0);
    if (!used.length) return h("p", { class: "muted small pad", text: "Liquidation map: no estimated liquidation zones within ±12% of the price." });
    const W = 760, rowH = 7, padT = 8, padB = 8, padL = 10, padR = 110;
    const H = padT + padB + all.length * rowH;
    const maxUsd = Math.max(...all.map((b) => Math.max(b.long_usd, b.short_usd)), 1);
    const lo = all[0].low, hi = all[all.length - 1].high;
    const y = (price) => padT + ((hi - price) / (hi - lo)) * (all.length * rowH);
    const xw = (usd) => (usd / maxUsd) * (W - padL - padR);
    const tip = h("div", { class: "chart-tip", text: "Hover a band for its estimated liquidations." });
    const rows = all.map((b) => {
      const usd = Math.max(b.long_usd, b.short_usd);
      const isLong = b.long_usd >= b.short_usd;
      const top = y(b.high);
      const bar = usd > 0 ? s("path", { d: hbarPath(padL, top + 1, xw(usd), rowH - 2), fill: isLong ? "var(--viz-neg)" : "var(--viz-pos)", class: "viz-bar" }) : null;
      const hit = s("rect", { x: padL, y: top, width: W - padL - padR, height: rowH, fill: "transparent", tabindex: usd > 0 ? 0 : null });
      const describe = () => {
        if (bar) bar.classList.add("hover");
        const parts = [];
        if (b.long_usd) parts.push(`longs ${fmtUsd(b.long_usd)} would be forced to sell`);
        if (b.short_usd) parts.push(`shorts ${fmtUsd(b.short_usd)} would be forced to buy`);
        tip.textContent = `${fmtPrice(b.low)} to ${fmtPrice(b.high)}: ${parts.join(" · ") || "no estimated liquidations"} (estimate)`;
      };
      hit.addEventListener("mousemove", describe);
      hit.addEventListener("focus", describe);
      const clear = () => { if (bar) bar.classList.remove("hover"); };
      hit.addEventListener("mouseleave", clear);
      hit.addEventListener("blur", clear);
      return s("g", {}, bar, hit);
    });
    const marks = [["now", map.price, null], ["entry", levels.entry, "2 3"], ["stop", levels.stop, "5 3"], ["TP2", levels.tp2, "2 3"]]
      .filter(([, v]) => v != null && v >= lo && v <= hi).map(([label, v, dash]) => ({ label, v, dash, yy: y(v) }))
      .sort((a, b) => a.yy - b.yy);
    for (let i = 1; i < marks.length; i++) marks[i].ly = Math.max(marks[i].yy, (marks[i - 1].ly ?? marks[i - 1].yy) + 12);
    const markEls = marks.map((m) => s("g", {},
      s("line", { x1: padL, x2: W - padR, y1: m.yy, y2: m.yy, stroke: "var(--ink-2)", "stroke-width": 1, "stroke-dasharray": m.dash }),
      s("text", { x: W - padR + 6, y: (m.ly ?? m.yy) + 4, "font-size": 11, fill: "var(--ink-2)" }, document.createTextNode(`${m.label} ${fmtPrice(m.v)}`))));
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": `Estimated liquidation map from ${fmtPrice(lo)} to ${fmtPrice(hi)}; price ${fmtPrice(map.price)}` }, rows, markEls);
    const sorted = used.slice().sort((a, b) => b.high - a.high);
    return h("div", { class: "liq-chart" },
      h("div", { class: "pad-tight" }, h("strong", { class: "small", text: `Liquidation map (estimate from the last ${map.hours} h of open interest)` })),
      h("div", { class: "chart-legend" },
        h("span", {}, h("i", { class: "swatch neg" }), "long liquidations: forced selling if the price falls there"),
        h("span", {}, h("i", { class: "swatch pos" }), "short liquidations: forced buying if the price rises there")),
      h("div", { class: "lab-chart" }, svg), tip,
      h("details", { class: "pad small" }, h("summary", { text: "Show as a table" }),
        table([["Price band"], ["Longs (est.)", "num"], ["Shorts (est.)", "num"]],
          sorted.map((b) => h("tr", {}, h("td", { text: `${fmtPrice(b.low)} – ${fmtPrice(b.high)}` }),
            h("td", { class: "num", text: b.long_usd ? fmtUsd(b.long_usd) : DASH }), h("td", { class: "num", text: b.short_usd ? fmtUsd(b.short_usd) : DASH }))))),
      map.notes && map.notes.length ? h("p", { class: "muted small pad", text: map.notes.join("; ") }) : null);
  }

  function evidenceBlock(board, levels = {}) {
    if (!board) return h("p", { class: "muted small pad", text: "No evidence board: it is switched off, or this result is from before the upgrade." });
    const vetoed = board.vetoes && board.vetoes.length;
    const parts = [h("div", { class: "ev-head" },
      h("div", { class: "ev-score" }, h("span", { class: "score", text: signedNum(board.score) }), h("span", { class: "unit", text: " of ±100" })),
      toneBadge(vetoed ? "critical" : GRADE_TONE[board.grade] || "neutral", vetoed ? "veto" : GRADE_TEXT[board.grade] || board.grade),
      confluenceMeter(board.score),
      h("span", { class: "muted small", text: `${board.factors.length} factors with data${board.missing.length ? `, ${board.missing.length} without` : ""}` }))];
    if (vetoed) parts.push(h("div", { class: "alert inset", role: "alert" }, h("strong", { text: "Veto: " }), board.vetoes.join("; ")));
    const groups = Object.entries(board.groups || {}).map(([key, title]) => {
      const rows = board.factors.filter((f) => f.group === key);
      return rows.length ? h("div", { class: "ev-group" }, h("h4", { text: title }), h("ul", { class: "ev-list" }, rows.map(factorRow))) : null;
    }).filter(Boolean);
    parts.push(h("div", { class: "ev-groups" }, groups));
    if (board.notes && board.notes.length) parts.push(h("div", { class: "pad" }, h("strong", { class: "small", text: "Liquidation zones near this plan" }), plainList(board.notes, "small")));
    if (board.liq_map) parts.push(liqChart(board.liq_map, levels));
    if (board.missing.length) parts.push(h("p", { class: "muted small pad", text: `No data for: ${board.missing.map(factorName).join(", ")}.` }));
    if (board.errors && board.errors.length) {
      parts.push(h("details", { class: "pad small muted" }, h("summary", { text: `${board.errors.length} source message(s)` }), plainList(board.errors, "small")));
    }
    return h("div", { class: "evidence" }, parts);
  }

  function heldBackNote(filteredBy, wouldBe) {
    if (!filteredBy && !wouldBe) return null;
    return h("div", { class: "alert inset held", role: "note" },
      h("strong", { text: "Held back: " }), `${FILTER_TEXT[filteredBy] || "a filter"} kept this ${wouldBe ? humanize(wouldBe) : "buy"} at WATCH. `
        + "It is still followed in the track record, so you can see whether holding it back was right.");
  }

  function renderLearning(l) {
    const parts = [];
    const need = l.needed || 90;
    parts.push(h("div", { class: "pad" },
      h("strong", { class: "small", text: "What the outcomes say" }),
      h("p", { class: "small", text: `${l.samples || 0} closed setups with an evidence board (${l.shown || 0} shown as buys, ${l.held_back || 0} held back). `
        + `The learned model needs ${need} to train and validate.` }),
      h("div", { class: "score-cell" }, meter(Math.min(l.samples || 0, need), need), h("span", { class: "small muted", text: `${Math.min(l.samples || 0, need)}/${need}` }))));
    const sh = l.shown_stats, hb = l.held_stats;
    if ((sh && sh.n) || (hb && hb.n)) {
      parts.push(table([["Group"], ["Closed", "num"], ["Win rate", "num"], ["Avg", "num"]],
        [["Shown as buys", sh], ["Held back by filters", hb]].filter(([, st]) => st && st.n).map(([label, st]) => h("tr", {},
          h("td", { text: label }), h("td", { class: "num", text: st.n }), h("td", { class: "num", text: `${Math.round(st.win_rate)}%` }),
          h("td", { class: `num ${st.avg_r > 0 ? "pnl-up" : st.avg_r < 0 ? "pnl-down" : ""}`, text: fmtR(st.avg_r) })))));
    }
    const rows = (l.factors || []).filter((f) => f.for.n + f.against.n > 0);
    if (rows.length) {
      parts.push(h("div", { class: "pad" }, h("strong", { class: "small", text: "Measured edge per factor: average result when it argued for the trade versus against it" })),
        table([["Factor"], ["For", "num"], ["Against", "num"], ["Edge", "num"], ["Verdict", "hide-sm"]],
          rows.map((f) => h("tr", {},
            h("td", { text: f.label }),
            h("td", { class: "num", text: f.for.n ? `${fmtR(f.for.avg_r)} (${f.for.n})` : DASH }),
            h("td", { class: "num", text: f.against.n ? `${fmtR(f.against.avg_r)} (${f.against.n})` : DASH }),
            h("td", { class: `num ${f.edge_r > 0 ? "pnl-up" : f.edge_r < 0 ? "pnl-down" : ""}`, text: f.edge_r == null ? DASH : fmtR(f.edge_r) }),
            h("td", { class: "hide-sm", text: f.verdict || "needs 5+ each way" })))));
    }
    const m = l.model;
    if (m) {
      const mt = m.metrics || {};
      parts.push(h("div", { class: "pad ml-block" },
        h("div", { class: "mood-head" }, h("strong", { class: "small", text: "Learned evidence model" }),
          toneBadge(m.validated ? "good" : "neutral", m.validated ? "Validated" : "Not validated"),
          h("span", { class: "muted small", text: l.enabled ? "used when validated" : "switched off" })),
        h("dl", { class: "detail-grid compact" },
          kv("Test AUC", `${mt.test_auc == null ? DASH : mt.test_auc.toFixed(2)} (0.5 = no skill)`),
          kv("Kept vs all (newer setups)", `${fmtR(mt.test_kept_expectancy_r)} vs ${fmtR(mt.test_expectancy_r)}`),
          kv("Strongest factors", (m.top_features || []).slice(0, 4).map((f) => `${factorName(f.name)} ${f.weight > 0 ? "+" : "−"}`).join(", ") || DASH)),
        plainList(m.reasons || [], "small")));
    }
    const ai = evSettings && evSettings.ai_news && evSettings.ai_news.last;
    if (ai && ai.coins && Object.keys(ai.coins).length) {
      const coins = Object.entries(ai.coins).sort((a, b) => Math.abs(b[1].impact) - Math.abs(a[1].impact)).slice(0, 8);
      parts.push(h("div", { class: "pad" }, h("strong", { class: "small", text: `AI reading of ${ai.headlines} headlines (${ai.model}, ${fmtTime(ai.computed_at)})` }),
        h("ul", { class: "plain-list small" }, coins.map(([sym, v]) => h("li", {},
          h("strong", { text: `${sym} ${v.impact > 0 ? "+" : v.impact < 0 ? "−" : ""}${Math.abs(v.impact)}` }), v.critical ? " (critical) " : " ", v.reason)))));
    } else if (ai && ai.error) {
      parts.push(h("p", { class: "muted small pad", text: `AI news reading failed: ${ai.error}` }));
    }
    $("ev-body").replaceChildren(...parts);
  }

  async function loadEvidence() {
    try {
      const [st, learn] = await Promise.all([getJSON("/api/evidence/settings"), getJSON("/api/evidence/learning")]);
      evSettings = st;
      $("ev-mode").value = st.mode;
      $("ev-news").checked = !!st.refresh_news;
      const aiOk = !!(st.ai_news && st.ai_news.configured);
      $("ev-ai").checked = !!(st.ai_news && st.ai_news.enabled) && aiOk;
      $("ev-ai").disabled = !aiOk;
      $("ev-ai").parentElement.title = aiOk ? "One OpenAI request per new set of headlines" : "Needs OPENAI_API_KEY on the server";
      $("ev-learned").checked = !!learn.enabled;
      $("evidence-meta").textContent = `mode ${st.mode} · futures data ${st.derivatives ? "on" : "off"}`;
      renderLearning(learn);
    } catch (err) {
      $("ev-body").replaceChildren(h("p", { class: "muted small pad", text: `Evidence engine unavailable: ${err.message}` }));
    }
  }

  async function saveEvidence() {
    const body = { mode: $("ev-mode").value, refresh_news: $("ev-news").checked };
    if (!$("ev-ai").disabled) body.ai_news = $("ev-ai").checked;
    try {
      evSettings = await api("/api/evidence/settings", { method: "PUT", body });
      $("evidence-meta").textContent = `mode ${evSettings.mode} · futures data ${evSettings.derivatives ? "on" : "off"}`;
      $("ev-status").textContent = "Saved. The next scan uses it.";
    } catch (err) {
      window.alert(`Could not save: ${err.message}`);
      loadEvidence();
    }
  }

  async function toggleLearned() {
    try {
      renderLearning(await api("/api/evidence/learning", { method: "PUT", body: { enabled: $("ev-learned").checked } }));
    } catch (err) {
      window.alert(err.message);
      $("ev-learned").checked = !$("ev-learned").checked;
    }
  }

  async function recomputeLearning() {
    const button = $("ev-learn");
    button.disabled = true;
    try {
      renderLearning(await api("/api/evidence/learning/refresh", { method: "POST" }));
      $("ev-status").textContent = `Recomputed ${fmtTime(new Date().toISOString())}.`;
    } catch (err) {
      $("ev-status").textContent = `Could not recompute: ${err.message}`;
    } finally {
      button.disabled = false;
    }
  }

  async function loadFuturesMarket() {
    const box = $("ev-market-body");
    const button = $("ev-market");
    button.disabled = true;
    box.replaceChildren(h("p", { class: "muted small pad", text: "Asking the exchanges…" }));
    try {
      const m = await getJSON("/api/derivatives/market");
      if (!m.coins) {
        box.replaceChildren(h("p", { class: "muted small pad", text: `No exchange answered. ${(m.errors || []).slice(0, 3).join("; ")}` }));
        return;
      }
      box.replaceChildren(h("dl", { class: "detail-grid compact pad" },
        kv("Average funding", `${signedNum(m.avg_funding_pct, 4)}% per 8h over ${m.coins} perpetuals (0.01% is normal)`),
        kv("Crowded longs", m.hot.length ? m.hot.join(", ") : "none"),
        kv("Crowded shorts", m.cold.length ? m.cold.join(", ") : "none"),
        kv("Open interest", fmtUsd(m.open_interest_usd)),
        kv("Exchanges answering", (m.venues || []).map(humanize).join(", ") || DASH),
        kv("Checked", fmtTime(m.fetched_at))),
        ...(m.errors && m.errors.length ? [h("p", { class: "muted small pad", text: `Not reachable from this server: ${m.errors.slice(0, 4).join("; ")}` })] : []));
    } catch (err) {
      box.replaceChildren(h("p", { class: "muted small pad", text: `Futures positioning unavailable: ${err.message}` }));
    } finally {
      button.disabled = emergency;
    }
  }

  // ---------------------------------------------------------------- futures signals (phase 12)

  let futuresHorizon = "1h";
  let lastFutures = null;
  const futuresOpen = new Set();
  const futuresSeen = {};
  const isTrade = (sig) => sig.signal === "BUY" || sig.signal === "STRONG BUY";

  function sideChip(side) {
    if (!side) return null;
    return h("span", { class: `side-chip ${side}` },
      h("span", { class: "side-mark", "aria-hidden": "true", text: side === "long" ? "▲" : "▼" }), side === "long" ? "Long" : "Short");
  }

  function futuresBadge(sig) {
    const tone = isTrade(sig) ? "good" : sig.signal === "WATCH" ? "warning" : "neutral";
    return toneBadge(tone, humanize(sig.label));
  }

  function futuresResearchTable(rows) {
    const cell = (sp) => (sp && sp.trades ? [fmtR(sp.expectancy_r), h("div", { class: "muted small", text: `${sp.trades} tr · ${Math.round(sp.win_rate ?? 0)}%` })] : [DASH]);
    const cls = (v) => `num ${v > 0 ? "pnl-up" : v < 0 ? "pnl-down" : ""}`;
    const sorted = rows; // the server orders them: validated, then enough trades to judge, then closest to enough
    return table([["Strategy"], ["Side"], ["Older 70%", "num"], ["Newer 30%", "num"], ["All", "num hide-sm"], ["Verdict"]],
      sorted.map((r) => h("tr", { class: r.validated ? "chosen" : null },
        h("td", { title: r.source }, h("strong", { class: "small", text: r.name }), h("div", { class: "muted small rule-text", text: r.rule })),
        h("td", {}, sideChip(r.side)),
        h("td", { class: cls(r.train.expectancy_r) }, ...cell(r.train)),
        h("td", { class: cls(r.test.expectancy_r) }, ...cell(r.test)),
        h("td", { class: "num hide-sm" }, ...cell(r.all)),
        h("td", {}, toneBadge(r.validated ? "good" : "neutral", r.validated ? "Validated" : "Not validated"),
          h("div", { class: "muted small", text: r.reasons[0] || "" })))));
  }

  function futuresDetail(sig) {
    const parts = [];
    if (sig.would_be) parts.push(heldBackNote(sig.filtered_by, sig.would_be));
    const reviewBox = h("div", { class: "pad" }, sig.ai_review ? aiReviewBlock(sig.ai_review) : null);
    const reviewBtn = h("button", { type: "button", class: "small", text: sig.ai_review ? "Review again with AI" : "AI review",
      title: "Ask the OpenAI model for a second opinion on this futures setup (uses your OpenAI credits)" });
    reviewBtn.addEventListener("click", (e) => { e.stopPropagation(); requestReview("futures", sig.symbol, sig.horizon, reviewBox, reviewBtn); });
    parts.push(h("div", { class: "pad review-row" }, reviewBtn), reviewBox);
    if (sig.strategy) {
      const stg = sig.strategy;
      parts.push(h("div", { class: "pad strategy-box" },
        h("strong", { class: "small" }, "Strategy: ", stg.name, " ", sideChip(sig.side)),
        h("p", { class: "small", text: stg.rule }),
        h("p", { class: "muted small", text: `Source: ${stg.source}` }),
        sig.also && sig.also.length ? h("p", { class: "muted small", text: `Also fired now: ${sig.also.join(" · ")}` }) : null));
    }
    if (!sig.plan && sig.pending) {
      const w = sig.pending;
      parts.push(h("div", { class: "pad conditional" },
        h("strong", { class: "small" }, "Not a trade yet: conditional plan ", sideChip(w.side)),
        h("p", { class: "small", text: w.text }),
        h("dl", { class: "detail-grid compact" },
          kv(w.side === "long" ? "Trigger (close above)" : "Trigger (close below)", fmtPrice(w.trigger)),
          kv("Stop", `${fmtPrice(w.stop)} (${w.side === "long" ? "−" : "+"}${w.risk_pct.toFixed(2)}%)`),
          kv("TP1 / TP2", `${fmtPrice(w.tp1)} / ${fmtPrice(w.tp2)}`))));
    }
    const p = sig.plan;
    const lev = sig.leverage;
    if (p && lev) {
      const usd = (v) => (v == null ? "" : ` (${fmtUsd(v)})`);
      parts.push(h("dl", { class: "detail-grid compact" },
        kv("Direction", sideChip(p.side)),
        kv("Entry zone", `${fmtPrice(p.entry_low)} to ${fmtPrice(p.entry_high)} (signal ${fmtPrice(p.entry)})`),
        kv("Stop", `${fmtPrice(p.stop)} (${p.side === "long" ? "−" : "+"}${p.risk_pct.toFixed(2)}%)`),
        kv("Leverage", `${lev.leverage}x isolated · margin ${lev.margin_pct_of_equity.toFixed(1)}% of equity${usd(lev.margin_usd)}`),
        kv("Position size", `${lev.notional_pct_of_equity.toFixed(0)}% of equity${usd(lev.notional_usd)}`),
        kv("Loss at the stop", `≈ ${lev.risk_pct_of_equity.toFixed(2)}% of equity${usd(lev.loss_at_stop_usd)} incl. costs`),
        kv("Liquidation (est.)", `${fmtPrice(lev.liquidation_price)} · ${lev.liquidation_distance_pct.toFixed(1)}% away; the stop is ${p.risk_pct.toFixed(2)}% away`),
        kv("Funding", sig.funding_pct == null ? "unknown" : `${sig.funding_pct >= 0 ? "+" : ""}${sig.funding_pct.toFixed(4)}%/8h · ${lev.funding_pct > 0 ? "you pay" : "you receive"} ≈ ${Math.abs(lev.funding_pct).toFixed(3)}% over the hold`),
        kv("Costs", `${p.cost_pct.toFixed(2)}% round trip (fees + slippage)`),
        sig.basis_pct != null ? kv("Perpetual vs spot", `${sig.basis_pct >= 0 ? "+" : ""}${sig.basis_pct.toFixed(2)}%`) : null,
        kv("Take profit", p.exit_rule, "wide"),
        kv("Valid until", `${fmtTime(p.valid_until)} · ${p.time_exit}`, "wide")));
      if (lev.notes && lev.notes.length) parts.push(h("div", { class: "pad" }, plainList(lev.notes, "small muted")));
    }
    parts.push(h("div", { class: "pad" }, h("strong", { class: "small", text: "Why" }), plainList(sig.reasons, "small")));
    if (sig.risks && sig.risks.length) parts.push(h("div", { class: "pad" }, h("strong", { class: "small", text: "Risks" }), plainList(sig.risks, "small")));
    if (sig.record && sig.record.trades) {
      const r = sig.record;
      parts.push(h("p", { class: "small pad muted", text: `This strategy on ${sig.symbol} (${sig.side}): ${r.trades} past trades, ${Math.round(r.win_rate ?? 0)}% winners, ${fmtR(r.expectancy_r)} per trade after costs.` }));
    }
    if (sig.board) {
      const lv = p ? { entry: p.entry, stop: p.stop, tp2: p.tp2 } : sig.pending ? { entry: sig.pending.trigger, stop: sig.pending.stop, tp2: sig.pending.tp2 } : {};
      parts.push(h("div", { class: "ev-inline" }, h("div", { class: "pad-tight" }, h("strong", { class: "small" }, "Evidence board for this ", sig.side || "", " trade")),
        evidenceBlock(sig.board, lv)));
    }
    return parts;
  }

  function futuresRows(sig) {
    const open = futuresOpen.has(sig.symbol);
    const toggle = () => {
      if (futuresOpen.has(sig.symbol)) futuresOpen.delete(sig.symbol); else futuresOpen.add(sig.symbol);
      renderFutures(lastFutures);
    };
    const p = sig.plan;
    const w = !p ? sig.pending : null;
    const cls = isTrade(sig) ? "num" : "num muted";
    const level = (value, sub) => (value == null ? [DASH] : [fmtPrice(value), sub ? h("div", { class: "muted small", text: sub }) : null]);
    const entry = p ? level(p.entry, p.side === "long" ? "long entry" : "short entry")
      : w ? [h("span", { class: "cond", text: `${w.side === "long" ? "> " : "< "}${fmtPrice(w.trigger)}` }), h("div", { class: "muted small", text: "trigger" })] : [DASH];
    const stopSub = (x) => `${x.side === "long" ? "−" : "+"}${x.risk_pct.toFixed(2)}%`;
    const lev = sig.leverage;
    const row = h(
      "tr",
      { class: `clickable${open ? " open" : ""}${w ? " waiting" : ""}`, onclick: toggle, "aria-expanded": open ? "true" : "false" },
      h("td", {}, h("div", { class: "asset-cell" }, h("strong", { text: sig.symbol }), h("span", { class: "muted small", text: sig.name }))),
      h("td", {}, h("div", { class: "fut-signal" }, sideChip(sig.side), futuresBadge(sig)),
        sig.status ? h("div", { class: "muted small status-line", text: sig.status }) : null, aiChip(sig.ai_review),
        sig.board ? evidenceChip(sig.board.score, sig.board.grade, sig.board.vetoes && sig.board.vetoes.length) : null),
      h("td", { class: "hide-sm", title: sig.strategy ? sig.strategy.source : null },
        sig.strategy ? sig.strategy.name : w ? humanize(w.kind) : DASH,
        sig.also && sig.also.length ? h("div", { class: "muted small", text: `+${sig.also.length} more` }) : null),
      h("td", { class: cls }, ...entry),
      h("td", { class: cls }, ...(p ? level(p.stop, stopSub(p)) : w ? level(w.stop, stopSub(w)) : [DASH])),
      h("td", { class: cls }, ...(p ? level(p.tp1, fmtPrice(p.tp2)) : w ? level(w.tp1, fmtPrice(w.tp2)) : [DASH])),
      h("td", { class: cls }, ...(lev ? [`${lev.leverage}x`, h("div", { class: "muted small", text: `liq ${fmtPrice(lev.liquidation_price)}` })] : [DASH])),
      h("td", { class: "hide-sm" }, h("span", { class: "reason", text: sig.reasons[0] || "" })),
    );
    if (!open) return [row];
    return [row, h("tr", { class: "detail-row" }, h("td", { colspan: 8 }, h("div", { class: "scalp-detail" }, ...futuresDetail(sig))))];
  }

  function renderFutures(res) {
    lastFutures = res;
    const rows = $("futures-rows");
    const why = $("futures-why");
    const box = $("futures-research");
    if (!res) {
      rows.replaceChildren(h("tr", {}, h("td", { colspan: 8, class: "empty", text: "No futures scan yet for this horizon. Press “Find futures trades”." })));
      $("futures-meta").textContent = "";
      why.hidden = true;
      box.hidden = true;
      return;
    }
    const c = res.counts;
    $("futures-meta").textContent = `${res.label}: setup ${res.setup_timeframe}, trend ${res.trend_timeframe} · ${c.LONG} long, ${c.SHORT} short, `
      + `${c.WATCH} watch · costs ${res.cost_pct.toFixed(2)}% · ${fmtTime(res.generated_at)}`;
    if (res.no_trade_reason) {
      why.replaceChildren(h("strong", { text: "Why no trade: " }), res.no_trade_reason);
      why.hidden = false;
    } else {
      why.hidden = true;
    }
    const research = res.research || [];
    if (research.length) {
      $("futures-research-meta").textContent = ` · ${research.filter((r) => r.validated).length} of ${research.length} validated on these coins`;
      $("futures-research-body").replaceChildren(futuresResearchTable(research),
        h("p", { class: "muted small", text: "Per trade in R after futures fees, slippage and a funding allowance, pooled over the scanned coins. "
          + "Shorts are researched on the mirrored chart, so each side is proven on its own." }));
      box.hidden = false;
    } else {
      box.hidden = true;
    }
    const err = $("futures-error");
    if (res.errors.length) setMessage(err, "Some coins could not be analysed", res.errors.slice(0, 5)); else err.hidden = true;
    rows.replaceChildren(...(res.signals.length ? res.signals.flatMap(futuresRows)
      : [h("tr", {}, h("td", { colspan: 8, class: "empty", text: "No coins analysed (check Coins to analyse)." }))]));
  }

  async function refreshFutures() {
    const horizon = futuresHorizon;
    try {
      const r = await getJSON(`/api/futures?horizon=${horizon}`);
      if (horizon !== futuresHorizon) return;
      renderFutures(r.result);
      renderFuturesStatus(r.status);
      if (r.settings) $("futures-lev").value = String(r.settings.max_leverage);
    } catch (err) {
      if (horizon === futuresHorizon) setMessage($("futures-error"), `Futures signals unavailable: ${err.message}`);
    }
  }

  function renderFuturesStatus(all) {
    if (!all) return;
    const st = all[futuresHorizon];
    const button = $("futures-scan");
    button.disabled = !!(st && st.running) || emergency;
    button.textContent = st && st.running ? "Scanning…" : "Find futures trades";
    let text = "";
    if (st) {
      if (st.running) text = `Researching both sides… ${st.done}/${st.total || "?"}`;
      else if (st.outcome === "stopped") text = `Stopped ${fmtTime(st.finished_at)}`;
      else if (st.outcome === "failed") text = `Scan failed: ${st.error || "unknown error"}`;
    }
    $("futures-status").textContent = text;
    $("futures-progress").style.width = st && st.running && st.total ? `${(st.done / st.total) * 100}%` : "0%";
    for (const [key, value] of Object.entries(all)) {
      const seen = futuresSeen[key];
      if (value.finished_at && seen !== undefined && seen !== value.finished_at && key === futuresHorizon) {
        refreshFutures();
        refreshRecord();
      }
      futuresSeen[key] = value.finished_at;
    }
  }

  function setFuturesHorizon(key) {
    futuresHorizon = key;
    renderFutures(null);
    pref("futuresHorizon", key);
    document.querySelectorAll("[data-fhorizon]").forEach((b) => b.setAttribute("aria-checked", b.dataset.fhorizon === key ? "true" : "false"));
    futuresOpen.clear();
    refreshFutures();
  }

  async function startFutures() {
    try {
      const r = await api(`/api/futures/scan?horizon=${futuresHorizon}`, { method: "POST" });
      renderFuturesStatus(r.status);
      scheduleControl(1000);
    } catch (err) {
      setMessage($("futures-error"), `Could not start the scan: ${err.message}`);
    }
  }

  async function saveFuturesLeverage() {
    try {
      const s = await api("/api/futures/settings", { method: "PUT", body: { max_leverage: Number($("futures-lev").value) } });
      $("futures-status").textContent = `Plans now use at most ${s.max_leverage}x (next scan).`;
    } catch (err) {
      window.alert(`Could not save: ${err.message}`);
      refreshFutures();
    }
  }

  // ---------------------------------------------------------------- AI review (phase 7)

  const VERDICT_TONE = { agree: "good", caution: "warning", reject: "critical" };

  function aiChip(review) {
    if (!review) return null;
    return h("span", { class: "ai-chip", title: review.summary }, toneBadge(VERDICT_TONE[review.verdict] || "neutral", `AI: ${review.verdict}`));
  }

  function aiReviewBlock(review) {
    return h("div", { class: `ai-review ${review.verdict}` },
      h("div", { class: "mood-head" }, toneBadge(VERDICT_TONE[review.verdict] || "neutral", `AI ${review.verdict}`),
        h("span", { class: "muted small", text: `${Math.round((review.confidence ?? 0) * 100)}% confident · ${review.model} · ${review.effect}` })),
      h("p", { class: "small", text: review.summary }),
      review.risks.length ? h("div", {}, h("strong", { class: "small", text: "Risks it sees" }), plainList(review.risks, "small")) : null,
      review.checks_before_entry.length ? h("div", {}, h("strong", { class: "small", text: "Check before buying" }), plainList(review.checks_before_entry, "small")) : null,
      h("p", { class: "muted small", text: "A second opinion from the language model. It can only lower a signal, never raise one." }),
    );
  }

  async function requestReview(kind, symbol, horizon, container, button) {
    button.disabled = true;
    button.classList.add("busy");
    try {
      const review = await api("/api/ai/review", { method: "POST", body: { kind, symbol, horizon: horizon || null, model: $("chat-model").value || null }, timeoutMs: 180000 });
      container.replaceChildren(aiReviewBlock(review));
      if (kind === "scalp") refreshScalp(); else if (kind === "futures") refreshFutures(); else refreshSignals();
    } catch (err) {
      container.replaceChildren(h("p", { class: "muted small", text: `AI review unavailable: ${err.message}` }));
    } finally {
      button.disabled = false;
      button.classList.remove("busy");
    }
  }

  async function loadAiSettings() {
    try {
      const st = await getJSON("/api/ai/settings");
      $("ai-mode").value = st.mode;
      $("ai-auto").checked = !!st.auto;
      $("ai-mode").disabled = $("ai-auto").disabled = !st.available;
      if (!st.available) $("ai-mode").title = $("ai-auto").title = "Needs OPENAI_API_KEY on the server";
    } catch { /* optional */ }
  }

  async function saveAiSettings() {
    try {
      await api("/api/ai/settings", { method: "PUT", body: { mode: $("ai-mode").value, auto: $("ai-auto").checked } });
    } catch (err) {
      window.alert(err.message);
      loadAiSettings();
    }
  }

  // ---------------------------------------------------------------- coins to analyse (selection)

  let selectionState = null; // { mode, symbols:Set, universe:[...] }
  let selectionDraft = null; // Set of symbols being edited

  function isSelected(symbol) {
    return !selectionState || selectionState.mode === "all" || selectionState.symbols.has(symbol);
  }

  function describeSelection() {
    if (!selectionState) return "All coins";
    if (selectionState.mode === "all") return `All coins (${selectionState.universe.length || "Top 20 + watchlist"})`;
    const list = [...selectionState.symbols];
    return `${list.length} selected: ${list.slice(0, 6).join(", ")}${list.length > 6 ? "…" : ""}`;
  }

  function renderSelection() {
    const st = selectionState;
    $("selection-summary").textContent = describeSelection();
    $("selection-summary").classList.toggle("active", !!st && st.mode === "selected");
    if (!st) return;
    const draft = selectionDraft;
    $("selection-count").textContent = `${draft.size} of ${st.universe.length} selected`;
    $("selection-chips").replaceChildren(
      ...(st.universe.length
        ? st.universe.map((u) => h(
            "button",
            {
              type: "button", class: `select-chip${u.supported ? "" : " unsupported"}`, "aria-pressed": draft.has(u.symbol) ? "true" : "false",
              title: `${u.name}${u.watchlist ? " (watchlist)" : ""}${u.supported ? "" : " - no spot market"}`,
              onclick: () => {
                if (draft.has(u.symbol)) draft.delete(u.symbol); else draft.add(u.symbol);
                renderSelection();
              },
            },
            h("span", { class: "chip-rank", text: u.watchlist ? "★" : `#${u.rank}` }),
            u.symbol,
          ))
        : [h("span", { class: "muted small", text: "Load market data first (Refresh data)." })]),
    );
  }

  async function loadSelection() {
    try {
      const r = await getJSON("/api/control/selection");
      selectionState = { mode: r.mode, symbols: new Set(r.symbols), universe: r.universe };
      selectionDraft = new Set(r.universe.filter((u) => u.selected).map((u) => u.symbol));
      if (r.mode === "selected") r.symbols.forEach((s) => selectionDraft.add(s));
      renderSelection();
      $("market-rows").querySelectorAll("tr").forEach((tr) => {
        const btn = tr.querySelector(".link-button");
        if (btn) tr.classList.toggle("deselected", !isSelected(btn.textContent.trim()));
      });
    } catch {
      /* selection is optional; scans then cover every coin */
    }
  }

  async function saveSelection() {
    if (!selectionState) return;
    const all = selectionState.universe.every((u) => selectionDraft.has(u.symbol));
    const body = all ? { mode: "all", symbols: [] } : { mode: "selected", symbols: [...selectionDraft] };
    const button = $("sel-save");
    button.disabled = true;
    try {
      const r = await api("/api/control/selection", { method: "PUT", body });
      selectionState = { mode: r.mode, symbols: new Set(r.symbols), universe: r.universe };
      renderSelection();
      await loadSelection();
      button.textContent = "Saved ✓";
      setTimeout(() => { button.textContent = "Save selection"; }, 1500);
    } catch (err) {
      window.alert(err.message);
    } finally {
      button.disabled = false;
    }
  }

  function presetSelection(kind) {
    if (!selectionState) return;
    const ranked = selectionState.universe.filter((u) => !u.watchlist && u.supported);
    if (kind === "all") selectionDraft = new Set(selectionState.universe.map((u) => u.symbol));
    else if (kind === "none") selectionDraft = new Set();
    else selectionDraft = new Set(ranked.slice(0, kind).map((u) => u.symbol));
    renderSelection();
  }

  // ---------------------------------------------------------------- token unlocks, airdrops

  let unlocksLoaded = false;
  let eventsLoading = false;

  function unlockItem(c) {
    const next = c.next_unlock;
    const pct = c.window_pct_circulating;
    const tone = pct == null ? "neutral" : pct >= 2 ? "critical" : pct >= 1 ? "serious" : pct > 0 ? "warning" : "neutral";
    const allocs = next ? Object.entries(next.allocations).map(([k, v]) => `${k} ${fmtNum(v)}`).join(" · ") : "";
    return h(
      "li",
      {},
      h(
        "div",
        { class: "whale-row" },
        h("strong", { text: c.symbol }),
        toneBadge(tone, pct == null ? "supply n/a" : `${pct.toFixed(2)}% of supply`),
        h("span", { class: "muted", text: c.window_value_usd ? fmtUsd(c.window_value_usd) : "" }),
      ),
      next
        ? h(
            "div",
            { class: "news-sub" },
            h("span", { text: `next ${new Date(next.date).toLocaleDateString()} (in ${Math.max(0, Math.round(next.days_until))} d)` }),
            h("span", { text: `${fmtNum(next.tokens)} tokens` }),
            allocs ? h("span", { text: allocs }) : null,
          )
        : null,
    );
  }

  function renderUnlocks(d) {
    const panel = $("unlocks-panel");
    if (!d.configured) {
      panel.replaceChildren(h("p", { class: "muted small pad", text: d.message }));
      return;
    }
    const soon = d.coins.filter((c) => c.window_tokens > 0);
    const later = d.coins.filter((c) => !(c.window_tokens > 0));
    panel.replaceChildren(...[
      h("p", { class: "muted small pad", text: `${d.checked.length} coins checked · next ${d.window_days} days · ${fmtTime(d.fetched_at)}${d.errors.length ? ` · ${d.errors.length} not found` : ""}` }),
      h("ul", { class: "news-list" },
        ...(soon.length ? soon.map(unlockItem) : [h("li", { class: "muted small", text: `No unlocks in the next ${d.window_days} days for the analysed coins.` })])),
      later.length ? h("details", { class: "whales" }, h("summary", { class: "small", text: `Later unlocks (${later.length})` }), h("ul", { class: "news-list" }, ...later.map(unlockItem))) : null,
      h("p", { class: "muted small pad", text: "Unlocks of 1%+ of circulating supply within 14 days are added to that coin's risk notes." }),
    ].filter(Boolean));
  }

  const DROP_TONE = { active: "good", claimable: "warning", upcoming: "neutral" };

  function renderAirdrops(d) {
    const panel = $("airdrops-panel");
    if (!d.configured) {
      panel.replaceChildren(h("p", { class: "muted small pad", text: d.message }));
      return;
    }
    panel.replaceChildren(...[
      h("ul", { class: "news-list" },
        ...(d.airdrops.length
          ? d.airdrops.map((a) => h(
              "li",
              {},
              h("div", { class: "whale-row" },
                a.url ? h("a", { href: a.url, target: "_blank", rel: "noopener noreferrer", text: a.name }) : h("strong", { text: a.name }),
                a.status ? toneBadge(DROP_TONE[a.status.toLowerCase()] || "neutral", humanize(a.status)) : null),
              h("div", { class: "news-sub" },
                a.chains.length ? h("span", { text: a.chains.join(" · ") }) : null,
                a.reward ? h("span", { text: `reward ${a.reward}` }) : null,
                a.cost ? h("span", { text: `cost ${a.cost}` }) : null,
                a.ends_at ? h("span", { text: `ends ${new Date(a.ends_at).toLocaleDateString()}` }) : null),
            ))
          : [h("li", { class: "muted small", text: "No airdrops returned." })])),
      d.errors.length ? h("p", { class: "muted small pad", title: d.errors.join("\n"), text: `${d.errors.length} request(s) failed` }) : null,
      h("p", { class: "muted small pad", text: "Airdrops are unverified third-party listings: never connect a wallet or pay fees you do not understand." }),
    ].filter(Boolean));
  }

  async function refreshEvents(force = false, quiet = false) {
    if (eventsLoading) return;
    eventsLoading = true;
    const button = $("events-refresh");
    button.disabled = true;
    button.classList.add("busy");
    const q = force ? "?refresh=true" : "";
    const [unlocks, drops] = await Promise.allSettled([
      getJSON(`/api/events/unlocks${q}`, 90000), quiet ? Promise.reject(new Error("skipped")) : getJSON(`/api/events/airdrops${q}`, 60000),
    ]);
    if (unlocks.status === "fulfilled") renderUnlocks(unlocks.value);
    else $("unlocks-panel").replaceChildren(h("p", { class: "muted small pad", text: `Unlocks unavailable: ${unlocks.reason.message}` }));
    if (drops.status === "fulfilled") renderAirdrops(drops.value);
    else if (!quiet) $("airdrops-panel").replaceChildren(h("p", { class: "muted small pad", text: `Airdrops unavailable: ${drops.reason.message}` }));
    unlocksLoaded = true;
    button.disabled = false;
    button.classList.remove("busy");
    button.textContent = "Refresh";
    eventsLoading = false;
  }

  function selectTab(which) {
    for (const name of ["unlocks", "airdrops"]) {
      const on = name === which;
      $(`tab-${name}`).setAttribute("aria-selected", on ? "true" : "false");
      $(`${name}-panel`).hidden = !on;
    }
  }

  // ---------------------------------------------------------------- chat

  let chatMessages = [];
  let chatBusy = false;

  function renderChat() {
    const log = $("chat-log");
    const nodes = chatMessages.map((m) =>
      h("div", { class: `chat-msg ${m.role}${m.error ? " error" : ""}` }, m.content, m.meta ? h("div", { class: "chat-meta", text: m.meta }) : null),
    );
    if (chatBusy) nodes.push(h("div", { class: "chat-msg assistant muted", text: "Thinking…" }));
    if (!nodes.length) {
      nodes.push(h("p", { class: "muted small chat-hint", text: "Ask about the latest scan, a coin's setup, news or your portfolio. The assistant sees the dashboard's data; its answers never change a signal." }));
    }
    log.replaceChildren(...nodes);
    log.scrollTop = log.scrollHeight;
    try {
      sessionStorage.setItem("chat", JSON.stringify(chatMessages.filter((m) => !m.error).slice(-30)));
    } catch {
      /* not persisted */
    }
  }

  async function sendChat(text) {
    if (chatBusy || !text.trim()) return;
    chatMessages.push({ role: "user", content: text.trim() });
    chatBusy = true;
    renderChat();
    const symbol = $("chat-symbol").value || null;
    try {
      const history = chatMessages.filter((m) => !m.error).map(({ role, content }) => ({ role, content })).slice(-16);
      const model = $("chat-model").value || null;
      const effort = $("chat-effort").value || null;
      const r = await api("/api/chat", {
        method: "POST", body: { messages: history, symbol, model, reasoning_effort: effort }, timeoutMs: 180000,
      });
      const fell = r.fallback_from && r.fallback_from.length ? ` · fallback (${r.fallback_from.join("; ")})` : "";
      chatMessages.push({ role: "assistant", content: r.reply, meta: `${r.model}${symbol ? ` · focus ${symbol}` : ""}${fell}` });
    } catch (err) {
      chatMessages.push({ role: "assistant", content: `Chat unavailable: ${err.message}`, error: true });
    } finally {
      chatBusy = false;
      renderChat();
    }
  }

  const DEFAULT_MODELS = ["gpt-5-mini", "gpt-4o-mini", "gpt-5", "gpt-5-nano", "gpt-4.1-mini", "gpt-4.1", "gpt-4o", "o4-mini"];

  function fillModels(list, defaultId) {
    const select = $("chat-model");
    const saved = pref("chatModel") || "";
    select.replaceChildren(
      h("option", { value: "", text: `Default (${defaultId || "server"})` }),
      ...list.map((m) => h("option", {
        value: m.id,
        text: `${m.id}${m.reasoning ? " · reasoning" : ""}${m.available === false ? " · not on this key" : ""}`,
        disabled: m.available === false,
      })),
    );
    if ([...select.options].some((o) => o.value === saved && !o.disabled)) select.value = saved;
    syncEffort();
  }

  function syncEffort() {
    const id = $("chat-model").value;
    const reasoning = !id || /^(gpt-5|o\d)/.test(id);
    $("chat-effort").disabled = !reasoning;
    $("chat-effort").title = reasoning ? "Reasoning effort: lower is faster and cheaper" : "This model does not use reasoning effort";
  }

  async function loadChatModels() {
    try {
      const r = await getJSON("/api/chat/models");
      fillModels(r.models, r.default);
    } catch {
      fillModels(DEFAULT_MODELS.map((id) => ({ id, reasoning: /^(gpt-5|o\d)/.test(id), available: null })), null);
    }
  }

  function setChatSymbols(symbols) {
    const select = $("chat-symbol");
    const current = select.value;
    select.replaceChildren(h("option", { value: "", text: "All coins" }), ...symbols.map((s) => h("option", { value: s, text: s })));
    if (symbols.includes(current)) select.value = current;
  }

  // ---------------------------------------------------------------- portfolio (Phase 4)

  const RISK_FIELDS = [
    ["max_risk_per_signal_pct", "Max loss per trade, % of equity"],
    ["max_allocation_per_opportunity_pct", "Max position size, %"],
    ["initial_allocation_pct", "First buy (DCA), %"],
    ["max_dca_allocation_pct", "Extra DCA buys, %"],
    ["max_total_open_allocation_pct", "Max total exposure, %"],
    ["fee_pct", "Fee per side, %"],
    ["slippage_pct", "Slippage per side, %"],
  ];

  function money(v) {
    return v == null ? DASH : nf({ maximumFractionDigits: 2 }).format(v);
  }

  function pnlSpan(value, pct) {
    if (value == null) return h("span", { class: "muted", text: DASH });
    return h("span", { class: value >= 0 ? "pnl-up" : "pnl-down" }, `${value >= 0 ? "+" : "−"}${money(Math.abs(value))}`,
      pct != null ? h("div", { class: "small", text: fmtPct(pct, 1) }) : null);
  }

  function renderPortfolio(p) {
    $("portfolio-error").hidden = true;
    $("portfolio-meta").textContent = `${p.currency} · ${fmtTime(p.generated_at)}`;
    $("portfolio-summary").replaceChildren(
      kv("Equity", money(p.equity)),
      kv("Cash", money(p.cash)),
      kv("Invested", money(p.invested)),
      kv("Exposure", `${p.exposure_pct.toFixed(1)}% (limit ${p.risk_settings.max_total_open_allocation_pct}%)`),
      kv("Unrealized P/L", pnlSpan(p.unrealized_pnl)),
    );
    if (document.activeElement !== $("cash-input")) $("cash-input").value = p.cash;
    const rows = p.positions.map((r) =>
      h(
        "tr",
        {},
        h("td", {}, h("button", { type: "button", class: "link-button", text: r.symbol, onclick: () => openDetail(r.symbol) })),
        h("td", { class: "num", text: nf({ maximumFractionDigits: 8 }).format(r.quantity) }),
        h("td", { class: "num hide-sm", text: fmtPrice(r.average_entry) }),
        h("td", { class: "num" }, h("span", { "data-live-price": r.symbol, text: fmtPrice(r.price) })),
        h("td", { class: "num hide-sm", text: money(r.value) }),
        h("td", { class: "num", title: r.scenarios.map((s) => `${s.label}: ${money(s.pnl)}`).join("\n") || null }, pnlSpan(r.pnl, r.pnl_pct)),
        h("td", { class: "num hide-sm", text: r.allocation_pct == null ? DASH : `${r.allocation_pct.toFixed(1)}%` }),
        h("td", { class: "hide-sm" }, r.signal ? signalBadge(r.signal) : h("span", { class: "muted small", text: "not analysed" })),
        h("td", {}, h("button", {
          type: "button", class: "ghost small", text: "Remove",
          onclick: async () => {
            if (!window.confirm(`Remove the ${r.symbol} position?`)) return;
            try { renderPortfolio(await api(`/api/portfolio/positions/${encodeURIComponent(r.symbol)}`, { method: "DELETE" })); }
            catch (err) { portfolioError(err); }
          },
        })),
      ),
    );
    $("portfolio-rows").replaceChildren(...(rows.length ? rows : [h("tr", {}, h("td", { colspan: 9, class: "empty", text: "No positions yet. Add what you hold below." }))]));
    const form = $("risk-form");
    if (!form.contains(document.activeElement)) {
      form.replaceChildren(
        ...RISK_FIELDS.map(([key, label]) => h("label", {}, label, h("input", { type: "number", step: "any", name: key, value: p.risk_settings[key] }))),
        h("button", { type: "submit", text: "Save risk settings" }),
      );
    }
    const box = $("portfolio-error");
    const notes = [...p.warnings, ...p.errors];
    if (notes.length) { setMessage(box, "Portfolio notes", notes); box.classList.add("warn"); }
  }

  function portfolioError(err) {
    const box = $("portfolio-error");
    box.classList.remove("warn");
    setMessage(box, err.status === 401 ? "Admin token required to view the portfolio" : "Portfolio", [err.message]);
  }

  async function refreshPortfolio(prompt = false) {
    try {
      renderPortfolio(await (prompt ? api : getJSON)("/api/portfolio"));
    } catch (err) {
      portfolioError(err);
    }
  }

  function renderPlan(plan) {
    const t = plan.targets;
    return card(
      `Trade plan: ${plan.symbol}`,
      `${humanize(plan.signal)} · score ${plan.score} · ${plan.quote_asset}`,
      plan.warnings.length ? h("div", { class: "alert warn inset" }, plainList(plan.warnings)) : null,
      h("dl", { class: "detail-grid" },
        kv("Budget", `${money(plan.budget)} (suggested ${money(plan.suggested_budget)})`),
        kv("Average entry if all fill", fmtPrice(plan.average_entry)),
        kv("Stop", fmtPrice(plan.stop_loss)),
        kv("Loss at stop", `${money(plan.loss_at_stop)} (${plan.loss_at_stop_pct_of_equity == null ? DASH : plan.loss_at_stop_pct_of_equity.toFixed(2) + "% of equity"})`),
        kv("Targets", t.map((x) => fmtPrice(x.price)).join(" / ")),
      ),
      table([["DCA buy"], ["Price", "num"], ["Amount", "num"], ["Quantity", "num"]],
        plan.tranches.map((x) => h("tr", {}, h("td", { text: x.label }), h("td", { class: "num", text: fmtPrice(x.price) }),
          h("td", { class: "num", text: money(x.amount) }), h("td", { class: "num", text: nf({ maximumSignificantDigits: 6 }).format(x.quantity) })))),
      table([["Scenario"], ["P/L after costs", "num"], ["% of equity", "num"]],
        plan.scenarios.map((x) => h("tr", {}, h("td", { text: x.label }), h("td", { class: "num" }, pnlSpan(x.pnl)),
          h("td", { class: "num", text: x.pnl_pct_of_equity == null ? DASH : fmtPct(x.pnl_pct_of_equity) })))),
      h("div", { class: "card-foot small muted", text: plan.note }),
    );
  }

  // ---------------------------------------------------------------- chart (Phase 3)

  const SVG_NS = "http://www.w3.org/2000/svg";
  function s(tag, attrs, ...children) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, String(v));
    for (const c of children.flat()) if (c) el.append(c);
    return el;
  }

  function emaSeries(values, period) {
    const out = [];
    let current = null;
    const alpha = 2 / (period + 1);
    values.forEach((v, i) => {
      if (i === period - 1) current = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
      else if (i >= period) current += alpha * (v - current);
      out.push(i >= period - 1 ? current : null);
    });
    return out;
  }

  function drawChart(container, data, plan) {
    const all = data.candles;
    const closes = all.map((c) => c.close);
    const ema20 = emaSeries(closes, 20), ema50 = emaSeries(closes, 50);
    const n = Math.min(120, all.length);
    const candles = all.slice(-n), e20 = ema20.slice(-n), e50 = ema50.slice(-n);
    const W = 760, H = 320, padL = 8, padR = 70, padT = 10, padB = 22;
    const levels = plan ? [plan.stop_loss, plan.entry_low, plan.entry_high, ...plan.targets.map((t) => t.price)] : [];
    let lo = Math.min(...candles.map((c) => c.low)), hi = Math.max(...candles.map((c) => c.high));
    for (const v of levels) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    const span = hi - lo || hi * 0.01;
    lo -= span * 0.04; hi += span * 0.04;
    const x = (i) => padL + ((i + 0.5) * (W - padL - padR)) / n;
    const y = (v) => padT + ((hi - v) * (H - padT - padB)) / (hi - lo);
    const bw = Math.max(1.5, ((W - padL - padR) / n) * 0.62);
    const grid = [0, 0.25, 0.5, 0.75, 1].map((f) => {
      const v = hi - f * (hi - lo);
      return [s("line", { x1: padL, x2: W - padR, y1: y(v), y2: y(v), stroke: "var(--grid)", "stroke-width": 1 }),
        s("text", { x: W - padR + 6, y: y(v) + 4, "font-size": 11, fill: "var(--muted)" }, document.createTextNode(fmtPrice(v)))];
    });
    const line = (series, color) => {
      const pts = series.map((v, i) => (v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean);
      return pts.length > 1 ? s("polyline", { points: pts.join(" "), fill: "none", stroke: color, "stroke-width": 2 }) : null;
    };
    const planMarks = [];
    if (plan) {
      planMarks.push(s("rect", { x: padL, width: W - padL - padR, y: y(plan.entry_high), height: Math.max(1, y(plan.entry_low) - y(plan.entry_high)), fill: "var(--accent)", opacity: 0.12 }));
      const mark = (v, color, label) => [
        s("line", { x1: padL, x2: W - padR, y1: y(v), y2: y(v), stroke: color, "stroke-width": 1.5, "stroke-dasharray": "5 4" }),
        s("text", { x: padL + 4, y: y(v) - 4, "font-size": 11, fill: color }, document.createTextNode(label)),
      ];
      planMarks.push(...mark(plan.stop_loss, "var(--critical)", "stop"));
      plan.targets.forEach((t, i) => planMarks.push(...mark(t.price, "var(--good)", `TP${i + 1}`)));
    }
    const bodies = candles.map((c, i) => {
      const up = c.close >= c.open;
      const color = up ? "var(--up)" : "var(--down)";
      const top = y(Math.max(c.open, c.close)), bottom = y(Math.min(c.open, c.close));
      return s("g", {}, s("line", { x1: x(i), x2: x(i), y1: y(c.high), y2: y(c.low), stroke: color, "stroke-width": 1 }),
        s("rect", { x: x(i) - bw / 2, y: top, width: bw, height: Math.max(1, bottom - top), fill: color, rx: 1 }));
    });
    const cross = s("line", { y1: padT, y2: H - padB, stroke: "var(--muted)", "stroke-width": 1, opacity: 0, "pointer-events": "none" });
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${data.symbol} ${data.label} candlestick chart` },
      grid, planMarks, bodies, line(e20, "var(--accent)"), line(e50, "var(--series-2)"), cross);
    const tip = h("div", { class: "chart-tip", text: "Hover the chart for candle values." });
    svg.addEventListener("mousemove", (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = ((ev.clientX - rect.left) / rect.width) * W;
      const i = Math.max(0, Math.min(n - 1, Math.floor(((px - padL) / (W - padL - padR)) * n)));
      const c = candles[i];
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", 0.6);
      tip.textContent = `${new Date(c.open_time).toLocaleString()} · O ${fmtPrice(c.open)} H ${fmtPrice(c.high)} L ${fmtPrice(c.low)} C ${fmtPrice(c.close)}` +
        (e20[i] != null ? ` · EMA20 ${fmtPrice(e20[i])}` : "") + (e50[i] != null ? ` · EMA50 ${fmtPrice(e50[i])}` : "");
    });
    svg.addEventListener("mouseleave", () => cross.setAttribute("opacity", 0));
    container.replaceChildren(
      h("div", { class: "chart-legend" },
        h("span", {}, h("i", { class: "legend-ema20" }), "EMA20"), h("span", {}, h("i", { class: "legend-ema50" }), "EMA50"),
        plan ? h("span", { text: "shaded: entry zone · dashed: stop and targets" }) : null,
        h("span", { text: `${data.source} ${data.market_symbol} · closed candles` })),
      svg, tip);
  }

  function chartCard(symbol, plan) {
    const body = h("div", {}, h("p", { class: "muted small", style: null, text: "Loading chart…" }));
    const buttons = ["15m", "1h", "4h", "1d"].map((tf) =>
      h("button", { type: "button", class: "ghost small", "aria-pressed": tf === "4h" ? "true" : "false", text: tf.toUpperCase(),
        onclick: (e) => { for (const b of buttons) b.setAttribute("aria-pressed", "false"); e.currentTarget.setAttribute("aria-pressed", "true"); load(tf); } }));
    async function load(tf) {
      try {
        drawChart(body, await getJSON(`/api/assets/${encodeURIComponent(symbol)}/candles?timeframe=${tf}&limit=200`), plan);
      } catch (err) {
        body.replaceChildren(h("p", { class: "muted small", text: `Chart unavailable: ${err.message}` }));
      }
    }
    load("4h");
    const section = card("Price chart", null, h("div", { class: "chart-card" }, body));
    section.querySelector(".card-head").append(h("div", { class: "chart-tools" }, buttons));
    return section;
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

    $("refresh").addEventListener("click", () => {
      refreshAll();
      refreshPortfolio();
    });
    try {
      $("auto-refresh").checked = localStorage.getItem("autoRefresh") === "1";
    } catch {
      /* default off */
    }
    $("auto-refresh").addEventListener("change", () => {
      try { localStorage.setItem("autoRefresh", $("auto-refresh").checked ? "1" : "0"); } catch { /* ignore */ }
      if (autoRefreshOn()) refresh();
      schedule();
    });

    $("analyze").addEventListener("click", () => control("/api/control/analyze"));
    $("stop").addEventListener("click", () => control("/api/control/stop"));
    $("live").addEventListener("click", () =>
      control(controlState && controlState.live.running ? "/api/control/live/stop" : "/api/control/live/start"));
    $("auto-analyze").addEventListener("change", (e) => control("/api/control/auto", { minutes: Number(e.target.value) }));
    $("unlock").addEventListener("click", () => {
      if (askToken()) {
        refreshControl();
        refreshPortfolio();
      }
    });

    $("watch-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        renderWatchlist((await api("/api/watchlist", { method: "POST", body: { symbol: $("watch-symbol").value } })).items);
        loadSelection();
        $("watch-symbol").value = "";
      } catch (err) {
        window.alert(err.message);
      }
    });
    getJSON("/api/watchlist").then((w) => renderWatchlist(w.items)).catch(() => renderWatchlist([]));

    // News, mood and events load only when asked (or when their "Load when the page opens" box is ticked).
    $("news-refresh").addEventListener("click", () => refreshNews($("news-refresh").textContent === "Refresh"));
    $("news-auto").checked = pref("newsAuto") === "1";
    $("news-auto").addEventListener("change", (e) => pref("newsAuto", e.target.checked ? "1" : "0"));
    if ($("news-auto").checked) refreshNews();

    $("mood-refresh").addEventListener("click", () => refreshMood(moodLoaded));
    $("mood-auto").checked = pref("moodAuto") === "1";
    $("mood-auto").addEventListener("change", (e) => pref("moodAuto", e.target.checked ? "1" : "0"));
    if ($("mood-auto").checked) refreshMood();

    $("events-refresh").addEventListener("click", () => refreshEvents(unlocksLoaded));
    $("tab-unlocks").addEventListener("click", () => selectTab("unlocks"));
    $("tab-airdrops").addEventListener("click", () => selectTab("airdrops"));

    const savedHorizon = pref("scalpHorizon");
    document.querySelectorAll(".segmented [data-horizon]").forEach((b) => b.addEventListener("click", () => setHorizon(b.dataset.horizon)));
    setHorizon(["15m", "4h", "1d"].includes(savedHorizon) ? savedHorizon : "1h");
    $("scalp-scan").addEventListener("click", startScalp);
    const savedFutures = pref("futuresHorizon");
    document.querySelectorAll("[data-fhorizon]").forEach((b) => b.addEventListener("click", () => setFuturesHorizon(b.dataset.fhorizon)));
    setFuturesHorizon(["15m", "4h", "1d"].includes(savedFutures) ? savedFutures : "1h");
    $("futures-scan").addEventListener("click", startFutures);
    $("futures-lev").addEventListener("change", saveFuturesLeverage);
    const savedLab = pref("labHorizon");
    document.querySelectorAll("[data-lab-horizon]").forEach((b) => b.addEventListener("click", () => setLabHorizon(b.dataset.labHorizon)));
    setLabHorizon(["15m", "4h", "1d"].includes(savedLab) ? savedLab : "1h");
    $("lab-run").addEventListener("click", startLab);
    $("ai-mode").addEventListener("change", saveAiSettings);
    $("ai-auto").addEventListener("change", saveAiSettings);
    loadAiSettings();
    $("record-refresh").addEventListener("click", refreshRecord);
    refreshRecord();
    for (const id of ["ev-mode", "ev-news", "ev-ai"]) $(id).addEventListener("change", saveEvidence);
    $("ev-learned").addEventListener("change", toggleLearned);
    $("ev-learn").addEventListener("click", recomputeLearning);
    $("ev-market").addEventListener("click", loadFuturesMarket);
    loadEvidence();

    $("sel-all").addEventListener("click", () => presetSelection("all"));
    $("sel-none").addEventListener("click", () => presetSelection("none"));
    $("sel-top5").addEventListener("click", () => presetSelection(5));
    $("sel-top10").addEventListener("click", () => presetSelection(10));
    $("sel-save").addEventListener("click", saveSelection);
    $("selection-box").addEventListener("toggle", (e) => { if (e.target.open) loadSelection(); });
    loadSelection();

    $("chat-model").addEventListener("change", (e) => { pref("chatModel", e.target.value); syncEffort(); });
    $("chat-effort").value = pref("chatEffort") || "";
    $("chat-effort").addEventListener("change", (e) => pref("chatEffort", e.target.value));
    loadChatModels();

    $("year").textContent = String(new Date().getFullYear());
    document.querySelectorAll("main .card").forEach((el, i) => el.style.setProperty("--i", String(Math.min(i, 12))));

    try {
      chatMessages = JSON.parse(sessionStorage.getItem("chat") || "[]");
    } catch {
      chatMessages = [];
    }
    renderChat();
    $("chat-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const text = $("chat-input").value;
      $("chat-input").value = "";
      sendChat(text);
    });
    $("chat-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        $("chat-form").requestSubmit();
      }
    });
    $("chat-clear").addEventListener("click", () => {
      chatMessages = [];
      renderChat();
    });
    $("detail-ask").addEventListener("click", () => {
      if (!detailSymbol) return;
      const select = $("chat-symbol");
      if (![...select.options].some((o) => o.value === detailSymbol)) select.append(h("option", { value: detailSymbol, text: detailSymbol }));
      select.value = detailSymbol;
      closeDetail();
      $("chat-input").value = `What do you think of the ${detailSymbol} setup right now?`;
      $("chat-input").focus();
    });

    $("cash-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try { renderPortfolio(await api("/api/portfolio/cash", { method: "PUT", body: { cash: Number($("cash-input").value) } })); }
      catch (err) { portfolioError(err); }
    });
    $("position-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = { symbol: $("pos-symbol").value, quantity: Number($("pos-qty").value), average_entry: Number($("pos-avg").value) };
      try {
        renderPortfolio(await api("/api/portfolio/positions", { method: "POST", body }));
        e.target.reset();
      } catch (err) { portfolioError(err); }
    });
    $("risk-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const body = {};
      for (const input of e.target.querySelectorAll("input")) body[input.name] = Number(input.value);
      try { renderPortfolio(await api("/api/portfolio/risk", { method: "PUT", body })); }
      catch (err) { portfolioError(err); }
    });
    $("plan-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const out = $("plan-result");
      out.replaceChildren(h("p", { class: "muted small", text: "Analysing and sizing…" }));
      const budget = $("plan-budget").value ? Number($("plan-budget").value) : null;
      try {
        out.replaceChildren(renderPlan(await api("/api/portfolio/plan", { method: "POST", body: { symbol: $("plan-symbol").value, budget }, timeoutMs: SIGNALS_TIMEOUT_MS })));
      } catch (err) {
        const box = h("div", { class: "alert" });
        setMessage(box, "No plan", [err.message]);
        out.replaceChildren(box);
      }
    });
    refreshPortfolio();
    refreshControl();
    $("detail-close").addEventListener("click", closeDetail);
    dialog().addEventListener("close", () => {
      detailToken++;
      if (location.hash) history.replaceState(null, "", location.pathname + location.search);
    });
    dialog().addEventListener("click", (e) => {
      if (e.target === dialog()) closeDetail();
    });

    $("kill").addEventListener("click", killAll);
    $("resume").addEventListener("click", resumeAll);
    getJSON("/api/control/status").then((st) => {
      renderControl(st);
      if (emergency) refreshSignals(); else refreshAll();
    }).catch(() => refreshAll());
    schedule();
    const hash = decodeURIComponent(location.hash.slice(1));
    if (/^[A-Za-z0-9]{1,20}$/.test(hash)) openDetail(hash.toUpperCase());
  });
})();
